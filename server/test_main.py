import math
import unittest

from main import (
    SegmentResponse,
    TranscriptSegment,
    episode_duration_guidance,
    sanitize_segments,
    valid_episode_endpoint,
)


class EpisodeDurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transcript = [
            TranscriptSegment(0.0, 10.0, "Opening"),
            TranscriptSegment(80.0, 90.0, "Thanks for listening"),
        ]

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
        segments = [SegmentResponse(
            startSeconds=80.0,
            endSeconds=90.0,
            summary="Farewell and closing music",
            kind="outro",
        )]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=100.0)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].startSeconds, 80.0)
        self.assertEqual(cleaned[0].endSeconds, 100.0)

    def test_ads_and_intros_remain_bounded_by_transcript(self) -> None:
        segments = [
            SegmentResponse(startSeconds=0.0, endSeconds=95.0, summary="Opening", kind="intro"),
            SegmentResponse(startSeconds=50.0, endSeconds=100.0, summary="Sponsor", kind="ad"),
        ]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=120.0)

        self.assertEqual([segment.endSeconds for segment in cleaned], [90.0, 90.0])

    def test_outro_falls_back_to_transcript_end_without_valid_duration(self) -> None:
        segments = [SegmentResponse(
            startSeconds=80.0,
            endSeconds=120.0,
            summary="Farewell",
            kind="outro",
        )]

        cleaned = sanitize_segments(segments, self.transcript, episode_duration=89.0)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].endSeconds, 90.0)


if __name__ == "__main__":
    unittest.main()
