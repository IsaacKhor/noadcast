"""MP3 splice hints remain weak evidence and keep decoder timestamps."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from noadcast.classify.core import TranscriptClassifier
from noadcast.classify.render import coalesce_sentences, render_transcript
from noadcast.pipeline.stages import _mp3_breaks
from noadcast.transcribe.protocol import Mp3Break
from tests.test_classify_helpers import request, sentence
from tests.test_stages import StageTestCase


class Mp3BreakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sentences = [
            sentence(0, 1.0, 5.0, "The story begins."),
            sentence(1, 10.0, 15.0, "The story continues."),
        ]
        self.hint = Mp3Break(8.25, "embedded_id3")

    def test_render_places_hint_between_sentences_without_relabeling_it(self) -> None:
        text = render_transcript(self.sentences, [], fmt="sentences", include_silence=False,
                                 mp3_breaks=[self.hint]).text.splitlines()
        self.assertEqual(len(text), 3)
        self.assertEqual(text[0], "[1.00-5.00] The story begins.")
        self.assertEqual(text[2], "[10.00-15.00] The story continues.")
        self.assertIn("[8.25] --- MP3 structure changes here (embedded ID3 tag", text[1])
        self.assertIn("not proof of an ad", text[1])

    def test_break_prevents_sentence_coalescing_across_splice(self) -> None:
        first = sentence(0, 1.0, 5.0, "The story",)
        second = sentence(1, 10.0, 15.0, "continues.")
        first = replace(first, break_reason="gap")
        self.assertEqual(len(coalesce_sentences([first, second])), 1)
        self.assertEqual(len(coalesce_sentences([first, second], [self.hint])), 2)

    def test_v4_prompt_receives_cue_and_v3_remains_immutable(self) -> None:
        req = request(self.sentences, duration=20.0, mp3_breaks=[self.hint])
        current = TranscriptClassifier(model="deepseek/deepseek-v4.1-flash")
        old = TranscriptClassifier(model="deepseek/deepseek-v4.1-flash", prompt_version="segments-v3")
        prepared = current.prepare(req)
        self.assertIn("MP3 structure changes here", prepared.user_text)
        self.assertIn("A break alone does not establish an ad", current.prompt.system)
        self.assertNotIn("MP3 structure changes here", old.prepare(req).user_text)
        self.assertNotEqual(prepared.request_sha256, current.prepare(request(self.sentences, duration=20.0)).request_sha256)

    def test_chunk_around_silent_splice_keeps_hint(self) -> None:
        spaced = [
            sentence(0, 1.0, 5.0, "First chapter."),
            sentence(1, 3000.0, 3005.0, "Second chapter."),
            sentence(2, 6000.0, 6005.0, "Third chapter."),
        ]
        classifier = TranscriptClassifier(model="deepseek/deepseek-v4.1-flash", max_input_tokens=1)
        jobs = classifier._jobs(classifier.prepare(request(
            spaced, duration=6010.0, mp3_breaks=[Mp3Break(2999.0, "encoder_header")]
        )))
        self.assertGreater(len(jobs), 1)
        self.assertTrue(any("[2999.00] --- MP3 structure changes" in job.user_text for job in jobs))

    def test_bad_stored_hints_are_ignored(self) -> None:
        payload = json.dumps({"mp3_breaks": [
            {"seconds": 8.25, "kind": "embedded_id3"},
            {"seconds": -1, "kind": "decode_error"},
            {"seconds": 9, "kind": "unknown"},
            {"seconds": "11", "kind": "encoder_header"},
        ]})
        self.assertEqual(_mp3_breaks(payload, 20), [self.hint])
        self.assertEqual(_mp3_breaks("bad json", 20), [])


class PipelineHintTests(StageTestCase):
    async def test_stored_decoder_hint_reaches_classifier(self) -> None:
        original_transcribe = self.transcriber.transcribe

        async def with_hint(*args, **kwargs):
            result = await original_transcribe(*args, **kwargs)
            return replace(result, options={**result.options, "mp3_breaks": [
                {"seconds": 123.456, "kind": "encoder_header"},
            ]})

        self.transcriber.transcribe = with_hint
        self.process()
        await self.run_chain()
        [received] = self.ctx.classifiers.classifiers["openrouter"].requests
        self.assertEqual(received.mp3_breaks, [Mp3Break(123.456, "encoder_header")])


if __name__ == "__main__":
    unittest.main()
