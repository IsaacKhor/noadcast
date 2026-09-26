"""Experiment B metrics on hand-built interval sets with known answers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import score_ad_eval as score  # noqa: E402


def seg(start: float, end: float, kind: str = "ad", summary: str = "") -> dict:
    return {"start_seconds": start, "end_seconds": end, "kind": kind, "summary": summary,
            "start_line": None, "end_line": None}


class PercentileTest(unittest.TestCase):
    def test_linear_interpolation_between_order_statistics(self) -> None:
        values = [4.0, 1.0, 3.0, 2.0]
        self.assertEqual(score.percentile(values, 0.5), 2.5)
        self.assertAlmostEqual(score.percentile(values, 0.1), 1.3)
        self.assertAlmostEqual(score.percentile(values, 0.9), 3.7)
        self.assertEqual(score.percentile(values, 0.0), 1.0)
        self.assertEqual(score.percentile(values, 1.0), 4.0)

    def test_single_value_and_empty(self) -> None:
        self.assertEqual(score.percentile([7.0], 0.1), 7.0)
        self.assertIsNone(score.percentile([], 0.5))


class FrameTest(unittest.TestCase):
    def test_midpoint_rule_on_half_open_segments(self) -> None:
        self.assertEqual(score.frames([seg(0.4, 2.6)], 10), {0, 1, 2})
        self.assertEqual(score.frames([seg(0.5, 2.5)], 10), {0, 1})  # 2.5 is excluded, 0.5 included
        self.assertEqual(score.frames([seg(0.6, 1.4)], 10), set())  # covers no midpoint
        self.assertEqual(score.frames([seg(8.0, 30.0)], 10), {8, 9})  # clipped to the grid

    def test_kind_filter(self) -> None:
        segments = [seg(0, 2, "intro"), seg(5, 7, "ad")]
        self.assertEqual(score.frames(segments, 10, "intro"), {0, 1})
        self.assertEqual(score.frames(segments, 10, "ad"), {5, 6})
        self.assertEqual(score.frames(segments, 10), {0, 1, 5, 6})

    def test_frame_count_rounds_partial_seconds_up(self) -> None:
        self.assertEqual(score.frame_count(20.0), 20)
        self.assertEqual(score.frame_count(3918.94), 3919)


class JaccardTest(unittest.TestCase):
    def test_overlap(self) -> None:
        a, b = score.frames([seg(0, 10)], 20), score.frames([seg(5, 15)], 20)
        self.assertAlmostEqual(score.jaccard(a, b), 5 / 15)

    def test_agreeing_on_absence_is_agreement(self) -> None:
        self.assertEqual(score.jaccard(set(), set()), 1.0)
        self.assertEqual(score.jaccard({1, 2}, set()), 0.0)

    def test_per_kind_and_overall(self) -> None:
        a = [seg(0, 30, "intro"), seg(100, 160, "ad")]
        b = [seg(0, 30, "intro"), seg(130, 160, "ad")]
        result = score.compare(a, b, 200.0)["frame_jaccard"]
        self.assertAlmostEqual(result["any"], 60 / 90)
        self.assertEqual(result["intro"], 1.0)
        self.assertAlmostEqual(result["ad"], 30 / 60)
        self.assertEqual(result["outro"], 1.0)


class MatchTest(unittest.TestCase):
    def test_iou(self) -> None:
        self.assertAlmostEqual(score.iou(seg(0, 10), seg(1, 11)), 9 / 11)
        self.assertEqual(score.iou(seg(0, 10), seg(20, 30)), 0.0)
        self.assertEqual(score.iou(seg(5, 5), seg(5, 5)), 0.0)

    def test_matching_is_kind_aware_and_one_to_one(self) -> None:
        a = [seg(0, 10), seg(20, 30)]
        b = [seg(1, 11), seg(100, 110), seg(20, 30, "intro")]
        matches, unmatched_a, unmatched_b = score.match_segments(a, b)
        self.assertEqual([(i, j) for i, j, _ in matches], [(0, 0)])
        self.assertAlmostEqual(matches[0][2], 9 / 11)
        self.assertEqual((unmatched_a, unmatched_b), (1, 2))

    def test_threshold_excludes_slivers(self) -> None:
        matches, unmatched_a, unmatched_b = score.match_segments([seg(0, 10)], [seg(9, 100)])
        self.assertEqual(matches, [])
        self.assertEqual((unmatched_a, unmatched_b), (1, 1))

    def test_greedy_takes_the_best_pair_first(self) -> None:
        matches, _, unmatched_b = score.match_segments([seg(0, 10)], [seg(0, 9), seg(0, 10)])
        self.assertEqual([(i, j, value) for i, j, value in matches], [(0, 1, 1.0)])
        self.assertEqual(unmatched_b, 1)

    def test_boundary_deltas_and_counts(self) -> None:
        a = [seg(0, 30, "intro"), seg(100, 160), seg(170, 175)]
        b = [seg(0, 32, "intro"), seg(97, 161)]
        result = score.compare(a, b, 200.0)
        self.assertEqual(sorted(result["delta_start"]), [0.0, 3.0])
        self.assertEqual(sorted(result["delta_end"]), [1.0, 2.0])
        self.assertEqual(result["counts_a"], {"ad": 2, "intro": 1, "outro": 0})
        self.assertEqual(result["counts_b"], {"ad": 1, "intro": 1, "outro": 0})
        self.assertEqual(result["count_diff"], 1)
        self.assertEqual((result["unmatched_a"], result["unmatched_b"]), (1, 0))


class SummaryAndDecisionTest(unittest.TestCase):
    def rows(self, pairs: list[tuple[list[dict], list[dict]]]) -> list[dict]:
        return [score.compare(a, b, 100.0) for a, b in pairs]

    def test_summary_statistics(self) -> None:
        rows = self.rows([
            ([seg(0, 10)], [seg(0, 10)]),  # J 1.0, delta 0
            ([seg(0, 10)], [seg(2, 10)]),  # J 0.8, delta 2
            ([seg(0, 10)], [seg(0, 10), seg(50, 60)]),  # J 0.5, delta 0, one extra
        ])
        summary = score.summarize(rows)
        self.assertEqual(summary["frame_jaccard"]["any"]["median"], 0.8)
        self.assertAlmostEqual(summary["frame_jaccard"]["any"]["mean"], (1.0 + 0.8 + 0.5) / 3)
        self.assertEqual(summary["matched"], 3)
        self.assertEqual((summary["delta_start"]["median"], summary["delta_start"]["max"]), (0.0, 2.0))
        self.assertAlmostEqual(summary["delta_start"]["p90"], 1.6)
        self.assertEqual((summary["unmatched_a"], summary["unmatched_b"]), (0, 1))
        self.assertAlmostEqual(summary["mean_count_diff"], 1 / 3)
        self.assertAlmostEqual(summary["count_agreement"]["ad"], 2 / 3)
        self.assertEqual(summary["count_agreement"]["intro"], 1.0)

    def test_suffices_within_margins(self) -> None:
        floor = score.summarize(self.rows([([seg(0, 20)], [seg(0, 20)])] * 2))
        variant = score.summarize(self.rows([([seg(0, 25)], [seg(1, 25)])] * 2))  # J 0.96, delta 1
        decision = score.decide(floor, variant)
        self.assertAlmostEqual(decision["jaccard_gap"], 0.04)
        self.assertEqual(decision["variant_median_abs_delta_start"], 1.0)
        self.assertEqual(decision["verdict"], "suffices")

    def test_boundary_margin_fails_even_with_a_small_jaccard_gap(self) -> None:
        floor = score.summarize(self.rows([([seg(0, 100)], [seg(0, 100)])] * 2))
        variant = score.summarize(self.rows([([seg(0, 100)], [seg(3, 100)])] * 2))  # J 0.97, delta 3
        decision = score.decide(floor, variant)
        self.assertTrue(decision["jaccard_within_margin"])
        self.assertFalse(decision["delta_start_within_margin"])
        self.assertEqual(decision["verdict"], "inconclusive")

    def test_large_gap_requires_review(self) -> None:
        floor = score.summarize(self.rows([([seg(0, 20), seg(40, 60)], [seg(0, 20), seg(40, 60)])] * 2))
        variant = score.summarize(self.rows([([seg(0, 20)], [seg(0, 20), seg(40, 60)])] * 2))  # J 0.5
        decision = score.decide(floor, variant)
        self.assertAlmostEqual(decision["jaccard_gap"], 0.5)
        self.assertEqual(decision["verdict"], "review")

    def test_count_limit(self) -> None:
        floor = score.summarize(self.rows([([seg(0, 90)], [seg(0, 90)])] * 2))
        busy = [seg(0, 90), seg(91, 91.4), seg(92, 92.4)]  # two extra sub-frame ads: J unchanged
        variant = score.summarize(self.rows([([seg(0, 90)], busy)] * 2))
        decision = score.decide(floor, variant)
        self.assertEqual(decision["variant_frame_jaccard"], 1.0)
        self.assertFalse(decision["count_diff_within_limit"])
        self.assertEqual(decision["verdict"], "inconclusive")

    def test_nothing_matched_anywhere_is_vacuous(self) -> None:
        floor = score.summarize(self.rows([([], [])]))
        decision = score.decide(floor, score.summarize(self.rows([([], [])])))
        self.assertIsNone(decision["self_median_abs_delta_start"])
        self.assertEqual(decision["verdict"], "suffices")


class DisagreementRunTest(unittest.TestCase):
    def test_runs_split_on_direction_and_weight_by_vote_margin(self) -> None:
        runs = score.disagreement_runs([0, 3, 3, 1, 0, 2], [0, 0, 0, 3, 0, 1], 3)
        self.assertEqual([(run["sign"], run["start"], run["end"]) for run in runs], [(1, 1, 3), (-1, 3, 4), (1, 5, 6)])
        self.assertAlmostEqual(runs[0]["score"], 2.0)
        self.assertAlmostEqual(runs[1]["score"], 2 / 3)
        self.assertAlmostEqual(runs[2]["score"], 1 / 3)
        self.assertEqual((runs[0]["candidate_votes"], runs[0]["reference_votes"]), (6, 0))


if __name__ == "__main__":
    unittest.main()
