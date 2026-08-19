"""Interface web local para gerar narrações a partir de arquivos SRT ou TXT."""

from __future__ import annotations

import asyncio
import os
import webbrowser
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Timer

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

import narrador
from narrador import NarrationError


# Arquivos de texto comuns têm poucos quilobytes. O limite evita manter uploads
# grandes em memória; a margem adicional cobre os metadados do multipart/form-data.
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = max(MAX_SUBTITLE_BYTES, MAX_TEXT_BYTES) + 1024 * 1024
MAX_SUBTITLE_BLOCKS = 10_000
MAX_SPOKEN_CHARACTERS = 300_000
MAX_TEXT_CHARACTERS = narrador.MAX_CONTINUOUS_TEXT_CHARACTERS
MAX_TIMELINE_MS = 3 * 60 * 60 * 1_000

# Somente vozes brasileiras conhecidas pelo edge-tts podem chegar ao sintetizador.
ALLOWED_VOICES = frozenset(
    {
        "pt-BR-AntonioNeural",
        narrador.DEFAULT_VOICE,
        "pt-BR-ThalitaMultilingualNeural",
    }
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
generation_slot = BoundedSemaphore(value=1)


def _error(message: str, status: int):
    """Cria a forma única das respostas de erro da API."""
    return jsonify(error=message), status


def _continuous_text_request_content() -> tuple[str | None, tuple[str, int] | None]:
    """Seleciona e normaliza exatamente uma fonte de texto do multipart."""
    text_files = request.files.getlist("text")
    direct_contents = request.form.getlist("content")
    if len(text_files) > 1:
        return None, ("Envie somente uma parte de arquivo no campo 'text'.", 400)
    if len(direct_contents) > 1:
        return None, ("Envie somente um campo 'content'.", 400)

    text_file = text_files[0] if text_files else None
    file_selected = text_file is not None and bool(text_file.filename)
    file_data = b""

    if file_selected:
        try:
            file_data = text_file.stream.read(MAX_TEXT_BYTES + 1)
        except (OSError, ValueError):
            return None, ("Não foi possível ler o arquivo enviado.", 400)
        if len(file_data) > MAX_TEXT_BYTES:
            return None, ("O arquivo TXT excede o limite de 5 MB.", 413)

    direct_content = direct_contents[0] if direct_contents else None

    file_is_useful = file_selected and bool(file_data.strip())
    direct_is_useful = direct_content is not None and bool(direct_content.strip())

    if file_is_useful and direct_is_useful:
        return None, ("Envie um arquivo TXT ou informe o texto, não ambos.", 400)

    if not file_is_useful and not direct_is_useful:
        if file_selected:
            return None, ("O arquivo TXT está vazio.", 400)
        if direct_content is not None:
            return None, ("O texto digitado está vazio.", 400)
        return None, ("Envie um arquivo no campo 'text' ou informe o campo 'content'.", 400)

    if direct_is_useful:
        try:
            direct_data = direct_content.encode("utf-8")
        except UnicodeEncodeError:
            return None, ("O texto digitado não pôde ser interpretado como UTF-8.", 422)
        if len(direct_data) > MAX_TEXT_BYTES:
            return None, ("O texto digitado excede o limite de 5 MB.", 413)
        source_content = direct_content

    else:
        # A extensão e o tamanho só importam quando o arquivo é a fonte escolhida.
        assert text_file is not None
        if Path(text_file.filename).suffix.casefold() != ".txt":
            return None, ("O arquivo deve ter a extensão .txt.", 415)
        try:
            source_content = file_data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, ("O arquivo TXT deve usar codificação UTF-8.", 422)

    try:
        normalized_content = narrador.validate_continuous_text(source_content)
    except NarrationError as error:
        return None, (str(error), 422)
    if len(normalized_content) > MAX_TEXT_CHARACTERS:
        return None, (
            "O texto ultrapassa o limite de 20.000 caracteres para uma geração.",
            422,
        )
    return normalized_content, None


def _narration_error_status(error: NarrationError) -> int:
    """Distingue entrada inválida de falhas locais ou de serviços de voz."""
    message = str(error).casefold()

    if "ffmpeg não foi encontrado" in message:
        return 503
    if "mecanismos de voz falharam" in message or "serviço de voz" in message:
        return 502
    if "não foi possível salvar" in message or "falha ao exportar" in message:
        return 500
    return 422


@app.get("/")
def index():
    """Exibe a interface de geração de narrações."""
    return render_template("index.html")


@app.get("/health")
def health():
    """Permite verificar se o servidor HTTP está respondendo."""
    return jsonify(status="ok")


@app.post("/api/narrations")
def create_narration():
    """Valida o formulário, gera o MP3 e o devolve como download."""
    subtitle = request.files.get("subtitle")
    if subtitle is None:
        return _error("Envie um arquivo no campo 'subtitle'.", 400)

    if not subtitle.filename:
        return _error("Selecione um arquivo SRT.", 400)

    if Path(subtitle.filename).suffix.casefold() != ".srt":
        return _error("O arquivo deve ter a extensão .srt.", 415)

    voice = request.form.get("voice", "").strip()
    if not voice:
        return _error("Selecione uma voz.", 400)
    if voice not in ALLOWED_VOICES:
        return _error("A voz selecionada não é permitida.", 400)

    # Ler no máximo um byte além do limite permite detectar excesso mesmo
    # quando o cliente não informa corretamente o Content-Length da parte.
    try:
        subtitle_data = subtitle.stream.read(MAX_SUBTITLE_BYTES + 1)
    except (OSError, ValueError):
        return _error("Não foi possível ler o arquivo enviado.", 400)
    if len(subtitle_data) > MAX_SUBTITLE_BYTES:
        return _error("O arquivo SRT excede o limite de 5 MB.", 413)
    if not subtitle_data.strip():
        return _error("O arquivo SRT está vazio.", 400)

    try:
        # O MP3 é copiado para memória antes do fim do contexto. Assim, o
        # diretório temporário pode ser removido imediatamente também no Windows.
        with TemporaryDirectory(prefix="narrador_web_") as temporary_directory:
            workdir = Path(temporary_directory)
            input_path = workdir / "input.srt"
            output_path = workdir / "narration_synchronized.mp3"
            input_path.write_bytes(subtitle_data)

            subtitles = narrador.load_subtitles(input_path)
            if len(subtitles) > MAX_SUBTITLE_BLOCKS:
                formatted_limit = f"{MAX_SUBTITLE_BLOCKS:,}".replace(",", ".")
                return _error(
                    f"O arquivo possui mais de {formatted_limit} legendas.", 422
                )

            spoken_characters = sum(
                len(narrador.normalize_text(subtitle.content))
                for subtitle in subtitles
            )
            if spoken_characters > MAX_SPOKEN_CHARACTERS:
                return _error("O texto das legendas é grande demais para uma geração.", 422)

            final_timestamp_ms = max(
                narrador.timedelta_to_milliseconds(subtitle.end)
                for subtitle in subtitles
            )
            if final_timestamp_ms > MAX_TIMELINE_MS:
                return _error("A linha do tempo não pode ultrapassar 3 horas.", 422)

            if not generation_slot.acquire(blocking=False):
                response = jsonify(
                    error="Já existe uma narração sendo gerada. Tente novamente em instantes."
                )
                response.status_code = 429
                response.headers["Retry-After"] = "5"
                return response

            try:
                asyncio.run(narrador.build_narration(input_path, output_path, voice))
                mp3_data = output_path.read_bytes()
            finally:
                generation_slot.release()

        if not mp3_data:
            app.logger.error("O gerador concluiu sem produzir conteúdo MP3.")
            return _error("Não foi possível gerar o arquivo de áudio.", 500)

    except NarrationError as error:
        status = _narration_error_status(error)
        if status >= 500:
            app.logger.error("Falha ao gerar narração: %s", error)
            public_messages = {
                500: "Não foi possível concluir a geração do MP3.",
                502: "O serviço de voz está indisponível no momento.",
                503: "O FFmpeg não está disponível neste computador.",
            }
            return _error(public_messages.get(status, "Falha interna na geração."), status)
        return _error(str(error), status)
    except OSError as error:
        app.logger.error("Falha de sistema de arquivos durante a geração: %s", error)
        return _error("Não foi possível processar o arquivo enviado.", 500)
    except Exception:
        # O detalhe fica apenas no log local e nunca é incluído na resposta.
        app.logger.exception("Erro inesperado durante a geração da narração.")
        return _error("Ocorreu um erro inesperado ao gerar a narração.", 500)

    response = send_file(
        BytesIO(mp3_data),
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name="narration_synchronized.mp3",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/api/text-narrations")
def create_text_narration():
    """Gera um MP3 contínuo a partir de arquivo TXT ou texto digitado."""
    normalized_text, source_error = _continuous_text_request_content()
    if source_error is not None:
        return _error(*source_error)
    assert normalized_text is not None

    voice = request.form.get("voice", "").strip()
    if not voice:
        return _error("Selecione uma voz.", 400)
    if voice not in ALLOWED_VOICES:
        return _error("A voz selecionada não é permitida.", 400)

    try:
        with TemporaryDirectory(prefix="narrador_text_web_") as temporary_directory:
            workdir = Path(temporary_directory)
            input_path = workdir / "input.txt"
            output_path = workdir / "narration_continuous.mp3"
            input_path.write_text(normalized_text, encoding="utf-8")

            if not generation_slot.acquire(blocking=False):
                response = jsonify(
                    error="Já existe uma narração sendo gerada. Tente novamente em instantes."
                )
                response.status_code = 429
                response.headers["Retry-After"] = "5"
                return response

            try:
                asyncio.run(
                    narrador.build_text_narration(input_path, output_path, voice)
                )
                mp3_data = output_path.read_bytes()
            finally:
                generation_slot.release()

        if not mp3_data:
            app.logger.error("O gerador de texto concluiu sem produzir conteúdo MP3.")
            return _error("Não foi possível gerar o arquivo de áudio.", 500)

    except NarrationError as error:
        status = _narration_error_status(error)
        if status >= 500:
            app.logger.error("Falha ao gerar narração de texto: %s", error)
            public_messages = {
                500: "Não foi possível concluir a geração do MP3.",
                502: "O serviço de voz está indisponível no momento.",
                503: "O FFmpeg não está disponível neste computador.",
            }
            return _error(public_messages.get(status, "Falha interna na geração."), status)
        return _error(str(error), status)
    except OSError as error:
        app.logger.error(
            "Falha de sistema de arquivos durante a geração de texto: %s", error
        )
        return _error("Não foi possível processar o arquivo enviado.", 500)
    except Exception:
        app.logger.exception("Erro inesperado durante a geração de texto.")
        return _error("Ocorreu um erro inesperado ao gerar a narração.", 500)

    response = send_file(
        BytesIO(mp3_data),
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name="narration_continuous.mp3",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error_details: RequestEntityTooLarge):
    """Mantém o formato JSON quando o corpo inteiro ultrapassa o limite."""
    return _error("O upload excede o limite permitido de 5 MB.", 413)


if __name__ == "__main__":
    local_url = "http://127.0.0.1:5000"
    print(f"Abra o Narrador em: {local_url}")
    if os.environ.get("NARRADOR_OPEN_BROWSER", "1") == "1":
        browser_timer = Timer(1.0, webbrowser.open, args=(local_url,))
        browser_timer.daemon = True
        browser_timer.start()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
