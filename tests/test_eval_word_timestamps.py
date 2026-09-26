"""Experiment A decision rule and run-log verification on synthetic inputs."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import report_word_timestamps as report  # noqa: E402
from report_tal_comparison import VerificationError  # noqa: E402


def runs(walls: tuple, pss: tuple, cpu: tuple | None = None) -> list[dict]:
    cpu = cpu or tuple(wall * 17 for wall in walls)
    return [{"position": position, "suite_wall_seconds": wall, "sum_episode_cpu_seconds": seconds,
             "sum_worker_transcribe_seconds": wall * 9, "median_episode_peak_rss_mib": 1300.0,
             "memory": {"suite": {"peak_pss_mib": peak}}}
            for position, wall, peak, seconds in zip(range(1, 5), walls, pss, cpu)]


def episodes(text_mismatch: tuple = (), count_mismatch: tuple = ()) -> list[dict]:
    return [{"index": index, "text_identical": index not in text_mismatch,
             "segment_count_identical": index not in count_mismatch, "word_count": 1000} for index in range(1, 11)]


def failed(comparison: dict) -> list[str]:
    return [item["criterion"] for item in comparison["criteria"] if not item["passed"]]


class DecisionTest(unittest.TestCase):
    def test_accept(self) -> None:
        comparison = report.make_comparison(runs((100, 110, 112, 100), (10000, 10100, 10200, 10000)), episodes())
        self.assertAlmostEqual(comparison["pooled_on_over_off"]["wall_ratio"], 1.11)
        self.assertAlmostEqual(comparison["pairs"]["run_3_on_over_run_4_off"]["wall_ratio"], 1.12)
        self.assertAlmostEqual(comparison["largest_peak_pss_increase_mib"], 200.0)
        self.assertEqual(comparison["total_words_on"], 10000)
        self.assertEqual(comparison["decision"], "ACCEPT")

    def test_pooled_wall_above_threshold_rejects_even_when_each_pair_passes(self) -> None:
        comparison = report.make_comparison(runs((100, 125, 124, 100), (10000,) * 4), episodes())
        self.assertEqual(failed(comparison), ["pooled ON/OFF suite wall ratio"])
        self.assertEqual(comparison["decision"], "REJECT")

    def test_one_slow_pair_rejects(self) -> None:
        comparison = report.make_comparison(runs((100, 130, 100, 100), (10000,) * 4), episodes())
        self.assertIn("each pairwise ON/OFF suite wall ratio", failed(comparison))
        self.assertEqual(comparison["decision"], "REJECT")

    def test_cpu_ratio_rejects(self) -> None:
        comparison = report.make_comparison(runs((100, 110, 110, 100), (10000,) * 4, cpu=(100, 130, 130, 100)),
                                            episodes())
        self.assertEqual(failed(comparison), ["pooled ON/OFF summed episode CPU ratio"])

    def test_a_single_pair_memory_increase_is_not_averaged_away(self) -> None:
        comparison = report.make_comparison(runs((100, 110, 110, 100), (11000, 10000, 11100, 10000)), episodes())
        self.assertAlmostEqual(comparison["pooled_on_over_off"]["suite_peak_pss_change_mib"], 50.0)
        self.assertAlmostEqual(comparison["largest_peak_pss_increase_mib"], 1100.0)
        self.assertEqual(failed(comparison), ["largest aggregate suite peak PSS increase (pairwise or pooled)"])

    def test_text_or_segment_count_differences_reject_and_name_the_episodes(self) -> None:
        comparison = report.make_comparison(runs((100, 110, 110, 100), (10000,) * 4),
                                            episodes(text_mismatch=(3,), count_mismatch=(3, 7)))
        text, counts = comparison["criteria"][4], comparison["criteria"][5]
        self.assertEqual((text["measured"], text["mismatched_episodes"]), (9, [3]))
        self.assertEqual((counts["measured"], counts["mismatched_episodes"]), (8, [3, 7]))
        self.assertEqual(comparison["decision"], "REJECT")

    def test_measured_values_render_per_criterion(self) -> None:
        comparison = report.make_comparison(runs((100, 110, 112, 100), (10000, 10100, 10200, 10000)), episodes())
        rendered = [report.measured_text(item) for item in comparison["criteria"]]
        self.assertEqual(rendered, ["1.110×", "1.100×, 1.120×", "1.110×", "+200.0 MiB", "10 of 10", "10 of 10"])


class LogTest(unittest.TestCase):
    summary = {"episode_count": 10, "suite_wall_seconds": 141.4, "load_end": [15.3, 7.7, 3.75]}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "tal").mkdir()
        for name, value in (("ROOT", root), ("TAL_ROOT", root / "tal")):
            patcher = mock.patch.object(report, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = root / "tal" / "run-x.log"

    def write(self, *, run: str = "run-x", end: bool = True, summary: dict | None = None) -> None:
        lines = [f"[2026-09-22T20:16:23Z] start {run} loadavg=1.07 1.14 1.27", "Worker 0 ready: load 0.20s",
                 json.dumps(summary or self.summary, indent=2), "Results: /data/runs/run-x/results.json"]
        if end:
            lines.append("[2026-09-22T20:18:47Z] end run-x loadavg=15.31 7.71 3.75")
        self.log.write_text("\n".join(lines) + "\n")

    def test_brackets_and_logged_summary(self) -> None:
        self.write()
        checked = report.validate_log("run-x", {"summary": self.summary})
        self.assertEqual(checked["start"], {"at": "2026-09-22T20:16:23Z", "loadavg": [1.07, 1.14, 1.27]})
        self.assertEqual(checked["end"]["loadavg"], [15.31, 7.71, 3.75])
        self.assertIs(checked["logged_summary_matches_results"], True)

    def test_logged_summary_must_equal_results(self) -> None:
        self.write(summary={**self.summary, "suite_wall_seconds": 140.0})
        with self.assertRaisesRegex(VerificationError, "logged summary differs"):
            report.validate_log("run-x", {"summary": self.summary})

    def test_missing_end_line(self) -> None:
        self.write(end=False)
        with self.assertRaisesRegex(VerificationError, "lacks start/end"):
            report.validate_log("run-x", {"summary": self.summary})

    def test_log_of_another_run(self) -> None:
        self.write(run="run-y")
        with self.assertRaisesRegex(VerificationError, "names another run"):
            report.validate_log("run-x", {"summary": self.summary})


if __name__ == "__main__":
    unittest.main()
