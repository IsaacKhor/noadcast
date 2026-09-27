#!/usr/bin/env python3
"""Experiment B driver: does the ASR transcript variant change the classifier's skip segments?

Every cell — transcript variant × render variant × provider arm × episode × repeat — goes through
``noadcast.classify`` exactly as the server would send it, and each provider response is stored as
a cassette under ``benchmarks/evals/<eval-id>/``. ``--replay`` (the default) never calls a provider
and fails loudly on a missing cassette, so scoring is offline and free once recorded. ``--record``
calls the provider only for cells without a cassette; ``--refresh`` re-records every cell.

The first ``--record``/``--refresh`` pins the eval in ``config.json``: the corpus manifest and episode
source hashes, ASR model identities and per-episode ASR transcript hashes, the joiner version,
parameters, and source hash, the joined sentences, every rendered prompt (content-addressed), the
prompt version, and the classifier sources. Transcripts are byte-reproducible, so a mismatch is
refused rather than scored. Replay writes ``results.json``, the input to ``score_ad_eval.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import gzip
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import noadcast
from noadcast.classify import base as classify_base, costs, render as classify_render, sanitize
from noadcast.classify.base import ClassifierError, ClassifyRequest, ClassifyResult, DetectedSegment, TokenUsage
from noadcast.classify.registry import ClassifierRegistry
from noadcast.classifier_models import MODEL_IDS, thinking_for_model
from noadcast.config import Settings
from noadcast.timeutil import now_iso
from noadcast.transcribe import joiner
from noadcast.transcribe.protocol import AsrSegmentMeta, Sentence, SilenceRegion, Word

from report_tal_comparison import ROOT, atomic_json, load_json, require, sha256_file


EVALS_ROOT = ROOT / "benchmarks" / "evals"
SCHEMA_VERSION = 1
KEY_NAMESPACE = "noadcast-ad-eval-cell-v1"
# This evaluation is pinned to the segments-v2 index/seconds A/B arms. The
# production segments-v3 sentence format is evaluated separately so these
# cassettes retain their historical prompt and transcript meaning.
RENDERS = {
    "index+silence": {"transcript_format": "index", "include_silence": True},
    "index-silence": {"transcript_format": "index", "include_silence": False},
    "seconds": {"transcript_format": "seconds", "include_silence": False},
}
# Repeat numbers compared by each pairing: self is the LLM noise floor on one transcript, cross_seed
# checks that floor on another repeat pair, variant compares a candidate transcript to the reference.
PAIRINGS = {"self": (1, 2), "cross_seed": (1, 3), "variant": (1, 1)}
PROVIDERS = ("openrouter",)
DEFAULT_CORPUS = "benchmarks/tal"
DEFAULT_VARIANTS = (
    "tiny.en=benchmarks/tal/runs/wt-crossover-2-on-20260922",
    "small.en=benchmarks/tal/runs/small-en-words-20260922",
)
DEFAULT_REFERENCE = "small.en"
EVAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
CORPUS_CAVEATS = {
    "tal": ("The TAL corpus contains no third-party advertisements (verified during planning: its episodes carry only "
            "intro billboards, act-break idents, a house promo, credits, and teasers), so on TAL this measures "
            "intro/outro/house-promo agreement, not ad detection."),
    "ads": ("Each benchmarks/ads episode is a single dynamic-ad-insertion render fetched on the date in its manifest; "
            "another fetch may carry different ads, so agreement applies only to these exact bytes."),
}
RUN_NOTES = {
    "wt-crossover-2-on-20260922": "Experiment A run 2 (word timestamps on) of the OFF/ON/ON/OFF crossover.",
    "small-en-words-20260922": ("Transcript input only: transcribed while other agents shared the host, so its "
                                "timings are not a clean benchmark."),
}


@dataclasses.dataclass(frozen=True, order=True)
class Cell:
    variant: str
    episode: int
    render: str
    arm: str
    repeat: int

    def label(self) -> str:
        return f"{self.variant} episode {self.episode:02d} {self.render} {self.arm} repeat {self.repeat}"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_gzip_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(gzip.compress(canonical(value), compresslevel=9, mtime=0))
    temporary.replace(path)


def read_gzip_json(path: Path) -> Any:
    require(path.is_file(), f"missing pinned file: {path}")
    return json.loads(gzip.decompress(path.read_bytes()))


def portable(path: Path) -> str:
    """Repository-relative when inside the repository, so committed pins do not embed a checkout path."""
    resolved = path.resolve()
    return str(resolved.relative_to(ROOT)) if resolved.is_relative_to(ROOT) else str(resolved)


def resolve(text: str) -> Path:
    path = Path(text)
    return path if path.is_absolute() else ROOT / path


def parse_arm(label: str) -> dict:
    provider, _, rest = label.partition(":")
    model, _, thinking = rest.partition(":")
    require(provider in PROVIDERS and bool(model),
            f"arm must be PROVIDER:MODEL[:THINKING] with PROVIDER in {', '.join(PROVIDERS)}: {label!r}")
    require(model in MODEL_IDS, f"model must be one of {MODEL_IDS}: {label!r}")
    expected_thinking = thinking_for_model(model)
    require(not thinking or thinking == expected_thinking, f"thinking is fixed by the model preset: {label!r}")
    return {"label": label, "provider": provider, "model": model, "thinking": expected_thinking}


def eval_settings() -> Settings:
    """Pinned v2 evaluation settings; production uses the v3 sentence prompt."""
    return Settings.load(env={"NOADCAST_SECRETS_FILE": str(ROOT / "secrets.env"), **os.environ,
                              "NOADCAST_ALLOW_NO_AUTH": "1"}).with_overrides(
                                  prompt_version="segments-v2", transcript_format="index", include_silence=True)


def asr_transcript(path: Path) -> tuple[list[Word], list[AsrSegmentMeta], str]:
    """faster-whisper benchmark transcript -> joiner input, plus a content hash that is stable across re-runs."""
    segments = load_json(path).get("segments")
    require(isinstance(segments, list) and segments, f"no ASR segments: {path}")
    metas = [AsrSegmentMeta(index, float(row["start"]), float(row["end"]), float(row["compression_ratio"]),
                            float(row["no_speech_prob"]), float(row["avg_logprob"])) for index, row in enumerate(segments)]
    words = [Word(float(word["start"]), float(word["end"]), word["word"], float(word["probability"]), index)
             for index, row in enumerate(segments) for word in row.get("words") or ()]
    require(bool(words), f"ASR transcript has no word timestamps: {path}")
    content = {"words": [dataclasses.astuple(word) for word in words],
               "segments": [dataclasses.astuple(meta) for meta in metas]}
    return words, metas, digest(content)


def sentence_from(row: dict) -> Sentence:
    return Sentence(**{**row, "asr_segments": tuple(row["asr_segments"]), "flags": tuple(row["flags"])})


def render_prompt(sentences: tuple[Sentence, ...], silences: tuple[SilenceRegion, ...], duration: float,
                  render: str) -> dict:
    spec = RENDERS[render]
    rendered = classify_render.render_transcript(
        sentences, silences, fmt=spec["transcript_format"], include_silence=spec["include_silence"],
        episode_duration=duration)
    require(rendered.format == spec["transcript_format"] and rendered.include_silence is spec["include_silence"],
            f"renderer did not honour render variant {render}")
    return {**spec, "text": rendered.text}


def podcast_title(corpus_root: Path, source: dict) -> str | None:
    if source.get("podcast_title"):
        return source["podcast_title"]
    feed = corpus_root / "feed.xml"
    return ET.parse(feed).getroot().findtext("channel/title") if feed.is_file() else None


def classifier_sources() -> dict[str, str]:
    package = Path(classify_base.__file__).parent
    return {path.name: sha256_file(path) for path in sorted(package.glob("*.py"))}


def prepare(eval_dir: Path, corpus_root: Path, variants: dict[str, Path], reference: str, arms: list[dict],
            repeats: int, episode_indexes: list[int] | None, settings: Settings) -> dict:
    """Join each variant's ASR words, render every prompt, and pin all of it in config.json."""
    require(reference in variants and len(variants) >= 2, "need a reference variant and at least one candidate")
    require(repeats >= max(max(pair) for pair in PAIRINGS.values()), f"need at least {max(PAIRINGS['cross_seed'])} repeats")
    require(len({arm["label"] for arm in arms}) == len(arms) and arms, "arms must be non-empty and distinct")
    require(settings.prompt_version == "segments-v2", "this A/B harness uses the segments-v2 prompt")
    primary = next((name for name, spec in RENDERS.items()
                    if spec == {"transcript_format": settings.transcript_format,
                                "include_silence": settings.include_silence}), None)
    require(primary is not None, "choose an index or seconds render for the segments-v2 A/B harness")
    manifest_path = corpus_root / "manifest.json"
    manifest = load_json(manifest_path)
    sources = {row["index"]: row for row in manifest["episodes"]}
    indexes = episode_indexes or sorted(sources)
    require(set(indexes) <= set(sources), f"episodes absent from {manifest_path}: {sorted(set(indexes) - set(sources))}")
    runs = {name: load_json(run_dir / "results.json") for name, run_dir in variants.items()}
    for name, results in runs.items():  # all runs are checked before anything is written
        require(results.get("status") == "complete", f"ASR run is not complete: {variants[name]}")
        require(results["config"].get("word_timestamps") is True, f"the joiner needs word timestamps: {variants[name]}")
        require(results["config"].get("sample_seconds") == 0, f"ASR run must transcribe full episodes: {variants[name]}")
        require(results["input"]["manifest_sha256"] == sha256_file(manifest_path),
                f"{variants[name]} transcribed a different corpus manifest")
    params = joiner.JoinerParams()
    durations: dict[int, float] = {}
    variant_pins, prompts = {}, {}
    for name, run_dir in variants.items():
        results_path, results = run_dir / "results.json", runs[name]
        by_index = {row["index"]: row for row in results["episodes"]}
        episodes, prompts[name] = {}, {}
        for index in indexes:
            row = by_index.get(index)
            require(row is not None and row["sha256"] == sources[index]["sha256"],
                    f"{run_dir} lacks episode {index} or transcribed different audio")
            duration = float(row["full_decoded_audio_seconds"])
            require(abs(duration - float(sources[index]["duration_seconds"])) < 2,
                    f"decoded duration disagrees with the manifest: {run_dir} episode {index}")
            require(abs(durations.setdefault(index, duration) - duration) < 1e-6,
                    f"variants decoded different audio for episode {index}")
            transcript_path = run_dir / row["transcript_json"]
            words, segments, transcript_sha256 = asr_transcript(transcript_path)
            sentences = tuple(joiner.join_words(words, segments, params))
            silences = tuple(joiner.derive_silences(words, duration, params))
            payload = {"schema_version": SCHEMA_VERSION, "variant": name, "episode": index,
                       "episode_duration": duration, "joiner_version": joiner.JOINER_VERSION,
                       "sentences": [dataclasses.asdict(item) for item in sentences],
                       "silences": [dataclasses.asdict(item) for item in silences]}
            relative = Path("transcripts") / name / f"{index:02d}.json.gz"
            write_gzip_json(eval_dir / relative, payload)
            episodes[str(index)] = {
                "asr_transcript": portable(transcript_path), "asr_transcript_sha256": transcript_sha256,
                "sentences": str(relative), "sentences_sha256": digest(payload), "word_count": len(words),
                "sentence_count": len(sentences), "silence_count": len(silences),
            }
            prompts[name][str(index)] = {}
            for render in RENDERS:
                record = render_prompt(sentences, silences, duration, render)
                prompt_sha256 = digest(record)
                write_gzip_json(eval_dir / "prompts" / f"{prompt_sha256}.json.gz", record)
                prompts[name][str(index)][render] = prompt_sha256
        variant_pins[name] = {
            "run": portable(run_dir), "run_id": results["run_id"], "results_sha256": sha256_file(results_path),
            "asr_model": results["config"]["model"], "asr_model_sha256": results["model"]["model_bin_sha256"],
            "asr_model_revision": results["model"].get("revision"), "compute_type": results["config"]["compute_type"],
            "workers": results["config"]["workers"], "cpu_threads": results["config"]["cpu_threads"],
            "batch_size": results["config"]["batch_size"], "beam_size": results["config"]["beam_size"],
            "language": results["config"]["language"], "versions": results["system"].get("versions"),
            "note": RUN_NOTES.get(run_dir.name), "episodes": episodes,
        }
    config = {
        "schema_version": SCHEMA_VERSION, "eval_id": eval_dir.name, "created_at": now_iso(),
        "noadcast_version": noadcast.__version__,
        "corpus": {"root": portable(corpus_root), "manifest": portable(manifest_path),
                   "manifest_sha256": sha256_file(manifest_path), "caveat": CORPUS_CAVEATS.get(corpus_root.name)},
        "episodes": [{"index": index, "title": sources[index]["title"],
                      "podcast_title": podcast_title(corpus_root, sources[index]),
                      "source_sha256": sources[index]["sha256"], "duration_seconds": durations[index]}
                     for index in indexes],
        "variants": variant_pins, "reference_variant": reference,
        "joiner": {"module": joiner.__name__, "version": joiner.JOINER_VERSION, "params": dataclasses.asdict(params),
                   "source_sha256": sha256_file(Path(joiner.__file__))},
        "classifier": {"prompt_version": settings.prompt_version, "snap_to_silence": settings.snap_to_silence,
                       "price_table_version": costs.PRICE_TABLE_VERSION, "source_sha256": classifier_sources()},
        "renders": RENDERS, "primary_render": primary, "arms": arms, "repeats": repeats,
        "pairings": {name: list(pair) for name, pair in PAIRINGS.items()}, "prompts": prompts,
    }
    atomic_json(eval_dir / "config.json", config)
    return config


def load_transcripts(eval_dir: Path, config: dict) -> dict[tuple[str, int], tuple[tuple, tuple]]:
    """Pinned sentences and silences per (variant, episode); refuses any whose sha256 no longer matches."""
    loaded = {}
    for variant, pin in config["variants"].items():
        for index, episode in pin["episodes"].items():
            payload = read_gzip_json(eval_dir / episode["sentences"])
            require(digest(payload) == episode["sentences_sha256"],
                    f"pinned transcript sha256 mismatch for {variant} episode {index}; refusing to use it")
            loaded[(variant, int(index))] = (tuple(sentence_from(row) for row in payload["sentences"]),
                                             tuple(SilenceRegion(**row) for row in payload["silences"]))
    return loaded


def verify_pins(eval_dir: Path, config: dict, transcripts: dict, *, rerender: bool) -> dict:
    """Refuse on any pinned-input mismatch. Code drift is reported; it is fatal only when recording."""
    manifest = resolve(config["corpus"]["manifest"])
    require(sha256_file(manifest) == config["corpus"]["manifest_sha256"], f"corpus manifest changed: {manifest}")
    durations = {row["index"]: row["duration_seconds"] for row in config["episodes"]}
    rechecked = prompts_checked = 0
    for variant, pin in config["variants"].items():
        for index, episode in pin["episodes"].items():
            source = resolve(episode["asr_transcript"])
            if source.is_file():
                require(asr_transcript(source)[2] == episode["asr_transcript_sha256"],
                        f"ASR transcript no longer matches its pinned sha256; refusing: {source}")
                rechecked += 1
            sentences, silences = transcripts[(variant, int(index))]
            for render, prompt_sha256 in config["prompts"][variant][index].items():
                record = read_gzip_json(eval_dir / "prompts" / f"{prompt_sha256}.json.gz")
                require(digest(record) == prompt_sha256 and {key: record[key] for key in RENDERS[render]}
                        == config["renders"][render], f"prompt file does not match its address: {prompt_sha256}")
                if rerender:
                    require(digest(render_prompt(sentences, silences, durations[int(index)], render)) == prompt_sha256,
                            f"renderer output changed since {config['eval_id']} was pinned; record a new --eval-id")
                prompts_checked += 1
    return {
        "corpus_manifest_verified": True, "sentence_files_verified": True, "sentence_files": len(transcripts),
        "asr_transcripts_verified": True, "asr_transcripts_rechecked": rechecked,
        "asr_transcripts_pinned": sum(len(pin["episodes"]) for pin in config["variants"].values()),
        "prompt_files_verified": True, "prompt_files": prompts_checked, "prompts_rerendered": rerender,
        "joiner_source_matches_pin": sha256_file(Path(joiner.__file__)) == config["joiner"]["source_sha256"],
        "classifier_source_matches_pin": classifier_sources() == config["classifier"]["source_sha256"],
    }


def build_requests(config: dict, transcripts: dict) -> dict[tuple[str, int], ClassifyRequest]:
    episodes = {row["index"]: row for row in config["episodes"]}
    return {
        (variant, index): ClassifyRequest(
            sentences=sentences, silences=silences, episode_duration=episodes[index]["duration_seconds"],
            episode_title=episodes[index]["title"], podcast_title=episodes[index]["podcast_title"],
            language=config["variants"][variant]["language"])
        for (variant, index), (sentences, silences) in transcripts.items()
    }


def cells(config: dict) -> list[Cell]:
    return [Cell(variant, episode["index"], render, arm["label"], repeat)
            for variant in config["variants"] for episode in config["episodes"] for render in config["renders"]
            for arm in config["arms"] for repeat in range(1, config["repeats"] + 1)]


def arm_for(config: dict, label: str) -> dict:
    return next(arm for arm in config["arms"] if arm["label"] == label)


def cell_key(config: dict, cell: Cell) -> str:
    duration = next(row["duration_seconds"] for row in config["episodes"] if row["index"] == cell.episode)
    return digest({
        "namespace": KEY_NAMESPACE, "cell": dataclasses.asdict(cell), "arm": arm_for(config, cell.arm),
        "render": config["renders"][cell.render], "prompt_version": config["classifier"]["prompt_version"],
        "sentences_sha256": config["variants"][cell.variant]["episodes"][str(cell.episode)]["sentences_sha256"],
        "prompt_sha256": config["prompts"][cell.variant][str(cell.episode)][cell.render],
        "episode_duration": duration,
    })


def cassette_path(eval_dir: Path, key: str) -> Path:
    return eval_dir / "cassettes" / f"{key}.json"


def result_from_json(data: dict) -> ClassifyResult:
    return ClassifyResult(**{**data, "segments": [DetectedSegment(**row) for row in data["segments"]],
                             "usage": TokenUsage(**data["usage"])})


def check_result(result: ClassifyResult, config: dict, cell: Cell) -> None:
    arm, spec = arm_for(config, cell.arm), config["renders"][cell.render]
    require(result.provider == arm["provider"] and result.model == arm["model"],
            f"{cell.label()}: result came from {result.provider}:{result.model}")
    require(result.render_format == spec["transcript_format"] and result.include_silence == spec["include_silence"],
            f"{cell.label()}: classifier used render {result.render_format}/{result.include_silence}")
    require(result.prompt_version == config["classifier"]["prompt_version"],
            f"{cell.label()}: classifier used prompt {result.prompt_version}")


def write_cassette(eval_dir: Path, config: dict, cell: Cell, result: ClassifyResult) -> None:
    check_result(result, config, cell)
    key = cell_key(config, cell)
    cassette_path(eval_dir, key).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(cassette_path(eval_dir, key), {
        "schema_version": SCHEMA_VERSION, "cell_key": key, "cell": dataclasses.asdict(cell),
        "sentences_sha256": config["variants"][cell.variant]["episodes"][str(cell.episode)]["sentences_sha256"],
        "prompt_sha256": config["prompts"][cell.variant][str(cell.episode)][cell.render],
        "recorded_at": now_iso(), "result": dataclasses.asdict(result),
    })


def read_cassette(eval_dir: Path, config: dict, cell: Cell) -> tuple[ClassifyResult, dict]:
    key = cell_key(config, cell)
    path = cassette_path(eval_dir, key)
    require(path.is_file(), f"missing cassette for {cell.label()}: {path}; record it with --record")
    payload = load_json(path)
    require(payload.get("cell_key") == key and payload.get("cell") == dataclasses.asdict(cell),
            f"cassette {path.name} does not belong to {cell.label()}")
    result = result_from_json(payload["result"])
    check_result(result, config, cell)
    return result, {"cassette": str(path.relative_to(eval_dir)), "cassette_sha256": sha256_file(path)}


class RegistryClassifiers:
    """One ClassifierRegistry per render variant: transcript format and silence lines are Settings fields."""

    def __init__(self, config: dict, settings: Settings) -> None:
        self.config, self.settings, self.registries = config, settings, {}

    def __call__(self, render: str, arm: dict):
        if render not in self.registries:
            spec = self.config["renders"][render]
            registry = ClassifierRegistry(self.settings.with_overrides(
                transcript_format=spec["transcript_format"], include_silence=spec["include_silence"],
                prompt_version=self.config["classifier"]["prompt_version"],
                snap_to_silence=self.config["classifier"]["snap_to_silence"]))
            missing = sorted({item["provider"] for item in self.config["arms"]} - {
                provider for provider, ready in registry.available().items() if ready})
            require(not missing, f"no API key configured for {', '.join(missing)} (see secrets.env.example)")
            self.registries[render] = registry
        return self.registries[render].get(arm["provider"], arm["model"], arm["thinking"])

    async def aclose(self) -> None:
        for registry in self.registries.values():
            await registry.aclose()


async def record(eval_dir: Path, config: dict, requests: dict, classifiers, *, refresh: bool,
                 concurrency: int) -> list[str]:
    """Call the provider for cells lacking a cassette (all cells when refreshing); return failures."""
    groups: dict[tuple, list[Cell]] = {}
    for cell in cells(config):
        groups.setdefault((cell.variant, cell.episode, cell.render, cell.arm), []).append(cell)
    semaphore, failures = asyncio.Semaphore(concurrency), []

    async def run(group: list[Cell]) -> None:
        async with semaphore:
            # Repeats run in order, so provider-side prefix caching can serve the later ones.
            for cell in group:
                if cassette_path(eval_dir, cell_key(config, cell)).is_file() and not refresh:
                    continue
                classifier = classifiers(cell.render, arm_for(config, cell.arm))
                try:
                    result = await classifier.classify(requests[(cell.variant, cell.episode)])
                except ClassifierError as error:
                    failures.append(f"{cell.label()} and its later repeats: {error}")
                    return
                write_cassette(eval_dir, config, cell, result)
                print(f"Recorded {cell.label()}: {len(result.segments)} segments, {result.latency_ms} ms", flush=True)

    try:
        await asyncio.gather(*(run(group) for group in groups.values()))
    finally:
        await classifiers.aclose()
    return failures


def run_eval(eval_dir: Path, mode: str, *, create=None, classifiers=None, concurrency: int = 2) -> dict:
    """Pin (when creating), optionally record, then replay every cell into results.json."""
    config_path = eval_dir / "config.json"
    if config_path.is_file():
        config = load_json(config_path)
    else:
        require(mode != "replay" and create is not None, f"no pinned eval at {eval_dir}; create it with --record")
        config = create()
    transcripts = load_transcripts(eval_dir, config)
    verification = verify_pins(eval_dir, config, transcripts, rerender=mode != "replay")
    requests = build_requests(config, transcripts)
    if mode != "replay":
        require(verification["classifier_source_matches_pin"],
                f"classifier sources changed since {config['eval_id']} was pinned; record a new --eval-id")
        classifiers = classifiers or RegistryClassifiers(config, eval_settings())
        failures = asyncio.run(record(eval_dir, config, requests, classifiers, refresh=mode == "refresh",
                                      concurrency=concurrency))
        missing = sum(not cassette_path(eval_dir, cell_key(config, cell)).is_file() for cell in cells(config))
        require(not failures, f"{missing} cells have no cassette; rerun --record to retry them:\n" + "\n".join(failures))
    rows, request_hashes = [], {}
    for cell in cells(config):
        result, cassette = read_cassette(eval_dir, config, cell)
        group = (cell.variant, cell.episode, cell.render, cell.arm)
        require(request_hashes.setdefault(group, result.request_sha256) == result.request_sha256,
                f"{cell.label()}: repeats sent different requests, so they cannot measure LLM noise")
        request = requests[(cell.variant, cell.episode)]
        final = sanitize.finalize(result.segments, request, snap=config["classifier"]["snap_to_silence"])
        rows.append({
            "cell": dataclasses.asdict(cell), "cell_key": cell_key(config, cell), **cassette,
            "request_sha256": result.request_sha256, "raw_segments": [dataclasses.asdict(s) for s in result.segments],
            "segments": [dataclasses.asdict(s) for s in final], "usage": dataclasses.asdict(result.usage),
            "cost_usd": costs.cost_for(result.provider, result.model, result.usage).total_usd,
            "thinking": result.thinking, "latency_ms": result.latency_ms, "attempts": result.attempts,
            "chunk_count": result.chunk_count, "line_resolution": result.raw_response.get("line_resolution"),
        })
    results = {
        "schema_version": SCHEMA_VERSION, "eval_id": config["eval_id"], "config_sha256": sha256_file(config_path),
        "price_table_version": costs.PRICE_TABLE_VERSION, "sanitize_source_sha256": sha256_file(Path(sanitize.__file__)),
        "cells": rows,
    }
    atomic_json(eval_dir / "results.json", results)
    return results


def parse_variant(text: str) -> tuple[str, Path]:
    name, separator, run = text.partition("=")
    require(bool(separator and name and run), f"variant must be NAME=RUN_DIR: {text!r}")
    return name, resolve(run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record or replay the transcript-variant ad-segment eval.")
    parser.add_argument("--eval-id", required=True, help="directory name under benchmarks/evals/")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", dest="mode", action="store_const", const="record",
                      help="call providers for cells without a cassette")
    mode.add_argument("--refresh", dest="mode", action="store_const", const="refresh",
                      help="call providers for every cell and overwrite cassettes")
    mode.add_argument("--replay", dest="mode", action="store_const", const="replay",
                      help="never call a provider; fail on a missing cassette (default)")
    parser.add_argument("--concurrency", type=int, default=2, help="concurrent provider requests when recording")
    setup = parser.add_argument_group("setup (only when creating an eval)")
    setup.add_argument("--corpus", help=f"corpus directory with manifest.json (default {DEFAULT_CORPUS})")
    setup.add_argument("--variant", action="append", metavar="NAME=RUN_DIR",
                       help="ASR run with word timestamps; repeatable (default tiny.en and small.en TAL runs)")
    setup.add_argument("--reference", help=f"variant the others are compared against (default {DEFAULT_REFERENCE})")
    setup.add_argument("--arm", action="append", metavar="PROVIDER:MODEL[:THINKING]",
                       help="OpenRouter model preset; repeatable (default NOADCAST_OPENROUTER_MODEL)")
    setup.add_argument("--repeats", type=int, help="calls per cell (default 3)")
    setup.add_argument("--episodes", help="comma-separated corpus indexes (default: all)")
    parser.set_defaults(mode="replay")
    args = parser.parse_args()
    require(bool(EVAL_ID.fullmatch(args.eval_id)), "--eval-id must be 1-80 safe filename characters")
    require(args.concurrency >= 1, "--concurrency must be positive")
    eval_dir = EVALS_ROOT / args.eval_id
    options = (args.corpus, args.variant, args.reference, args.arm, args.repeats, args.episodes)
    require(not (eval_dir / "config.json").is_file() or all(value is None for value in options),
            f"{args.eval_id} is already pinned; setup options apply only when creating an eval")

    def create() -> dict:
        settings = eval_settings()
        return prepare(
            eval_dir, resolve(args.corpus or DEFAULT_CORPUS), dict(map(parse_variant, args.variant or DEFAULT_VARIANTS)),
            args.reference or DEFAULT_REFERENCE,
            [parse_arm(label) for label in args.arm or [f"openrouter:{settings.openrouter_model}"]], args.repeats or 3,
            [int(item) for item in args.episodes.split(",")] if args.episodes else None, settings)

    results = run_eval(eval_dir, args.mode, create=create, concurrency=args.concurrency)
    print(f"{args.mode}: {len(results['cells'])} cells replayed; wrote {eval_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
