from __future__ import annotations

import unittest
from dataclasses import dataclass, replace

from noadcast.classify.core import resolve_lines
from noadcast.classify.render import collapse_repeats, render_transcript
from noadcast.transcribe.joiner import join_words
from noadcast.transcribe.protocol import Word
from tests.test_classify_helpers import episode, segment, sentence, sentences, silence


# The recovered server's formatter, verbatim from `git show 3e6d713:server/main.py`,
# as the oracle for the v1 "seconds" format.
@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


def format_transcript(transcript: list[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{segment.start_seconds:.2f} - {segment.end_seconds:.2f}] {segment.text}"
        for segment in transcript
    )


class SecondsFormatTests(unittest.TestCase):
    def test_matches_recovered_format_transcript_byte_for_byte(self) -> None:
        rows = [
            (0.0, 3.0, "A quick warning, there are curse words that are unbeeped in today's episode."),
            (0.005, 1.004999, "Rounding at the half cent."),
            (59.999, 61.5, "Café au lait, naïve résumé — em dash."),
            (1234.5678, 1240.0, "Numbers like 3.14 and U.S. stay as spoken."),
            (3892.13, 3909.14, "Next week on the podcast, or in your local public radio station."),
        ]
        expected = format_transcript([TranscriptSegment(*row) for row in rows])
        rendered = render_transcript(sentences(*rows), [], fmt="seconds", include_silence=False)
        self.assertEqual(rendered.text, expected)
        self.assertEqual(rendered.text.encode("utf-8"), expected.encode("utf-8"))
        self.assertEqual(rendered.format, "seconds")
        self.assertFalse(rendered.include_silence)

    def test_silences_are_ignored_when_off(self) -> None:
        req = episode()
        rendered = render_transcript(req.sentences, req.silences, fmt="seconds", include_silence=False)
        self.assertNotIn("no speech", rendered.text)
        self.assertEqual(len(rendered.text.splitlines()), len(req.sentences))

    def test_silence_lines_interleave_when_on(self) -> None:
        req = episode()
        rendered = render_transcript(
            req.sentences, req.silences, fmt="seconds", include_silence=True, episode_duration=185.0
        )
        lines = rendered.text.splitlines()
        self.assertTrue(rendered.include_silence)
        self.assertEqual(lines[0], "[0.00 - 1.50] --- 2s of no speech ---")
        self.assertEqual(lines[1], "[1.50 - 8.00] From WBEZ Chicago, it's This American Life.")
        self.assertEqual(lines[-1], "[170.00 - 185.00] --- 15s of no speech (end of audio at 185.00) ---")


class SentenceFormatTests(unittest.TestCase):
    def test_exact_joined_sentence_line_and_no_silence(self) -> None:
        rendered = render_transcript(
            sentences((22.24, 23.88, "A complete sentence.")),
            [silence(0.0, 22.24, "head")],
            fmt="sentences",
            include_silence=False,
        )
        self.assertEqual(rendered.text, "[22.24-23.88] A complete sentence.")
        self.assertEqual(rendered.line_times, {0: (22.24, 23.88)})
        self.assertEqual((rendered.format, rendered.include_silence), ("sentences", False))

    def test_words_across_asr_segments_render_as_one_sentence(self) -> None:
        words = [
            Word(22.24, 22.60, " A", 0.9, 0),
            Word(22.60, 23.10, " complete", 0.9, 0),
            Word(23.10, 23.88, " sentence.", 0.9, 1),
        ]
        [joined] = join_words(words)
        self.assertEqual(joined.asr_segments, (0, 1))
        rendered = render_transcript([joined], [], fmt="sentences", include_silence=False)
        self.assertEqual(rendered.text, "[22.24-23.88] A complete sentence.")

    def test_gap_and_cap_fragments_join_through_next_punctuation(self) -> None:
        fragments = [
            replace(sentence(0, 22.24, 22.90, "This is a"), break_reason="gap"),
            replace(sentence(1, 23.00, 23.88, "complete sentence."), break_reason="punct"),
            replace(sentence(2, 30.00, 30.60, "A length capped"), break_reason="cap"),
            replace(sentence(3, 30.70, 31.25, "sentence ends here."), break_reason="punct"),
            replace(sentence(4, 40.00, 40.70, "Unpunctuated speech"), break_reason="eof"),
        ]
        rendered = render_transcript(fragments, [], fmt="sentences", include_silence=False)
        self.assertEqual(rendered.text, "[22.24-23.88] This is a complete sentence.\n"
                                        "[30.00-31.25] A length capped sentence ends here.\n"
                                        "[40.00-40.70] Unpunctuated speech")

    def test_sentence_format_rejects_silence_rows(self) -> None:
        with self.assertRaises(ValueError):
            render_transcript([], [], fmt="sentences", include_silence=True)


class IndexFormatTests(unittest.TestCase):
    def test_interleaves_silence_lines_with_one_line_index(self) -> None:
        req = episode()
        rendered = render_transcript(req.sentences, req.silences, fmt="index", episode_duration=185.0)
        self.assertEqual(
            rendered.text.splitlines(),
            [
                "0|0| --- 2s of no speech ---",
                "1|1| From WBEZ Chicago, it's This American Life.",
                "2|8| I'm Ira Glass.",
                "3|14| --- 6s of no speech ---",
                "4|20| Today on our program, a story about a house.",
                "5|34| It starts in a small town.",
                "6|50| --- 10s of no speech ---",
                "7|60| Support for this show comes from Acme Mattresses.",
                "8|75| Use promo code SLEEP for ten percent off.",
                "9|90| --- 5s of no speech ---",
                "10|95| And so the story continues for a while.",
                "11|150| Our program was produced by a team of people.",
                "12|170| --- 15s of no speech (end of audio at 185.00) ---",
            ],
        )
        self.assertEqual(rendered.line_times[0], (0.0, 1.5))
        self.assertEqual(rendered.line_times[6], (50.0, 60.0))
        self.assertEqual(rendered.line_times[8], (75.4, 90.0))
        self.assertEqual(rendered.line_times[12], (170.0, 185.0))
        self.assertEqual(sorted(rendered.line_times), list(range(13)))

    def test_without_silence_only_sentences_are_numbered(self) -> None:
        req = episode()
        rendered = render_transcript(req.sentences, req.silences, fmt="index", include_silence=False)
        lines = rendered.text.splitlines()
        self.assertEqual(len(lines), len(req.sentences))
        self.assertEqual(lines[0], "0|1| From WBEZ Chicago, it's This American Life.")
        self.assertEqual(lines[-1], "7|150| Our program was produced by a team of people.")
        self.assertEqual(rendered.line_times[7], (150.2, 170.0))
        self.assertFalse(rendered.include_silence)

    def test_tail_line_names_the_endpoint_or_its_own_end(self) -> None:
        sents = sentences((0.2, 5.0, "Hello."))
        tail = [silence(5.0, 31.4, "tail")]
        with_duration = render_transcript(sents, tail, fmt="index", include_silence=True,
                                          episode_duration=31.4).text.splitlines()[-1]
        without = render_transcript(sents, tail, fmt="index", include_silence=True).text.splitlines()[-1]
        self.assertEqual(with_duration, "1|5| --- 26s of no speech (end of audio at 31.40) ---")
        self.assertEqual(without, with_duration)

    def test_cited_lines_resolve_to_sentence_and_silence_bounds(self) -> None:
        req = episode()
        rendered = render_transcript(req.sentences, req.silences, fmt="index", episode_duration=185.0)
        # The ad cites the music break before it (line 6) through its last sentence (line 8);
        # the outro ends on the tail silence line. Echoed seconds are what the model saw.
        cited = [
            segment(50.0, 91.0, "ad", start_line=6, end_line=8),
            segment(150.0, 185.0, "outro", start_line=11, end_line=12),
        ]
        resolved, deltas, unresolved = resolve_lines(cited, rendered.line_times)
        self.assertEqual([(s.start_seconds, s.end_seconds) for s in resolved], [(50.0, 90.0), (150.2, 185.0)])
        self.assertEqual(deltas, [(0.0, 1.0), (0.2, 0.0)])
        self.assertEqual(unresolved, 0)

    def test_unknown_lines_fall_back_to_echoed_seconds(self) -> None:
        rendered = render_transcript(sentences((0.0, 4.0), (4.5, 9.0)), [], fmt="index")
        resolved, deltas, unresolved = resolve_lines([segment(0.0, 8.5, start_line=0, end_line=99)], rendered.line_times)
        self.assertEqual((resolved[0].start_seconds, resolved[0].end_seconds), (0.0, 8.5))
        self.assertEqual(deltas, [(0.0, None)])
        self.assertEqual(unresolved, 1)

    def test_multiline_text_stays_on_one_line(self) -> None:
        rendered = render_transcript([sentence(0, 1.0, 2.0, "two\nlines")], [], fmt="index")
        self.assertEqual(rendered.text, "0|1| two lines")

    def test_rejects_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            render_transcript([], [], fmt="srt")


class RepeatCollapseTests(unittest.TestCase):
    def test_flagged_sentence_is_collapsed_at_render_time(self) -> None:
        looped = sentence(0, 10.0, 28.0, "no no no no no no no no no", flags=("repetitive",))
        plain = sentence(1, 30.0, 32.0, "no no no no no no")
        rendered = render_transcript([looped, plain], [], fmt="index")
        self.assertEqual(rendered.text, "0|10| no no no [repeated ×9]\n1|30| no no no no no no")
        self.assertEqual(looped.text, "no no no no no no no no no")

    def test_ngram_runs_ignore_case_and_punctuation(self) -> None:
        text = "So he said thank you. Thank you, thank you. Thank you! Thank you. Then he left."
        self.assertEqual(collapse_repeats(text), "So he said thank you. Thank you, thank you. [repeated ×5] Then he left.")

    def test_a_short_collapsible_run_is_not_hidden_by_a_longer_pattern(self) -> None:
        # "x x x x y" repeats only three times, but "x" alone repeats four.
        self.assertEqual(collapse_repeats("x x x x y x x x x y x x x x y"),
                         "x x x [repeated ×4] y x x x [repeated ×4] y x x x [repeated ×4] y")

    def test_runs_of_three_or_fewer_are_kept(self) -> None:
        self.assertEqual(collapse_repeats("very very very good"), "very very very good")


if __name__ == "__main__":
    unittest.main()
