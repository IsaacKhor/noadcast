from __future__ import annotations

import math
import unittest

from noadcast.classify.prompts import episode_duration_guidance, valid_episode_endpoint
from noadcast.classify.sanitize import finalize, merge_overlapping, sanitize_segments, snap_to_silence
from tests.test_classify_helpers import episode, request, segment, sentences, silence


class EpisodeDurationTests(unittest.TestCase):
    """The recovered server's tests (3e6d713:server/test_main.py), on the new types."""

    def setUp(self) -> None:
        self.transcript = sentences((0.0, 10.0, "Opening"), (80.0, 90.0, "Thanks for listening"))

    def test_duration_guidance_names_the_audio_endpoint(self) -> None:
        guidance = episode_duration_guidance(100.0, self.transcript)

        self.assertIn("100.00 seconds", guidance)
        self.assertIn("outro", guidance)
        self.assertIn("endSeconds", guidance)

    def test_invalid_or_too_short_episode_endpoints_are_rejected(self) -> None:
        for duration in (None, -1.0, math.nan, math.inf, 89.0):
            with self.subTest(duration=duration):
                self.assertIsNone(valid_episode_endpoint(duration, self.transcript))
                guidance = episode_duration_guidance(duration, self.transcript)
                self.assertIn("duration is unknown", guidance)
                self.assertIn("final transcript timestamp", guidance)

    def test_outro_is_extended_to_valid_episode_endpoint(self) -> None:
        segments = [segment(80.0, 90.0, "outro", "Farewell and closing music")]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=100.0)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].start_seconds, 80.0)
        self.assertEqual(cleaned[0].end_seconds, 100.0)

    def test_ads_and_intros_are_bounded_by_the_episode_endpoint(self) -> None:
        # Changed from the recovered test, which bounded these ends by the
        # transcript end (90.0): ends now clamp to the audio endpoint, since an
        # ad can run into music that has no words.
        segments = [
            segment(0.0, 95.0, "intro", "Opening"),
            segment(50.0, 100.0, "ad", "Sponsor"),
            segment(60.0, 130.0, "ad", "Past the end"),
        ]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=120.0)

        self.assertEqual([s.end_seconds for s in cleaned], [95.0, 100.0, 120.0])

    def test_ads_and_intros_remain_bounded_by_transcript_without_valid_duration(self) -> None:
        segments = [segment(0.0, 95.0, "intro", "Opening"), segment(50.0, 100.0, "ad", "Sponsor")]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=None)

        self.assertEqual([s.end_seconds for s in cleaned], [90.0, 90.0])

    def test_outro_falls_back_to_transcript_end_without_valid_duration(self) -> None:
        segments = [segment(80.0, 120.0, "outro", "Farewell")]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=89.0)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].end_seconds, 90.0)


class IntroStartTests(unittest.TestCase):
    def test_intro_starts_at_zero_when_the_first_word_is_late(self) -> None:
        # Music-heavy show: the first word is 25.3 s in. The recovered sanitiser
        # clamped the intro to 25.3, so the opening music was never skipped.
        transcript = sentences((25.3, 31.0, "Welcome to the show."), (31.5, 60.0), (61.0, 300.0))
        for echoed in (0.0, 12.0, 25.0, 25.3, 26.2):
            with self.subTest(echoed=echoed):
                cleaned = sanitize_segments([segment(echoed, 60.0, "intro")], transcript, 300.0)
                self.assertEqual(cleaned[0].start_seconds, 0.0)

    def test_intro_at_the_first_word_starts_at_zero(self) -> None:
        transcript = sentences((0.69, 5.0), (5.5, 40.0), (41.0, 100.0))
        cleaned = sanitize_segments([segment(0.69, 40.0, "intro")], transcript, 100.0)
        self.assertEqual(cleaned[0].start_seconds, 0.0)

    def test_intro_after_a_cold_open_keeps_its_start(self) -> None:
        transcript = sentences((1.0, 40.0, "Cold open story."), (45.0, 75.0, "Theme and billboard."), (80.0, 400.0))
        cleaned = sanitize_segments([segment(45.0, 75.0, "intro")], transcript, 400.0)
        self.assertEqual(cleaned[0].start_seconds, 45.0)

    def test_ads_still_clamp_to_the_first_word(self) -> None:
        transcript = sentences((25.3, 31.0), (31.5, 300.0))
        cleaned = sanitize_segments([segment(0.0, 31.0, "ad")], transcript, 300.0)
        self.assertEqual(cleaned[0].start_seconds, 25.3)


class OutroExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transcript = sentences(
            (0.0, 3800.0, "The episode."),
            (3801.0, 3860.0, "Credits and thanks."),
            (3862.0, 3890.0, "More credits."),
            (3900.0, 3902.5, "Bye."),
        )

    def test_extends_over_silence_with_little_speech(self) -> None:
        # 3890 → 3918.9 holds only "Bye." (2.5 s) between music.
        cleaned = sanitize_segments([segment(3801.0, 3890.0, "outro")], self.transcript, 3918.9)
        self.assertEqual(cleaned[0].end_seconds, 3918.9)

    def test_keeps_its_end_when_speech_follows(self) -> None:
        # 3860 → 3918.9 still holds 28 s of credits: the model ended the outro early on purpose.
        cleaned = sanitize_segments([segment(3801.0, 3860.0, "outro")], self.transcript, 3918.9)
        self.assertEqual(cleaned[0].end_seconds, 3860.0)

    def test_keeps_its_end_when_the_gap_is_implausibly_long(self) -> None:
        # A declared duration 400 s past the last word is more likely wrong than silent.
        transcript = sentences((0.0, 2950.0), (2951.0, 3000.0, "Goodbye."))
        cleaned = sanitize_segments([segment(2951.0, 3000.0, "outro")], transcript, 3400.0)
        self.assertEqual(cleaned[0].end_seconds, 3000.0)

    def test_an_outro_ending_past_the_audio_is_clamped(self) -> None:
        cleaned = sanitize_segments([segment(3801.0, 4200.0, "outro")], self.transcript, 3918.9)
        self.assertEqual(cleaned[0].end_seconds, 3918.9)


class DropAndMergeTests(unittest.TestCase):
    def test_drops_unknown_kinds_non_finite_and_inverted_and_sorts(self) -> None:
        transcript = sentences((0.0, 50.0), (50.5, 100.0))
        cleaned = sanitize_segments(
            [
                segment(70.0, 80.0, "ad", "Second"),
                segment(10.0, 20.0, "sponsor"),
                segment(math.nan, 30.0, "ad"),
                segment(30.0, math.inf, "ad"),
                segment(60.0, 55.0, "ad"),
                segment(30.0, 40.0, "ad", "First"),
            ],
            transcript,
            100.0,
        )
        self.assertEqual([s.summary for s in cleaned], ["First", "Second"])

    def test_merges_overlapping_and_touching_same_kind_segments(self) -> None:
        merged = merge_overlapping(
            [
                segment(100.0, 160.0, "ad", "Mattress", end_line=9),
                segment(150.0, 200.0, "ad", "VPN", end_line=12),
                segment(200.0, 230.0, "ad", "Meal kit", end_line=14),
                segment(120.0, 130.0, "intro", "Overlaps an ad but is not one"),
                segment(300.0, 330.0, "ad", "Separate"),
            ]
        )
        self.assertEqual(
            [(s.kind, s.start_seconds, s.end_seconds, s.summary) for s in merged],
            [
                ("ad", 100.0, 230.0, "Mattress; VPN; Meal kit"),
                ("intro", 120.0, 130.0, "Overlaps an ad but is not one"),
                ("ad", 300.0, 330.0, "Separate"),
            ],
        )
        self.assertEqual(merged[0].end_line, 14)


class SnapToSilenceTests(unittest.TestCase):
    def test_moves_to_the_midpoint_of_a_nearby_short_silence(self) -> None:
        regions = [silence(100.0, 103.0)]
        self.assertEqual(snap_to_silence(100.2, regions), 101.5)
        self.assertEqual(snap_to_silence(99.5, regions), 101.5)
        self.assertEqual(snap_to_silence(99.0, regions), 101.0)  # moves at most 2 s
        self.assertEqual(snap_to_silence(104.5, regions), 102.5)

    def test_leaves_the_boundary_alone_without_silence_in_reach(self) -> None:
        self.assertEqual(snap_to_silence(95.0, [silence(100.0, 103.0)]), 95.0)
        self.assertEqual(snap_to_silence(95.0, []), 95.0)

    def test_moves_at_most_window_seconds_into_a_long_silence(self) -> None:
        bed = [silence(200.0, 260.0)]
        self.assertEqual(snap_to_silence(200.0, bed), 202.0)
        self.assertEqual(snap_to_silence(260.0, bed), 258.0)
        self.assertEqual(snap_to_silence(198.5, bed), 200.5)

    def test_picks_the_nearest_silence(self) -> None:
        regions = [silence(96.0, 99.0), silence(100.5, 104.0)]
        self.assertEqual(snap_to_silence(100.0, regions), 102.0)

    def test_respects_a_custom_window(self) -> None:
        self.assertEqual(snap_to_silence(99.0, [silence(100.0, 103.0)], window=0.5), 99.0)


class FinalizeTests(unittest.TestCase):
    def test_snaps_every_boundary_but_the_outro_end(self) -> None:
        req = episode()
        final = finalize(
            [
                segment(0.0, 14.0, "intro", "Billboard"),
                segment(60.0, 90.0, "ad", "Mattress"),
                segment(150.2, 185.0, "outro", "Credits"),
            ],
            req,
        )
        self.assertEqual(
            [(s.kind, s.start_seconds, s.end_seconds) for s in final],
            [("intro", 0.0, 16.0), ("ad", 58.0, 92.0), ("outro", 150.2, 185.0)],
        )

    def test_outro_start_snaps_into_a_silence(self) -> None:
        req = episode()
        final = finalize([segment(170.0, 185.0, "outro")], req)
        self.assertEqual((final[0].start_seconds, final[0].end_seconds), (172.0, 185.0))

    def test_snap_is_optional(self) -> None:
        req = episode()
        final = finalize([segment(60.0, 90.0, "ad")], req, snap=False)
        self.assertEqual((final[0].start_seconds, final[0].end_seconds), (60.0, 90.0))

    def test_boundaries_on_the_audio_edges_do_not_move(self) -> None:
        req = episode()
        final = finalize([segment(95.0, 185.0, "ad", "Runs to the end")], req)
        self.assertEqual((final[0].start_seconds, final[0].end_seconds), (93.0, 185.0))

    def test_merges_segments_that_snapping_joins(self) -> None:
        req = request(
            sentences((0.0, 50.0), (51.0, 80.0), (81.5, 110.0), (111.0, 200.0)),
            [silence(80.0, 81.5)],
            duration=200.0,
        )
        final = finalize([segment(51.0, 80.0, "ad", "One"), segment(81.5, 110.0, "ad", "Two")], req)
        self.assertEqual([(s.start_seconds, s.end_seconds, s.summary) for s in final], [(51.0, 110.0, "One; Two")])

    def test_empty_transcript_yields_nothing(self) -> None:
        self.assertEqual(finalize([segment(0.0, 10.0, "intro")], request([])), [])


if __name__ == "__main__":
    unittest.main()
