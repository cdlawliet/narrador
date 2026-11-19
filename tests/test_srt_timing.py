import asyncio
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import narrador
from pydub.generators import Sine


class SpeechSynthesizerTests(unittest.TestCase):
    def test_uses_selected_voice_even_when_the_provider_fails(self):
        with TemporaryDirectory() as directory:
            with patch("edge_tts.Communicate") as communicate:
                communicate.return_value.save = AsyncMock(side_effect=TimeoutError)
                voice = "pt-BR-AntonioNeural"
                with self.assertRaisesRegex(narrador.NarrationError, voice):
                    asyncio.run(
                        narrador.SpeechSynthesizer(voice).synthesize(
                            "Bom dia!", Path(directory), 1
                        )
                    )

        self.assertEqual(communicate.call_args.kwargs["voice"], voice)
        communicate.return_value.save.assert_awaited_once()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg necessário para ajustar as falas")
class SrtTimingTests(unittest.TestCase):
    def test_overlapping_cues_use_only_the_time_before_the_next_start(self):
        subtitle_source = (
            "1\n00:00:00,200 --> 00:00:01,800\nPrimeira fala.\n\n"
            "2\n00:00:01,200 --> 00:00:02,000\nSegunda fala.\n"
        )
        first = Sine(440, sample_rate=narrador.SAMPLE_RATE).to_audio_segment(1_400)
        second = Sine(660, sample_rate=narrador.SAMPLE_RATE).to_audio_segment(600)

        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "legendas.srt"
            input_path.write_text(subtitle_source, encoding="utf-8")
            with (
                patch.object(
                    narrador.SpeechSynthesizer, "synthesize", side_effect=[first, second]
                ),
                patch.object(narrador, "export_mp3") as export,
            ):
                asyncio.run(narrador.build_narration(input_path, input_path.with_suffix(".mp3")))

        timeline = export.call_args.args[0]
        self.assertEqual(len(timeline), 2_000)
        self.assertEqual(timeline[:200].rms, 0)
        self.assertGreater(timeline[200:900].rms, 0)
        self.assertEqual(timeline[1_200:1_800].raw_data, second.raw_data)
        self.assertEqual(timeline[1_800:].rms, 0)

    def test_reports_the_cue_when_speeding_up_is_insufficient(self):
        speech = Sine(440, sample_rate=narrador.SAMPLE_RATE).to_audio_segment(1_000)
        with self.assertRaisesRegex(narrador.NarrationError, "legenda 7 não cabe"):
            narrador.fit_speech(speech, available_ms=200, label=7)
