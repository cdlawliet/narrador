from __future__ import annotations

import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import app as web
import narrador


class ContinuousTextTests(unittest.TestCase):
    def test_load_preserves_punctuation_accents_and_paragraphs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "texto.txt"
            input_path.write_bytes(
                b"\xef\xbb\xbfOl\xc3\xa1, mundo!\r\nContinua aqui.\r\n\r\n"
                b"Segundo par\xc3\xa1grafo: voc\xc3\xaa est\xc3\xa1 bem?\n"
            )

            text = narrador.load_continuous_text(input_path)

        self.assertEqual(
            text,
            "Olá, mundo! Continua aqui.\n\nSegundo parágrafo: você está bem?",
        )

    def test_load_rejects_invalid_utf8_and_control_characters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid_utf8 = Path(directory) / "invalid-utf8.txt"
            invalid_utf8.write_bytes(b"texto\xff")
            with self.assertRaisesRegex(narrador.NarrationError, "UTF-8"):
                narrador.load_continuous_text(invalid_utf8)

            invalid_control = Path(directory) / "invalid-control.txt"
            invalid_control.write_bytes(b"texto\x00oculto")
            with self.assertRaisesRegex(narrador.NarrationError, "controle"):
                narrador.load_continuous_text(invalid_control)

    def test_shared_validation_rejects_dangerous_or_invisible_unicode(self) -> None:
        self.assertEqual(
            narrador.validate_continuous_text("\ufeffOlá!"),
            "Olá!",
        )
        for content in (
            "texto\ufeffinterno",
            "texto\u202einvertido",
            "texto\u2066isolado",
            "\u200b\u2060",
        ):
            with self.subTest(content=ascii(content)):
                with self.assertRaises(narrador.NarrationError):
                    narrador.validate_continuous_text(content)

    def test_build_uses_one_logical_synthesis_call(self) -> None:
        calls: list[tuple[str, Path, int]] = []

        class FakeSynthesizer:
            def __init__(self, voice: str) -> None:
                self.voice = voice

            async def synthesize(
                self, text: str, workdir: Path, sequence: int
            ) -> narrador.AudioSegment:
                calls.append((text, workdir, sequence))
                return narrador.silent_audio(25)

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "texto.txt"
            output_path = Path(directory) / "saida.mp3"
            input_path.write_text(
                "Primeira frase!\n\nSegunda frase, com acentuação.",
                encoding="utf-8",
            )

            with (
                patch.object(narrador, "SpeechSynthesizer", FakeSynthesizer),
                patch.object(narrador.shutil, "which", return_value="ffmpeg"),
                patch.object(narrador, "export_mp3") as export_mp3,
            ):
                asyncio.run(
                    narrador.build_text_narration(
                        input_path, output_path, "pt-BR-FranciscaNeural"
                    )
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][0],
            "Primeira frase!\n\nSegunda frase, com acentuação.",
        )
        self.assertEqual(calls[0][2], 1)
        export_mp3.assert_called_once()


class TextNarrationEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        web.app.config.update(TESTING=True)
        self.client = web.app.test_client()

    def test_index_offers_file_and_direct_text_sources(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name="textSource" value="file"', html)
        self.assertIn('name="textSource" value="content"', html)
        self.assertIn('textarea\n                id="textContent"', html)
        self.assertIn('name="content"', html)

    def _post(self, file_content: bytes, filename: str = "texto.txt", **fields: str):
        data = {
            "text": (BytesIO(file_content), filename),
            "voice": "pt-BR-FranciscaNeural",
        }
        data.update(fields)
        return self.client.post("/api/text-narrations", data=data)

    def _post_content(self, content: str, **fields: str):
        data = {
            "content": content,
            "voice": "pt-BR-FranciscaNeural",
        }
        data.update(fields)
        return self.client.post("/api/text-narrations", data=data)

    def test_requires_txt_and_allowed_voice(self) -> None:
        response = self.client.post("/api/text-narrations", data={})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.is_json)

        response = self._post(b"Texto.", filename="texto.srt")
        self.assertEqual(response.status_code, 415)

        response = self._post(b"Texto.", voice="voz-invalida")
        self.assertEqual(response.status_code, 400)

    def test_rejects_invalid_encoding_controls_and_character_limit(self) -> None:
        self.assertEqual(self._post(b"texto\xff").status_code, 422)
        self.assertEqual(self._post(b"texto\x00oculto").status_code, 422)

        oversized_text = b"a" * (web.MAX_TEXT_CHARACTERS + 1)
        response = self._post(oversized_text)
        self.assertEqual(response.status_code, 422)
        self.assertIn("20.000", response.get_json()["error"])

    def test_rejects_upload_larger_than_five_megabytes(self) -> None:
        for payload in (
            b"a" * (web.MAX_TEXT_BYTES + 1),
            b" " * (web.MAX_TEXT_BYTES + 1),
        ):
            with self.subTest(whitespace_only=not bool(payload.strip())):
                response = self._post(payload)
                self.assertEqual(response.status_code, 413)
                self.assertTrue(response.is_json)

    def test_accepts_direct_content_and_uses_shared_normalization(self) -> None:
        received: list[tuple[str, str]] = []

        async def fake_build(
            input_path: Path, output_path: Path, voice: str
        ) -> None:
            received.append((narrador.load_continuous_text(input_path), voice))
            output_path.write_bytes(b"ID3-direct-content")

        with patch.object(web.narrador, "build_text_narration", fake_build):
            # Formulários de navegador podem enviar o input de arquivo vazio.
            response = self.client.post(
                "/api/text-narrations",
                data={
                    "text": (BytesIO(b""), ""),
                    "content": "  Olá, mundo!\r\n\r\n  Segundo parágrafo.  ",
                    "voice": "pt-BR-FranciscaNeural",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ID3-direct-content")
        self.assertEqual(
            received,
            [("Olá, mundo!\n\nSegundo parágrafo.", "pt-BR-FranciscaNeural")],
        )

    def test_rejects_empty_direct_content(self) -> None:
        for content in ("", "  \r\n\t  "):
            with self.subTest(content=repr(content)):
                response = self._post_content(content)
                self.assertEqual(response.status_code, 400)
                self.assertIn("vazio", response.get_json()["error"])

    def test_rejects_two_useful_text_sources(self) -> None:
        response = self._post(b"Texto do arquivo.", content="Texto digitado.")
        self.assertEqual(response.status_code, 400)
        self.assertIn("não ambos", response.get_json()["error"])

    def test_rejects_duplicate_file_parts_and_content_fields(self) -> None:
        duplicate_files = MultiDict(
            [
                ("text", (BytesIO(b"Primeiro."), "primeiro.txt")),
                ("text", (BytesIO(b"Segundo."), "segundo.txt")),
                ("voice", "pt-BR-FranciscaNeural"),
            ]
        )
        response = self.client.post("/api/text-narrations", data=duplicate_files)
        self.assertEqual(response.status_code, 400)

        duplicate_content = MultiDict(
            [
                ("content", "Primeiro."),
                ("content", "Segundo."),
                ("voice", "pt-BR-FranciscaNeural"),
            ]
        )
        response = self.client.post("/api/text-narrations", data=duplicate_content)
        self.assertEqual(response.status_code, 400)

    def test_direct_content_reuses_controls_and_character_limit(self) -> None:
        self.assertEqual(self._post_content("texto\x00oculto").status_code, 422)

        for content in ("texto\u202eoculto", "texto\ufeffoculto", "\u200b\u2060"):
            with self.subTest(content=ascii(content)):
                self.assertEqual(self._post_content(content).status_code, 422)

        response = self._post_content("a" * (web.MAX_TEXT_CHARACTERS + 1))
        self.assertEqual(response.status_code, 422)
        self.assertIn("20.000", response.get_json()["error"])

    def test_character_limit_uses_normalized_tts_text(self) -> None:
        async def fake_build(
            input_path: Path, output_path: Path, _voice: str
        ) -> None:
            self.assertEqual(narrador.load_continuous_text(input_path), "Texto curto.")
            output_path.write_bytes(b"ID3-normalized-limit")

        raw_content = "Texto curto." + (" " * (web.MAX_TEXT_CHARACTERS + 1))
        with patch.object(web.narrador, "build_text_narration", fake_build):
            response = self._post_content(raw_content)

        self.assertEqual(response.status_code, 200)

    def test_accepts_twenty_thousand_unicode_codepoints(self) -> None:
        async def fake_build(
            input_path: Path, output_path: Path, _voice: str
        ) -> None:
            self.assertEqual(
                len(narrador.load_continuous_text(input_path)),
                web.MAX_TEXT_CHARACTERS,
            )
            output_path.write_bytes(b"ID3-unicode-limit")

        with patch.object(web.narrador, "build_text_narration", fake_build):
            response = self._post_content("😀" * web.MAX_TEXT_CHARACTERS)

        self.assertEqual(response.status_code, 200)

    def test_returns_continuous_mp3_attachment(self) -> None:
        received: list[tuple[str, str]] = []

        async def fake_build(
            input_path: Path, output_path: Path, voice: str
        ) -> None:
            received.append(
                (narrador.load_continuous_text(input_path), voice)
            )
            output_path.write_bytes(b"ID3-continuous-audio")

        with patch.object(web.narrador, "build_text_narration", fake_build):
            response = self._post(
                "Olá!\n\nComo você está?".encode("utf-8")
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ID3-continuous-audio")
        self.assertEqual(response.mimetype, "audio/mpeg")
        self.assertIn(
            "narration_continuous.mp3", response.headers["Content-Disposition"]
        )
        self.assertEqual(
            received,
            [("Olá!\n\nComo você está?", "pt-BR-FranciscaNeural")],
        )

    def test_reuses_generation_semaphore(self) -> None:
        self.assertTrue(web.generation_slot.acquire(blocking=False))
        try:
            response = self._post(b"Uma frase valida.")
        finally:
            web.generation_slot.release()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "5")

    def test_maps_voice_service_failure_without_internal_details(self) -> None:
        async def failed_build(*_args) -> None:
            raise narrador.NarrationError(
                "Os dois mecanismos de voz falharam. detalhe privado"
            )

        with patch.object(web.narrador, "build_text_narration", failed_build):
            response = self._post(b"Uma frase valida.")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": "O serviço de voz está indisponível no momento."},
        )

    def test_existing_srt_endpoint_remains_available(self) -> None:
        async def fake_srt_build(
            _input_path: Path, output_path: Path, _voice: str
        ) -> None:
            output_path.write_bytes(b"ID3-synchronized-audio")

        subtitle = (
            b"1\n00:00:00,000 --> 00:00:01,000\nLegenda preservada.\n"
        )
        with patch.object(web.narrador, "build_narration", fake_srt_build):
            response = self.client.post(
                "/api/narrations",
                data={
                    "subtitle": (BytesIO(subtitle), "legenda.srt"),
                    "voice": "pt-BR-FranciscaNeural",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ID3-synchronized-audio")
        self.assertIn(
            "narration_synchronized.mp3",
            response.headers["Content-Disposition"],
        )


if __name__ == "__main__":
    unittest.main()
