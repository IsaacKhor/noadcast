#!/usr/bin/env python3
"""Write AD_EVAL.md, ad-eval-verification.json, and ad-eval.csv for a scored ad-segment eval.

Re-scores the eval from results.json (re-verifying every pin) and refuses to report unless that
fresh scoring is identical to the stored scores.json. Outputs go to benchmarks/evals/<eval-id>/.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from noadcast.classify.base import SEGMENT_KINDS

from eval_ad_segments import EVALS_ROOT, EVAL_ID
from report_tal_comparison import atomic_json, atomic_text, load_json, require, sha256_file
from score_ad_eval import (
    DELTA_START_MARGIN_SECONDS, IOU_THRESHOLD, JACCARD_MARGIN, KINDS, MAX_MEAN_COUNT_DIFF, REVIEW_GAP, percentile,
    score_eval,
)


VERDICT_LABELS = {"suffices": "SUFFICES", "review": "REVIEW", "inconclusive": "INCONCLUSIVE"}
VERDICT_SENTENCES = {
    "suffices": "{candidate} suffices by the pre-registered rule",
    "review": ("the gap exceeds {review:.2f}, so the worst spans below need a manual read; escalate to {reference} "
               "only if one shows a lost ad-marker phrase"),
    "inconclusive": ("not every criterion is met, but the gap is within {review:.2f}, so the rule gives no case to "
                     "escalate to {reference}"),
}
TEXT_LIMIT = 600


def fmt(value: float | None, digits: int = 3, unit: str = "") -> str:
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


def clip(text: str) -> str:
    """Long spans keep their opening and closing words, where ad reads announce themselves."""
    if len(text) <= TEXT_LIMIT:
        return text or "(no words)"
    return text[: TEXT_LIMIT * 2 // 3] + " … " + text[-TEXT_LIMIT // 3:]


def pairing_rows(config: dict, group: dict) -> list[tuple[str, dict]]:
    reference = config["reference_variant"]
    self_repeats, cross_repeats, variant_repeats = (config["pairings"][name] for name in ("self", "cross_seed", "variant"))
    rows = [(f"self {variant} (r{self_repeats[0]} vs r{self_repeats[1]})", group["self"][variant])
            for variant in config["variants"]]
    rows += [(f"cross-seed {variant} (r{cross_repeats[0]} vs r{cross_repeats[1]})", group["cross_seed"][variant])
             for variant in config["variants"]]
    rows += [(f"{candidate} vs {reference} (r{variant_repeats[0]} vs r{variant_repeats[1]})", pairing)
             for candidate, pairing in group["variant"].items()]
    return rows


def render_markdown(config: dict, scored: dict) -> str:
    scores, verification = scored["scores"], scored["verification"]
    reference, primary = config["reference_variant"], config["primary_render"]
    candidates = [variant for variant in config["variants"] if variant != reference]
    groups = {(group["arm"], group["render"]): group for group in scores["groups"]}
    arms = [arm["label"] for arm in config["arms"]]
    hours = sum(row["duration_seconds"] for row in config["episodes"]) / 3600
    lead = groups[(arms[0], primary)]["decisions"][candidates[0]]
    words = {"candidate": candidates[0], "reference": reference, "review": REVIEW_GAP}
    lines = [
        f"# Transcript-variant ad-segment agreement: {config['eval_id']}", "",
        f"**{VERDICT_LABELS[lead['verdict']]}: with {arms[0]} and the {primary} rendering, {candidates[0]} and "
        f"{reference} transcripts produced skip segments with median frame Jaccard {lead['variant_frame_jaccard']:.3f} "
        f"against a same-transcript noise floor of {lead['self_frame_jaccard']:.3f} (gap {lead['jaccard_gap']:+.3f}), "
        f"median |Δstart| {fmt(lead['variant_median_abs_delta_start'], 2, ' s')} against "
        f"{fmt(lead['self_median_abs_delta_start'], 2, ' s')}, and {lead['mean_count_diff']:.2f} segments of count "
        f"difference per episode; {VERDICT_SENTENCES[lead['verdict']].format(**words)}.**", "",
        f"Corpus `{config['corpus']['root']}`: {len(config['episodes'])} episodes, {hours:.2f} hours. Each of "
        f"{len(config['variants'])} transcript variants × {len(config['renders'])} render variants × {len(arms)} "
        f"classifier arm{'s' if len(arms) > 1 else ''} × {config['repeats']} repeats was classified per episode, "
        f"{sum(item['calls'] for item in scores['usage'].values())} recorded calls in all. "
        + (config["corpus"]["caveat"] or ""), "",
        "| Arm | Render | Candidate | Self floor J | Cross-seed J | Candidate vs reference J | Gap | "
        "Median \\|Δstart\\| floor → candidate | Mean count diff | Verdict |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for arm in arms:
        for render in config["renders"]:
            group = groups[(arm, render)]
            for candidate, decision in group["decisions"].items():
                cross = group["cross_seed_floor"][candidate]["frame_jaccard"]["any"]["median"]
                lines.append(
                    f"| {arm} | {render}{' (primary)' if render == primary else ''} | {candidate} | "
                    f"{decision['self_frame_jaccard']:.3f} | {cross:.3f} | {decision['variant_frame_jaccard']:.3f} | "
                    f"{decision['jaccard_gap']:+.3f} | {fmt(decision['self_median_abs_delta_start'], 2, 's')} → "
                    f"{fmt(decision['variant_median_abs_delta_start'], 2, 's')} | {decision['mean_count_diff']:.2f} | "
                    f"{VERDICT_LABELS[decision['verdict']]} |")
    lines += [
        "", f"Self floor J pools the repeat-{config['pairings']['self'][0]}-versus-{config['pairings']['self'][1]} "
        f"frame Jaccards of {' and '.join(config['variants'])}; cross-seed does the same for repeats "
        f"{config['pairings']['cross_seed'][0]} and {config['pairings']['cross_seed'][1]}. All Jaccards are medians "
        "over episodes.", "",
        f"## Agreement by kind and by segment ({primary})", "",
        "| Arm | Pairing | J any | J ad | J intro | J outro | Matched | IoU median | IoU p10 | Unmatched a / b | "
        "\\|Δstart\\| median / p90 / max | \\|Δend\\| median / p90 / max | Equal counts ad / intro / outro |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in arms:
        for label, pairing in pairing_rows(config, groups[(arm, primary)]):
            summary = pairing["summary"]
            start, end = summary["delta_start"], summary["delta_end"]
            lines.append(
                f"| {arm} | {label} | "
                + " | ".join(f"{summary['frame_jaccard'][kind]['median']:.3f}" for kind in KINDS)
                + f" | {summary['matched']} | {fmt(summary['matched_iou_median'])} | {fmt(summary['matched_iou_p10'])} | "
                f"{summary['unmatched_a']} / {summary['unmatched_b']} | "
                f"{fmt(start['median'], 2)} / {fmt(start['p90'], 2)} / {fmt(start['max'], 2)} | "
                f"{fmt(end['median'], 2)} / {fmt(end['p90'], 2)} / {fmt(end['max'], 2)} | "
                + " / ".join(f"{summary['count_agreement'][kind]:.0%}" for kind in SEGMENT_KINDS) + " |")
    lines += ["", "## Per-episode", ""]
    for arm in arms:
        group = groups[(arm, primary)]
        for candidate, pairing in group["variant"].items():
            lines += [
                f"{arm}, {primary}: frame Jaccard per episode, and repeat-1 segment counts as ad / intro / outro.", "",
                f"| Episode | Minutes | Self J {candidate} | Self J {reference} | {candidate} vs {reference} J | "
                f"{candidate} counts | {reference} counts |", "|---|---:|---:|---:|---:|---|---|",
            ]
            for row, episode in zip(pairing["episodes"], config["episodes"]):
                own = {variant: next(item for item in group["self"][variant]["episodes"] if item["index"] == row["index"])
                       for variant in (candidate, reference)}
                lines.append(
                    f"| {episode['index']:02d} · {episode['title']} | {episode['duration_seconds'] / 60:.1f} | "
                    f"{own[candidate]['frame_jaccard']['any']:.3f} | {own[reference]['frame_jaccard']['any']:.3f} | "
                    f"{row['frame_jaccard']['any']:.3f} | "
                    + " / ".join(str(row["counts_a"][kind]) for kind in SEGMENT_KINDS) + " | "
                    + " / ".join(str(row["counts_b"][kind]) for kind in SEGMENT_KINDS) + " |")
            lines.append("")
    lines += ["## Line citations", ""]
    if scores["line_resolution"]:
        lines += [
            "Index renderings ask for line indices; the server resolves seconds from the sentence table. "
            "\\|resolved − echoed\\| is how far the seconds the model also echoed were from the lines it cited.", "",
            "| Arm | Render | Variant | Cited segments | Unresolved citations | \\|resolved − echoed\\| median / p90 / max |",
            "|---|---|---|---:|---:|---:|",
        ]
        for item in scores["line_resolution"]:
            spread = item["abs_delta_seconds"]
            lines.append(f"| {item['arm']} | {item['render']} | {item['variant']} | {item['cited_segments']} | "
                         f"{item['unresolved_citations']} | {fmt(spread['median'], 2, 's')} / "
                         f"{fmt(spread['p90'], 2, 's')} / {fmt(spread['max'], 2, 's')} |")
    else:
        lines.append("No recorded cell carried line-resolution metrics.")
    lines += [
        "", "## Usage and identities", "",
        "| Arm | Calls | Attempts | Input tokens | Cached input | Cache writes | Thought tokens | Output tokens | "
        "Recorded cost | Median latency |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, usage in scores["usage"].items():
        lines.append(
            f"| {arm} | {usage['calls']} | {usage['attempts']} | {usage['input_tokens']:,} | "
            f"{usage['cached_input_tokens']:,} | {usage['cache_write_tokens']:,} | {usage['thought_tokens']:,} | "
            f"{usage['output_tokens']:,} | ${usage['cost_usd']:.2f} | {fmt(usage['median_latency_ms'], 0, ' ms')} |")
    lines += ["", "Thought tokens are reported separately only by Gemini; Anthropic bills thinking inside output tokens.",
              "", "Transcript variants:", ""]
    for name, variant in config["variants"].items():
        lines.append(
            f"- {name}{' (reference)' if name == reference else ''}: {variant['asr_model']}, model SHA-256 "
            f"`{variant['asr_model_sha256']}`, revision `{variant['asr_model_revision']}`, run `{variant['run_id']}` "
            f"({variant['workers']} workers × {variant['cpu_threads']} threads, {variant['compute_type']}, beam "
            f"{variant['beam_size']}, batch {variant['batch_size']}, results SHA-256 `{variant['results_sha256']}`)."
            + (f" {variant['note']}" if variant["note"] else ""))
    joiner, classifier = config["joiner"], config["classifier"]
    lines += [
        "", f"Joiner: `{joiner['module']}` version {joiner['version']}, source SHA-256 `{joiner['source_sha256']}` "
        f"({'matches' if verification['joiner_source_matches_pin'] else 'differs from'} the current source), parameters "
        + ", ".join(f"`{key}={value}`" for key, value in joiner["params"].items()) + ".", "",
        f"Classifier: prompt `{classifier['prompt_version']}`, snap to silence {'on' if classifier['snap_to_silence'] else 'off'}, "
        f"price table `{classifier['price_table_version']}`; the pinned classifier sources "
        f"{'match' if verification['classifier_source_matches_pin'] else 'differ from'} the current ones. Corpus manifest "
        f"`{config['corpus']['manifest']}` SHA-256 `{config['corpus']['manifest_sha256']}`.", "",
        "## Worst disagreements for manual review", "",
        "Spans are ranked by the sum over their 1 s frames of |candidate votes − reference votes| / repeats, counting "
        "every repeat, so a span one transcript's repeats all skip and the other's never do ranks above LLM noise. "
        "Text is every sentence overlapping the span, clipped here; `scores.json` keeps it in full.", "",
    ]
    for arm, by_candidate in scores["worst_spans"].items():
        for candidate, spans in by_candidate.items():
            lines += [f"### {candidate} vs {reference}, {arm}, {primary}", ""]
            for number, span in enumerate(spans, 1):
                votes = ", ".join(f"{name} {value:.1f}/{span['repeats']}" for name, value in span["mean_votes"].items())
                cited = "; ".join(f"{segment['kind']}: {segment['summary']}" for segment in span["segments"])
                lines += [f"{number}. Episode {span['episode']:02d} · {span['title']}, {span['start_seconds']:.0f}–"
                          f"{span['end_seconds']:.0f} s ({span['seconds']:.0f} s), skipped more by "
                          f"{span['skipped_more_by']} (mean votes {votes}). {cited or 'No segment summary.'}"]
                lines += [f"   - {variant}: {clip(text)}" for variant, text in span["text"].items()]
            if not spans:
                lines.append("No frame differed between the variants' repeats.")
            lines.append("")
    snap = "on" if classifier["snap_to_silence"] else "off"
    lines += [
        "## Methodology", "",
        "Each cell sends one pinned transcript through `noadcast.classify` exactly as the server would: sentences joined "
        f"from the ASR run's word timestamps by joiner version {joiner['version']} with the parameters above, the "
        "silence map derived from word gaps, the measured decoded duration, and the episode and podcast titles. Render "
        "variants are `Settings` overrides of `transcript_format` × `include_silence`; the seconds format never renders "
        "silence lines, so it has a single variant. Scored segments are the classifier output after "
        f"`sanitize.finalize` (snap to silence {snap}), i.e. what a listener would skip. Repeats of one request run in "
        "sequence so provider-side prefix caching can apply; the harness itself sets no cache controls.", "",
        f"Frames are {scores['thresholds']['frame_seconds']:.0f} s wide and a frame is skipped when its midpoint lies in "
        "a segment. Frame Jaccard is |A ∩ B| / |A ∪ B| per episode, 1.0 when neither side skips anything, and each "
        "per-kind column restricts both sides to that kind. Segments are matched one-to-one within a kind by "
        f"descending IoU, keeping IoU ≥ {IOU_THRESHOLD}; |Δstart| and |Δend| are over matched pairs. The pre-registered "
        f"rule is SUFFICES when the candidate's median frame Jaccard is at least the pooled self floor − {JACCARD_MARGIN}, "
        f"its median |Δstart| is at most the floor's + {DELTA_START_MARGIN_SECONDS:.0f} s, and its mean count "
        f"difference is at most {MAX_MEAN_COUNT_DIFF:.0f} per episode; REVIEW when the Jaccard gap exceeds "
        f"{REVIEW_GAP}, where escalation needs a manual read that finds a lost ad-marker phrase, because a bare "
        "metric gap can be LLM nondeterminism amplified by trivial wording; otherwise INCONCLUSIVE, which is no case "
        "to escalate.", "",
        "## Caveats", "",
        (config["corpus"]["caveat"] + " " if config["corpus"]["caveat"] else "")
        + "No human labels exist: agreement between transcript variants, and between an LLM's repeats, is not "
        "detection accuracy, and both variants can agree on the same wrong span. The self floor comes from "
        f"{config['repeats']} repeats per request and is itself noisy; compare it with the cross-seed column. Providers "
        "can change the model behind a stable model ID, so cassettes pin what was answered when recorded, not what "
        "would be answered today. Recorded cost uses the price table in effect when the cassettes were replayed."
        + "".join(f" {name}: {variant['note']}" for name, variant in config["variants"].items() if variant["note"]), "",
        f"Verified: the corpus manifest hash, {verification['sentence_files']} pinned sentence files, "
        f"{verification['asr_transcripts_rechecked']} of {verification['asr_transcripts_pinned']} ASR transcripts "
        "re-hashed against their pins"
        + (" (the rest are absent from this checkout)"
           if verification["asr_transcripts_rechecked"] < verification["asr_transcripts_pinned"] else "") + ", "
        f"{verification['prompt_files']} content-addressed prompts, {verification['cassettes']} cassettes against "
        "results.json, the complete cell matrix, one request hash per repeated request, and a fresh scoring identical "
        "to `scores.json`. See `ad-eval-verification.json` and `ad-eval.csv`.", "",
    ]
    return "\n".join(lines)


def write_csv(path: Path, config: dict, scored: dict) -> None:
    columns = [
        "arm", "render", "pairing", "variant_a", "repeat_a", "variant_b", "repeat_b", "episode_index",
        "episode_title", *(f"frame_jaccard_{kind}" for kind in KINDS), "matched", "matched_iou_median",
        "unmatched_a", "unmatched_b", "delta_start_median", "delta_end_median",
        *(f"count_{kind}_a" for kind in SEGMENT_KINDS), *(f"count_{kind}_b" for kind in SEGMENT_KINDS), "count_diff",
    ]
    titles = {row["index"]: row["title"] for row in config["episodes"]}
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for group in scored["scores"]["groups"]:
            for name in ("self", "cross_seed", "variant"):
                for pairing in group[name].values():
                    for row in pairing["episodes"]:
                        writer.writerow({
                            "arm": group["arm"], "render": group["render"], "pairing": name,
                            "variant_a": pairing["a"]["variant"], "repeat_a": pairing["a"]["repeat"],
                            "variant_b": pairing["b"]["variant"], "repeat_b": pairing["b"]["repeat"],
                            "episode_index": row["index"], "episode_title": titles[row["index"]],
                            **{f"frame_jaccard_{kind}": row["frame_jaccard"][kind] for kind in KINDS},
                            "matched": len(row["matched_iou"]),
                            "matched_iou_median": percentile(row["matched_iou"], 0.5),
                            "unmatched_a": row["unmatched_a"], "unmatched_b": row["unmatched_b"],
                            "delta_start_median": percentile(row["delta_start"], 0.5),
                            "delta_end_median": percentile(row["delta_end"], 0.5),
                            **{f"count_{kind}_a": row["counts_a"][kind] for kind in SEGMENT_KINDS},
                            **{f"count_{kind}_b": row["counts_b"][kind] for kind in SEGMENT_KINDS},
                            "count_diff": row["count_diff"],
                        })
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Report a scored ad-segment eval in house style.")
    parser.add_argument("--eval-id", required=True, help="directory name under benchmarks/evals/")
    args = parser.parse_args()
    require(bool(EVAL_ID.fullmatch(args.eval_id)), "--eval-id must be 1-80 safe filename characters")
    return write_report(EVALS_ROOT / args.eval_id)


def write_report(eval_dir: Path) -> int:
    config = load_json(eval_dir / "config.json")
    stored = load_json(eval_dir / "scores.json")
    scored = score_eval(eval_dir)
    require(stored.get("scores") == scored["scores"],
            "scores.json does not match a fresh scoring of results.json; rerun score_ad_eval.py")
    outputs = {name: eval_dir / name for name in ("AD_EVAL.md", "ad-eval.csv", "ad-eval-verification.json")}
    atomic_text(outputs["AD_EVAL.md"], render_markdown(config, scored))
    write_csv(outputs["ad-eval.csv"], config, scored)
    decisions = {f"{group['arm']} {group['render']} {candidate}": decision["verdict"]
                 for group in scored["scores"]["groups"] for candidate, decision in group["decisions"].items()}
    atomic_json(outputs["ad-eval-verification.json"], {
        "verified": True, "eval_id": config["eval_id"], "config_sha256": scored["scores"]["config_sha256"],
        "results_sha256": scored["scores"]["results_sha256"], "scores_sha256": sha256_file(eval_dir / "scores.json"),
        "corpus": config["corpus"],
        "episodes": [{"index": row["index"], "source_sha256": row["source_sha256"]} for row in config["episodes"]],
        "variants": {name: {key: variant[key] for key in ("run_id", "results_sha256", "asr_model", "asr_model_sha256",
                                                            "asr_model_revision", "note")}
                     for name, variant in config["variants"].items()},
        "joiner": config["joiner"], "classifier": config["classifier"], "arms": config["arms"],
        **scored["verification"], "scores_recomputed_match_verified": True,
        "accuracy_evaluated": False, "human_labels": False, "decisions": decisions,
        "outputs": [str(path.relative_to(eval_dir)) for path in outputs.values()],
    })
    print(f"Verified and reported {config['eval_id']}; wrote " + ", ".join(str(path) for path in outputs.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
