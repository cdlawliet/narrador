"""Gera narrações MP3 sincronizadas por SRT ou contínuas a partir de TXT."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    import srt
    from pydub import AudioSegment
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"Dependência ausente ({exc.name}). "
        "Execute: python -m pip install -r requirements.txt"
    ) from None


DEFAULT_INPUT = Path("input.srt")
DEFAULT_OUTPUT = Path("narration_synchronized.mp3")
DEFAULT_VOICE = "pt-BR-FranciscaNeural"
SAMPLE_RATE = 24_000
MAX_SPEECH_TEMPO = 2.0
MAX_CONTINUOUS_TEXT_CHARACTERS = 20_000
FORBIDDEN_BIDI_CHARACTERS = frozenset(
    chr(codepoint)
    for start, end in ((0x202A, 0x202E), (0x2066, 0x2069))
    for codepoint in range(start, end + 1)
)


class NarrationError(RuntimeError):
    """Erro esperado durante a criação da narração."""


def timedelta_to_milliseconds(value: timedelta) -> int:
    """Converte timedelta para milissegundos sem usar ponto flutuante."""
    return (
        value.days * 86_400_000
        + value.seconds * 1_000
        + value.microseconds // 1_000
    )


def normalize_text(content: str) -> str:
    """Transforma legendas multilinha em uma frase, preservando o conteúdo."""
    return " ".join(content.split())


def normalize_continuous_text(content: str) -> str:
    """Normaliza espaços sem remover pontuação, acentos ou parágrafos."""
    content = unicodedata.normalize("NFC", content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n[\t ]*\n+", content)
    normalized_paragraphs = [
        " ".join(paragraph.split()) for paragraph in paragraphs if paragraph.strip()
    ]
    return "\n\n".join(normalized_paragraphs)


def validate_continuous_text(content: str) -> str:
    """Valida Unicode e devolve exatamente o texto normalizado enviado ao TTS."""
    # Um BOM inicial representa a codificação do documento; no meio do texto,
    # ele é um caractere de formatação invisível e deve ser rejeitado.
    if content.startswith("\ufeff"):
        content = content[1:]
    if "\ufeff" in content:
        raise NarrationError("O texto contém um marcador BOM interno inválido.")

    if any(character in FORBIDDEN_BIDI_CHARACTERS for character in content):
        raise NarrationError("O texto contém caracteres bidirecionais invisíveis.")

    invalid_control = next(
        (
            character
            for character in content
            if unicodedata.category(character) == "Cc"
            and character not in "\n\r\t"
        ),
        None,
    )
    if invalid_control is not None:
        raise NarrationError("O texto contém caracteres de controle inválidos.")

    normalized = normalize_continuous_text(content)
    has_visible_character = any(
        not character.isspace()
        and unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
        for character in normalized
    )
    if not normalized or not has_visible_character:
        raise NarrationError("O texto não contém caracteres visíveis para narrar.")
    return normalized


def load_continuous_text(input_path: Path) -> str:
    """Lê um TXT UTF-8/BOM e preserva sua estrutura semântica para o TTS."""
    if not input_path.is_file():
        raise NarrationError(f"Arquivo de entrada não encontrado: {input_path}")

    try:
        source = input_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NarrationError(
            f"Não foi possível ler {input_path} como UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise NarrationError(f"Não foi possível ler {input_path}: {exc}") from exc

    return validate_continuous_text(source)


def normalize_audio(audio: AudioSegment) -> AudioSegment:
    """Padroniza os trechos antes da mixagem."""
    return audio.set_frame_rate(SAMPLE_RATE).set_channels(1).set_sample_width(2)


def silent_audio(duration_ms: int) -> AudioSegment:
    """Cria silêncio mono de 24 kHz com a duração informada."""
    return normalize_audio(
        AudioSegment.silent(duration=max(0, duration_ms), frame_rate=SAMPLE_RATE)
    )


def load_subtitles(input_path: Path) -> list[srt.Subtitle]:
    """Lê, interpreta, ordena e valida as legendas."""
    if not input_path.is_file():
        raise NarrationError(f"Arquivo de entrada não encontrado: {input_path}")

    try:
        source = input_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NarrationError(
            f"Não foi possível ler {input_path} como UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise NarrationError(f"Não foi possível ler {input_path}: {exc}") from exc

    if not source.strip():
        raise NarrationError("O arquivo SRT está vazio.")

    try:
        subtitles = list(srt.parse(source))
    except Exception as exc:
        raise NarrationError(f"O arquivo SRT é inválido: {exc}") from exc

    if not subtitles:
        raise NarrationError("Nenhuma legenda foi encontrada no arquivo SRT.")

    subtitles.sort(key=lambda item: (item.start, item.end, item.index or 0))

    for position, subtitle in enumerate(subtitles, start=1):
        start_ms = timedelta_to_milliseconds(subtitle.start)
        end_ms = timedelta_to_milliseconds(subtitle.end)
        label = subtitle.index if subtitle.index is not None else position

        if start_ms < 0:
            raise NarrationError(f"A legenda {label} possui timestamp inicial negativo.")
        if end_ms < start_ms:
            raise NarrationError(
                f"A legenda {label} termina antes de seu timestamp inicial."
            )

    return subtitles


def audio_from_mp3(path: Path) -> AudioSegment:
    """Carrega e valida um MP3 produzido por um mecanismo de voz."""
    if not path.is_file() or path.stat().st_size == 0:
        raise NarrationError("O serviço de voz não produziu áudio.")

    try:
        audio = AudioSegment.from_file(str(path), format="mp3")
    except Exception as exc:
        raise NarrationError(f"O áudio gerado não pôde ser decodificado: {exc}") from exc

    if len(audio) == 0:
        raise NarrationError("O serviço de voz produziu um áudio vazio.")

    return normalize_audio(audio)


class SpeechSynthesizer:
    """Sintetiza com a voz solicitada no Edge TTS."""

    def __init__(self, voice: str) -> None:
        self.voice = voice

    async def synthesize(
        self, text: str, workdir: Path, sequence: int
    ) -> AudioSegment:
        edge_path = workdir / f"edge_{sequence:06d}.mp3"
        try:
            import edge_tts

            communication = edge_tts.Communicate(
                text=text,
                voice=self.voice,
                connect_timeout=10,
                receive_timeout=60,
            )
            await communication.save(str(edge_path))
            return audio_from_mp3(edge_path)
        except Exception as exc:
            status = getattr(exc, "status", None)
            detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
            raise NarrationError(
                f"O serviço de voz não conseguiu sintetizar {self.voice} ({detail})."
            ) from exc


def fit_speech(speech: AudioSegment, available_ms: int, label: int) -> AudioSegment:
    """Ajusta o tempo sem mudar o tom e confere a quantidade real de amostras."""
    frame_limit = available_ms * SAMPLE_RATE // 1_000
    byte_limit = frame_limit * speech.frame_width
    if available_ms > 0 and len(speech.raw_data) <= byte_limit:
        return speech

    if available_ms > 0:
        # O atempo pode devolver alguns milissegundos a mais que o estimado.
        target_ms = max(1, available_ms - min(30, available_ms // 20))
        tempo = min(MAX_SPEECH_TEMPO, len(speech) / target_ms)
        for speed in dict.fromkeys((max(1.0, tempo), MAX_SPEECH_TEMPO)):
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                        "-i", "pipe:0", "-af", f"atempo={speed:.8f}",
                        "-f", "s16le", "pipe:1",
                    ],
                    input=speech.raw_data,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise NarrationError("Falha ao ajustar a duração com FFmpeg.") from exc

            if 0 < len(result.stdout) <= byte_limit:
                return AudioSegment(
                    result.stdout, sample_width=2, frame_rate=SAMPLE_RATE, channels=1
                )

    raise NarrationError(
        f"A fala da legenda {label} não cabe no intervalo de {available_ms / 1_000:g} s. "
        "Aumente o intervalo ou encurte o texto da legenda."
    )


def export_mp3(timeline: AudioSegment, output_path: Path) -> None:
    """Exporta um AudioSegment como MP3 por substituição atômica."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(
            prefix=".narrador_export_", dir=output_path.parent
        ) as export_directory:
            temporary_output = Path(export_directory) / "narration.mp3"
            exported_file = timeline.export(
                str(temporary_output), format="mp3", bitrate="192k"
            )
            exported_file.close()
            os.replace(temporary_output, output_path)
    except OSError as exc:
        raise NarrationError(f"Não foi possível salvar {output_path}: {exc}") from exc
    except Exception as exc:
        raise NarrationError(f"Falha ao exportar o MP3 com FFmpeg: {exc}") from exc


async def build_text_narration(
    input_path: Path,
    output_path: Path,
    voice: str = DEFAULT_VOICE,
) -> None:
    """Gera uma narração contínua a partir de um arquivo TXT."""
    text = load_continuous_text(input_path)
    if len(text) > MAX_CONTINUOUS_TEXT_CHARACTERS:
        raise NarrationError(
            "O texto ultrapassa o limite de 20.000 caracteres para uma geração."
        )

    if shutil.which("ffmpeg") is None:
        raise NarrationError(
            "FFmpeg não foi encontrado no PATH. Instale-o antes de gerar o MP3."
        )

    synthesizer = SpeechSynthesizer(voice)
    with TemporaryDirectory(prefix="narrador_text_tts_") as temporary_directory:
        workdir = Path(temporary_directory)
        print("Gerando narração do texto corrido...")
        try:
            speech = await synthesizer.synthesize(text, workdir, 1)
        except NarrationError as exc:
            raise NarrationError(f"Falha ao narrar o texto: {exc}") from exc

    export_mp3(speech, output_path)
    print(
        f"Narração concluída: {output_path.resolve()} "
        f"({len(speech) / 1000:.3f} s)"
    )


async def build_narration(
    input_path: Path,
    output_path: Path,
    voice: str = DEFAULT_VOICE,
) -> None:
    """Gera as falas, posiciona-as na linha do tempo e exporta o MP3."""
    # Valide primeiro a entrada para produzir o erro mais útil ao usuário.
    subtitles = load_subtitles(input_path)

    if shutil.which("ffmpeg") is None:
        raise NarrationError(
            "FFmpeg não foi encontrado no PATH. Instale-o antes de gerar o MP3."
        )

    final_subtitle_ms = max(
        timedelta_to_milliseconds(subtitle.end) for subtitle in subtitles
    )
    timeline = silent_audio(final_subtitle_ms)
    synthesizer = SpeechSynthesizer(voice)
    spoken_subtitles = [
        (position, subtitle, text)
        for position, subtitle in enumerate(subtitles, start=1)
        if (text := normalize_text(subtitle.content))
    ]

    with TemporaryDirectory(prefix="narrador_tts_") as temporary_directory:
        workdir = Path(temporary_directory)

        for i, (position, subtitle, text) in enumerate(spoken_subtitles):
            start_ms = timedelta_to_milliseconds(subtitle.start)
            end_ms = timedelta_to_milliseconds(subtitle.end)
            if i + 1 < len(spoken_subtitles):
                next_start = spoken_subtitles[i + 1][1].start
                end_ms = min(end_ms, timedelta_to_milliseconds(next_start))
            label = subtitle.index if subtitle.index is not None else position
            available_ms = end_ms - start_ms
            if available_ms <= 0:
                raise NarrationError(
                    f"A legenda {label} ficou sem intervalo para narrar. "
                    "Verifique os timestamps do SRT."
                )
            print(f"[{position}/{len(subtitles)}] Gerando legenda {label}...")

            try:
                speech = await synthesizer.synthesize(text, workdir, position)
            except NarrationError as exc:
                raise NarrationError(f"Falha na legenda {label}: {exc}") from exc

            speech = fit_speech(speech, available_ms, label)
            timeline = timeline.overlay(speech, position=start_ms)

    if not spoken_subtitles and len(timeline) == 0:
        raise NarrationError("O arquivo SRT não contém texto nem duração de áudio.")

    export_mp3(timeline, output_path)

    print(
        f"Narração concluída: {output_path.resolve()} "
        f"({len(timeline) / 1000:.3f} s)"
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera uma narração sincronizada a partir de um arquivo SRT."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT,
        help="arquivo SRT de entrada (padrão: input.srt)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="arquivo MP3 de saída (padrão: narration_synchronized.mp3)",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"voz do edge-tts (padrão: {DEFAULT_VOICE})",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        asyncio.run(build_narration(arguments.input, arguments.output, arguments.voice))
    except KeyboardInterrupt:
        print("Operação cancelada.", file=sys.stderr)
        return 130
    except NarrationError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
