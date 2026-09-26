#!/usr/bin/env python3
"""Score the transcript-variant ad-segment eval by agreement against the LLM's own noise floor.

No human labels exist, so every number is agreement between two sets of final skip segments:

- ``self``: repeat 1 vs repeat 2 on the same transcript variant, the LLM noise floor;
- ``cross_seed``: repeat 1 vs repeat 3, a check that the floor itself is stable;
- ``variant``: candidate (tiny.en) repeat 1 vs reference (small.en) repeat 1.

Metrics: frame Jaccard on a 1 s grid (overall and per kind; the headline, listener-impact metric),
greedy same-kind matched-segment IoU (threshold 0.1), |Δstart| and |Δend| over matched pairs, and
per-kind count agreement. The pre-registered rule: the candidate suffices if its median frame
Jaccard is within 0.05 of the pooled self floor, its median |Δstart| is within 2 s of the floor's,
and it differs by at most one segment per episode on average. Escalation to the reference model is
considered only when the Jaccard gap exceeds 0.15 and a manual read of the worst-disagreeing spans,
printed below with both transcripts' text, finds a lost ad-marker phrase.

Refuses to score if any pinned transcript, prompt, or cassette no longer matches.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from noadcast.classify.base import SEGMENT_KINDS

from eval_ad_segments import (
    EVALS_ROOT, EVAL_ID, SCHEMA_VERSION, Cell, cell_key, cells, load_transcripts, verify_pins,
)
from report_tal_comparison import atomic_json, load_json, require, sha256_file


FRAME_SECONDS = 1.0
IOU_THRESHOLD = 0.1
JACCARD_MARGIN = 0.05
DELTA_START_MARGIN_SECONDS = 2.0
MAX_MEAN_COUNT_DIFF = 1.0
REVIEW_GAP = 0.15
WORST_SPANS = 10
KINDS = ("any", *SEGMENT_KINDS)
THRESHOLDS = {
    "frame_seconds": FRAME_SECONDS, "iou_threshold": IOU_THRESHOLD, "jaccard_margin": JACCARD_MARGIN,
    "delta_start_margin_seconds": DELTA_START_MARGIN_SECONDS, "max_mean_count_diff": MAX_MEAN_COUNT_DIFF,
    "review_gap": REVIEW_GAP, "worst_spans": WORST_SPANS,
}


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between order statistics (numpy's default); None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def frame_count(duration: float) -> int:
    return math.ceil(duration / FRAME_SECONDS)


def frames(segments: list[dict], count: int, kind: str = "any") -> set[int]:
    """Grid frames whose midpoint lies in a segment's half-open [start, end)."""
    marked: set[int] = set()
    for segment in segments:
        if kind == "any" or segment["kind"] == kind:
            first = max(0, math.ceil(segment["start_seconds"] / FRAME_SECONDS - 0.5))
            marked.update(range(first, min(count, math.ceil(segment["end_seconds"] / FRAME_SECONDS - 0.5))))
    return marked


def jaccard(a: set[int], b: set[int]) -> float:
    """1.0 when both sides skip nothing: agreeing on absence is agreement."""
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def iou(a: dict, b: dict) -> float:
    overlap = min(a["end_seconds"], b["end_seconds"]) - max(a["start_seconds"], b["start_seconds"])
    union = (a["end_seconds"] - a["start_seconds"]) + (b["end_seconds"] - b["start_seconds"]) - max(0.0, overlap)
    return max(0.0, overlap) / union if union > 0 else 0.0


def match_segments(a: list[dict], b: list[dict]) -> tuple[list[tuple[int, int, float]], int, int]:
    """Greedy one-to-one matching of same-kind segments by descending IoU, keeping IoU >= threshold."""
    candidates = sorted(((iou(x, y), i, j) for i, x in enumerate(a) for j, y in enumerate(b)
                         if x["kind"] == y["kind"]), key=lambda item: (-item[0], item[1], item[2]))
    used_a: set[int] = set()
    used_b: set[int] = set()
    matches = []
    for value, i, j in candidates:
        if value < IOU_THRESHOLD:
            break
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            matches.append((i, j, value))
    return matches, len(a) - len(matches), len(b) - len(matches)


def compare(a: list[dict], b: list[dict], duration: float) -> dict:
    count = frame_count(duration)
    matches, unmatched_a, unmatched_b = match_segments(a, b)
    counts_a = {kind: sum(segment["kind"] == kind for segment in a) for kind in SEGMENT_KINDS}
    counts_b = {kind: sum(segment["kind"] == kind for segment in b) for kind in SEGMENT_KINDS}
    return {
        "frame_jaccard": {kind: jaccard(frames(a, count, kind), frames(b, count, kind)) for kind in KINDS},
        "matched_iou": [value for _, _, value in matches],
        "delta_start": [abs(a[i]["start_seconds"] - b[j]["start_seconds"]) for i, j, _ in matches],
        "delta_end": [abs(a[i]["end_seconds"] - b[j]["end_seconds"]) for i, j, _ in matches],
        "unmatched_a": unmatched_a, "unmatched_b": unmatched_b, "counts_a": counts_a, "counts_b": counts_b,
        "count_diff": sum(abs(counts_a[kind] - counts_b[kind]) for kind in SEGMENT_KINDS),
    }


def spread(values: list[float]) -> dict:
    return {"median": percentile(values, 0.5), "p90": percentile(values, 0.9), "max": max(values, default=None)}


def summarize(rows: list[dict]) -> dict:
    ious = [value for row in rows for value in row["matched_iou"]]
    return {
        "episodes": len(rows),
        "frame_jaccard": {kind: {"median": percentile([row["frame_jaccard"][kind] for row in rows], 0.5),
                                 "mean": statistics.fmean(row["frame_jaccard"][kind] for row in rows)}
                          for kind in KINDS},
        "matched": len(ious), "matched_iou_median": percentile(ious, 0.5), "matched_iou_p10": percentile(ious, 0.1),
        "unmatched_a": sum(row["unmatched_a"] for row in rows), "unmatched_b": sum(row["unmatched_b"] for row in rows),
        "delta_start": spread([value for row in rows for value in row["delta_start"]]),
        "delta_end": spread([value for row in rows for value in row["delta_end"]]),
        "mean_count_diff": statistics.fmean(row["count_diff"] for row in rows),
        "count_agreement": {kind: statistics.fmean(row["counts_a"][kind] == row["counts_b"][kind] for row in rows)
                            for kind in SEGMENT_KINDS},
    }


def decide(floor: dict, variant: dict) -> dict:
    """Apply the pre-registered rule to the pooled self floor and the candidate-vs-reference summary."""
    self_jaccard = floor["frame_jaccard"]["any"]["median"]
    variant_jaccard = variant["frame_jaccard"]["any"]["median"]
    self_delta, variant_delta = floor["delta_start"]["median"], variant["delta_start"]["median"]
    jaccard_ok = variant_jaccard >= self_jaccard - JACCARD_MARGIN
    # With no matched pair on either side there is no boundary to disagree about.
    delta_ok = (self_delta is None and variant_delta is None) or (
        self_delta is not None and variant_delta is not None
        and variant_delta <= self_delta + DELTA_START_MARGIN_SECONDS)
    count_ok = variant["mean_count_diff"] <= MAX_MEAN_COUNT_DIFF
    gap = self_jaccard - variant_jaccard
    verdict = "suffices" if jaccard_ok and delta_ok and count_ok else "review" if gap > REVIEW_GAP else "inconclusive"
    return {
        "self_frame_jaccard": self_jaccard, "variant_frame_jaccard": variant_jaccard, "jaccard_gap": gap,
        "self_median_abs_delta_start": self_delta, "variant_median_abs_delta_start": variant_delta,
        "mean_count_diff": variant["mean_count_diff"], "jaccard_within_margin": jaccard_ok,
        "delta_start_within_margin": delta_ok, "count_diff_within_limit": count_ok, "verdict": verdict,
    }


def score_group(config: dict, segments: dict[Cell, list[dict]], arm: str, render: str) -> dict:
    reference = config["reference_variant"]
    durations = {row["index"]: row["duration_seconds"] for row in config["episodes"]}

    def pairing(variant_a: str, variant_b: str, repeats: list[int]) -> dict:
        rows = [{"index": index, **compare(segments[Cell(variant_a, index, render, arm, repeats[0])],
                                           segments[Cell(variant_b, index, render, arm, repeats[1])], duration)}
                for index, duration in durations.items()]
        return {"a": {"variant": variant_a, "repeat": repeats[0]}, "b": {"variant": variant_b, "repeat": repeats[1]},
                "episodes": rows, "summary": summarize(rows)}

    pairings = config["pairings"]
    self_pairs = {variant: pairing(variant, variant, pairings["self"]) for variant in config["variants"]}
    cross_pairs = {variant: pairing(variant, variant, pairings["cross_seed"]) for variant in config["variants"]}
    candidates = [variant for variant in config["variants"] if variant != reference]
    variant_pairs = {candidate: pairing(candidate, reference, pairings["variant"]) for candidate in candidates}
    floors = {candidate: summarize(self_pairs[candidate]["episodes"] + self_pairs[reference]["episodes"])
              for candidate in candidates}
    cross_floors = {candidate: summarize(cross_pairs[candidate]["episodes"] + cross_pairs[reference]["episodes"])
                    for candidate in candidates}
    return {
        "arm": arm, "render": render, "primary": render == config["primary_render"],
        "self": self_pairs, "cross_seed": cross_pairs, "variant": variant_pairs,
        "self_floor": floors, "cross_seed_floor": cross_floors,
        "decisions": {candidate: decide(floors[candidate], variant_pairs[candidate]["summary"])
                      for candidate in candidates},
    }


def disagreement_runs(candidate: list[int], reference: list[int], repeats: int) -> list[dict]:
    """Maximal runs of frames where one variant's repeats skip more often than the other's."""
    runs: list[dict] = []
    for frame, (c, r) in enumerate(zip(candidate, reference)):
        sign = (c > r) - (c < r)
        if not sign:
            continue
        if runs and runs[-1]["sign"] == sign and runs[-1]["end"] == frame:
            run = runs[-1]
            run["end"] = frame + 1
        else:
            run = {"sign": sign, "start": frame, "end": frame + 1, "score": 0.0, "candidate_votes": 0,
                   "reference_votes": 0}
            runs.append(run)
        run["score"] += abs(c - r) / repeats
        run["candidate_votes"] += c
        run["reference_votes"] += r
    return runs


def worst_spans(config: dict, segments: dict[Cell, list[dict]], transcripts: dict, arm: str,
                candidate: str) -> list[dict]:
    """Longest, most consistent candidate/reference disagreements at the primary render, with both texts.

    Each frame's weight is |candidate votes - reference votes| / repeats over all repeats, so a span
    that every candidate repeat skips and no reference repeat does (a transcript effect, not LLM noise)
    outranks an equally long one the repeats split on."""
    reference, render, repeats = config["reference_variant"], config["primary_render"], config["repeats"]
    spans = []
    for episode in config["episodes"]:
        count = frame_count(episode["duration_seconds"])
        votes = {}
        for variant in (candidate, reference):
            tally = [0] * count
            for repeat in range(1, repeats + 1):
                for frame in frames(segments[Cell(variant, episode["index"], render, arm, repeat)], count):
                    tally[frame] += 1
            votes[variant] = tally
        spans += [{"episode": episode, **run} for run in disagreement_runs(votes[candidate], votes[reference], repeats)]
    spans.sort(key=lambda span: (-span["score"], span["episode"]["index"], span["start"]))
    described = []
    for span in spans[:WORST_SPANS]:
        episode, length = span["episode"], span["end"] - span["start"]
        start, end = span["start"] * FRAME_SECONDS, span["end"] * FRAME_SECONDS
        skipper = candidate if span["sign"] > 0 else reference
        cited = sorted({(segment["kind"], segment["summary"])
                        for repeat in range(1, repeats + 1)
                        for segment in segments[Cell(skipper, episode["index"], render, arm, repeat)]
                        if segment["start_seconds"] < end and segment["end_seconds"] > start})
        described.append({
            "episode": episode["index"], "title": episode["title"], "start_seconds": start, "end_seconds": end,
            "seconds": length * FRAME_SECONDS, "score": span["score"], "skipped_more_by": skipper,
            "mean_votes": {candidate: span["candidate_votes"] / length, reference: span["reference_votes"] / length},
            "repeats": repeats, "segments": [{"kind": kind, "summary": summary} for kind, summary in cited],
            "text": {variant: " ".join(sentence.text.strip() for sentence in transcripts[(variant, episode["index"])][0]
                                       if sentence.start < end and sentence.end > start)
                     for variant in (candidate, reference)},
        })
    return described


def verify_results(eval_dir: Path, config: dict, results: dict) -> dict[Cell, dict]:
    """results.json must come from this config and cover the pinned cell matrix with intact cassettes."""
    require(results.get("schema_version") == SCHEMA_VERSION, "unsupported results.json schema")
    require(results.get("config_sha256") == sha256_file(eval_dir / "config.json"),
            "results.json was produced from a different config.json; rerun eval_ad_segments.py")
    rows: dict[Cell, dict] = {}
    requests: dict[tuple, set[str]] = {}
    for row in results["cells"]:
        cell = Cell(**row["cell"])
        require(cell not in rows, f"duplicate cell in results.json: {cell.label()}")
        require(row["cell_key"] == cell_key(config, cell), f"cell key mismatch: {cell.label()}")
        cassette = eval_dir / row["cassette"]
        require(cassette.is_file() and sha256_file(cassette) == row["cassette_sha256"],
                f"cassette changed since results.json was written: {cassette}")
        requests.setdefault((cell.variant, cell.episode, cell.render, cell.arm), set()).add(row["request_sha256"])
        rows[cell] = row
    expected = set(cells(config))
    require(set(rows) == expected, f"results.json covers {len(set(rows) & expected)} of {len(expected)} pinned cells")
    require(all(len(hashes) == 1 for hashes in requests.values()), "repeats of one request carry different hashes")
    return rows


def usage_totals(config: dict, rows: dict[Cell, dict]) -> dict:
    totals = {}
    for arm in config["arms"]:
        selected = [row for cell, row in rows.items() if cell.arm == arm["label"]]
        totals[arm["label"]] = {
            "calls": len(selected), "attempts": sum(row["attempts"] for row in selected),
            **{field: sum(row["usage"][field] for row in selected) for field in selected[0]["usage"]},
            "cost_usd": sum(row["cost_usd"] for row in selected),
            "median_latency_ms": percentile([row["latency_ms"] for row in selected], 0.5),
        }
    return totals


def line_resolution(config: dict, rows: dict[Cell, dict]) -> list[dict]:
    """|resolved - echoed| seconds for line-citing renders: how far the model's own timestamps drifted."""
    summary = []
    for arm in config["arms"]:
        for render in config["renders"]:
            for variant in config["variants"]:
                records = [row["line_resolution"] for cell, row in rows.items()
                           if (cell.arm, cell.render, cell.variant) == (arm["label"], render, variant)
                           and row["line_resolution"] is not None]
                if records:
                    deltas = [value for record in records for pair in record["deltas"] for value in pair
                              if value is not None]
                    summary.append({"arm": arm["label"], "render": render, "variant": variant,
                                    "cited_segments": sum(record["cited_segments"] for record in records),
                                    "unresolved_citations": sum(record["unresolved_citations"] for record in records),
                                    "abs_delta_seconds": spread(deltas)})
    return summary


def score_eval(eval_dir: Path) -> dict:
    config = load_json(eval_dir / "config.json")
    results_path = eval_dir / "results.json"
    results = load_json(results_path)
    transcripts = load_transcripts(eval_dir, config)
    verification = verify_pins(eval_dir, config, transcripts, rerender=False)
    rows = verify_results(eval_dir, config, results)
    segments = {cell: row["segments"] for cell, row in rows.items()}
    candidates = [variant for variant in config["variants"] if variant != config["reference_variant"]]
    scores = {
        "eval_id": config["eval_id"], "config_sha256": results["config_sha256"],
        "results_sha256": sha256_file(results_path), "thresholds": THRESHOLDS,
        "groups": [score_group(config, segments, arm["label"], render)
                   for arm in config["arms"] for render in config["renders"]],
        "worst_spans": {arm["label"]: {candidate: worst_spans(config, segments, transcripts, arm["label"], candidate)
                                       for candidate in candidates} for arm in config["arms"]},
        "usage": usage_totals(config, rows), "line_resolution": line_resolution(config, rows),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        # Round-trip through JSON so a fresh score compares equal to the stored scores.json.
        "scores": json.loads(json.dumps(scores)),
        "verification": {**verification, "results_verified": True, "cassettes_verified": True, "cassettes": len(rows),
                         "cell_matrix_complete_verified": True, "request_hash_stable_across_repeats_verified": True},
    }


def print_summary(config: dict, scored: dict) -> None:
    for group in scored["scores"]["groups"]:
        for candidate, decision in group["decisions"].items():
            print(f"{group['arm']} {group['render']}: {candidate} vs {config['reference_variant']} "
                  f"J={decision['variant_frame_jaccard']:.3f}, self floor {decision['self_frame_jaccard']:.3f}, "
                  f"gap {decision['jaccard_gap']:+.3f}, mean count diff {decision['mean_count_diff']:.2f}: "
                  f"{decision['verdict']}")
    for arm, by_candidate in scored["scores"]["worst_spans"].items():
        for candidate, spans in by_candidate.items():
            print(f"\nWorst {candidate}/{config['reference_variant']} disagreements, {arm}, {config['primary_render']}:")
            for number, span in enumerate(spans, 1):
                votes = ", ".join(f"{name} {value:.1f}/{span['repeats']}" for name, value in span["mean_votes"].items())
                print(f"{number:2d}. episode {span['episode']:02d} {span['start_seconds']:.0f}–{span['end_seconds']:.0f}s "
                      f"({span['seconds']:.0f}s), skipped more by {span['skipped_more_by']} ({votes})")
                for segment in span["segments"]:
                    print(f"    {segment['kind']}: {segment['summary']}")
                for variant, text in span["text"].items():
                    print(f"    {variant}: {text or '(no words)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a recorded ad-segment eval; refuses on any pin mismatch.")
    parser.add_argument("--eval-id", required=True, help="directory name under benchmarks/evals/")
    args = parser.parse_args()
    require(bool(EVAL_ID.fullmatch(args.eval_id)), "--eval-id must be 1-80 safe filename characters")
    eval_dir = EVALS_ROOT / args.eval_id
    scored = score_eval(eval_dir)
    atomic_json(eval_dir / "scores.json", scored)
    print_summary(load_json(eval_dir / "config.json"), scored)
    print(f"\nWrote {eval_dir / 'scores.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
