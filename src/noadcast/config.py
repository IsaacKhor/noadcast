"""Server configuration.

Values come from, in increasing precedence: built-in defaults, a
``secrets.env`` file (``KEY=VALUE`` lines), and the process environment.
Secrets such as API keys live only here and never leave the server.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import stat
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Mapping

from .classifier_models import DEFAULT_MODEL, MODEL_IDS

log = logging.getLogger(__name__)

GIB = 1024**3


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. ``#`` comments, blank lines, optional quotes; no interpolation."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum():
            raise ValueError(f"{path}:{lineno}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _optional(value: str) -> str | None:
    return value or None


@dataclass(frozen=True)
class Settings:
    # Storage and network
    data_dir: Path = Path("data")
    # Comma-separated bind addresses. Loopback by default; add the tailnet
    # address (e.g. 100.80.188.91) to serve the phone. Never 0.0.0.0.
    hosts: tuple[str, ...] = ("127.0.0.1",)
    port: int = 44007

    # Auth
    api_token: str | None = None
    allow_no_auth: bool = False
    signing_secret: bytes = b""
    audio_url_ttl_seconds: int = 12 * 3600

    # Classification
    classifier: str = "openrouter"
    openrouter_model: str = DEFAULT_MODEL
    prompt_version: str = "segments-v3"
    transcript_format: str = "sentences"  # production: [22.24-23.88] A complete sentence.
    include_silence: bool = False  # older evaluation formats may interleave silence rows
    snap_to_silence: bool = True
    classifier_max_input_tokens: int = 120_000
    openrouter_api_key: str | None = field(default=None, repr=False)
    openrouter_api_base: str = "https://openrouter.ai/api/v1"

    # Transcription
    asr_model_dir: Path = Path("data/models/tiny.en")
    asr_model_id: str = "Systran/faster-whisper-tiny.en"
    asr_language: str | None = "en"
    # GPU defaults from benchmarks/tal/GPU.md: tiny.en saturates a TITAN V at
    # 4 workers x batch 32 (~950x real time with word timestamps). For CPU-only
    # hosts use device cpu, 6 workers x 2 threads, batch 8 (int8 by default).
    asr_device: str = "cuda"  # cuda | cpu
    asr_compute_type: str | None = None  # None: float16 on cuda, int8 on cpu
    pool_workers: int = 4
    pool_threads: int = 4
    pool_pin: bool = False
    word_timestamps: bool = True
    asr_batch_size: int = 32
    asr_beam_size: int = 5

    # Pipeline concurrency and feed policy
    download_concurrency: int = 4
    download_per_host: int = 2
    classify_concurrency: int = 2
    refresh_concurrency: int = 3
    feed_interval_minutes: int = 30
    initial_backfill: int = 1
    new_episode_max_age_days: int = 30
    max_admits_per_refresh: int = 5
    max_feed_bytes: int = 10 * 1024**2
    max_audio_bytes: int = 3 * GIB

    # Retention: audio is deleted when the client reports an episode played;
    # these sweeps bound disk for episodes that are never played.
    keep_unplayed_days: int = 60
    min_free_bytes: int = 20 * GIB
    audio_cache_max_bytes: int = 150 * GIB
    evicted_redirect: bool = False
    tombstone_retention_days: int = 90

    # Logging
    log_level: str = "info"
    log_format: str = "json"  # json | text

    @property
    def db_path(self) -> Path:
        return self.data_dir / "noadcast.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def llm_dir(self) -> Path:
        return self.data_dir / "llm"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def auth_enabled(self) -> bool:
        return not self.allow_no_auth

    def with_overrides(self, **changes) -> "Settings":
        return replace(self, **changes)

    @classmethod
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        secrets_path: Path | None = None,
    ) -> "Settings":
        """Build settings from ``secrets.env`` (if present) overlaid by ``env``."""
        environ = dict(os.environ if env is None else env)
        path = secrets_path or Path(environ.get("NOADCAST_SECRETS_FILE", "secrets.env"))
        merged: dict[str, str] = {}
        if path.is_file():
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                log.warning("secrets file %s is group/world accessible; chmod 600 it", path)
            merged.update(parse_env_file(path))
        merged.update(environ)
        return cls.from_mapping(merged)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "Settings":
        def get(name: str) -> str | None:
            value = values.get(name)
            return None if value is None else value.strip()

        kwargs: dict[str, object] = {}
        simple = {
            "NOADCAST_PORT": ("port", int),
            "NOADCAST_ALLOW_NO_AUTH": ("allow_no_auth", _bool),
            "NOADCAST_AUDIO_URL_TTL_SECONDS": ("audio_url_ttl_seconds", int),
            "NOADCAST_CLASSIFIER": ("classifier", str),
            "NOADCAST_OPENROUTER_MODEL": ("openrouter_model", str),
            "NOADCAST_PROMPT_VERSION": ("prompt_version", str),
            "NOADCAST_TRANSCRIPT_FORMAT": ("transcript_format", str),
            "NOADCAST_INCLUDE_SILENCE": ("include_silence", _bool),
            "NOADCAST_SNAP_TO_SILENCE": ("snap_to_silence", _bool),
            "NOADCAST_CLASSIFIER_MAX_INPUT_TOKENS": ("classifier_max_input_tokens", int),
            "OPENROUTER_API_KEY": ("openrouter_api_key", _optional),
            "OPENROUTER_API_BASE": ("openrouter_api_base", str),
            "NOADCAST_ASR_MODEL_DIR": ("asr_model_dir", Path),
            "NOADCAST_ASR_MODEL_ID": ("asr_model_id", str),
            "NOADCAST_ASR_LANGUAGE": ("asr_language", _optional),
            "NOADCAST_ASR_DEVICE": ("asr_device", str),
            "NOADCAST_ASR_COMPUTE_TYPE": ("asr_compute_type", _optional),
            "NOADCAST_POOL_WORKERS": ("pool_workers", int),
            "NOADCAST_POOL_THREADS": ("pool_threads", int),
            "NOADCAST_POOL_PIN": ("pool_pin", _bool),
            "NOADCAST_WORD_TIMESTAMPS": ("word_timestamps", _bool),
            "NOADCAST_ASR_BATCH_SIZE": ("asr_batch_size", int),
            "NOADCAST_ASR_BEAM_SIZE": ("asr_beam_size", int),
            "NOADCAST_DOWNLOAD_CONCURRENCY": ("download_concurrency", int),
            "NOADCAST_DOWNLOAD_PER_HOST": ("download_per_host", int),
            "NOADCAST_CLASSIFY_CONCURRENCY": ("classify_concurrency", int),
            "NOADCAST_REFRESH_CONCURRENCY": ("refresh_concurrency", int),
            "NOADCAST_FEED_INTERVAL_MINUTES": ("feed_interval_minutes", int),
            "NOADCAST_INITIAL_BACKFILL": ("initial_backfill", int),
            "NOADCAST_NEW_EPISODE_MAX_AGE_DAYS": ("new_episode_max_age_days", int),
            "NOADCAST_MAX_ADMITS_PER_REFRESH": ("max_admits_per_refresh", int),
            "NOADCAST_MAX_FEED_BYTES": ("max_feed_bytes", int),
            "NOADCAST_MAX_AUDIO_BYTES": ("max_audio_bytes", int),
            "NOADCAST_KEEP_UNPLAYED_DAYS": ("keep_unplayed_days", int),
            "NOADCAST_MIN_FREE_BYTES": ("min_free_bytes", int),
            "NOADCAST_AUDIO_CACHE_MAX_BYTES": ("audio_cache_max_bytes", int),
            "NOADCAST_EVICTED_REDIRECT": ("evicted_redirect", _bool),
            "NOADCAST_TOMBSTONE_RETENTION_DAYS": ("tombstone_retention_days", int),
            "NOADCAST_LOG_LEVEL": ("log_level", str),
            "NOADCAST_LOG_FORMAT": ("log_format", str),
        }
        for env_name, (attr, convert) in simple.items():
            raw = get(env_name)
            if raw is not None:
                kwargs[attr] = convert(raw)

        data_dir = Path(get("NOADCAST_DATA_DIR") or "data")
        kwargs["data_dir"] = data_dir
        if get("NOADCAST_ASR_MODEL_DIR") is None:
            kwargs["asr_model_dir"] = data_dir / "models" / "tiny.en"
        hosts = get("NOADCAST_HOST")
        if hosts:
            kwargs["hosts"] = tuple(h.strip() for h in hosts.split(",") if h.strip())
        token = get("NOADCAST_API_TOKEN") or None
        kwargs["api_token"] = token
        secret = get("NOADCAST_SIGNING_SECRET")
        if secret:
            kwargs["signing_secret"] = secret.encode()
        elif token:
            # Derived so a token rotation also invalidates outstanding audio URLs.
            kwargs["signing_secret"] = hmac.new(
                token.encode(), b"noadcast-audio-url-v1", hashlib.sha256
            ).digest()
        settings = cls(**kwargs)
        settings.validate()
        return settings

    def validate(self) -> None:
        if "0.0.0.0" in self.hosts or "::" in self.hosts:
            raise ValueError("refusing to bind all interfaces; list loopback and tailnet addresses explicitly")
        if self.auth_enabled:
            if not self.api_token:
                raise ValueError("NOADCAST_API_TOKEN is required (or set NOADCAST_ALLOW_NO_AUTH=1 for local testing)")
            if len(self.api_token) < 32:
                raise ValueError("NOADCAST_API_TOKEN must be at least 32 characters")
        if self.classifier != "openrouter":
            raise ValueError(f"unknown NOADCAST_CLASSIFIER {self.classifier!r}")
        if self.openrouter_model not in MODEL_IDS:
            raise ValueError(f"unsupported NOADCAST_OPENROUTER_MODEL {self.openrouter_model!r}; expected one of {MODEL_IDS}")
        if self.transcript_format not in {"index", "seconds", "sentences"}:
            raise ValueError(f"unknown NOADCAST_TRANSCRIPT_FORMAT {self.transcript_format!r}")
        if self.transcript_format == "sentences" and self.include_silence:
            raise ValueError("NOADCAST_INCLUDE_SILENCE is incompatible with the sentences format")
        if self.asr_device not in {"cuda", "cpu"}:
            raise ValueError(f"unknown NOADCAST_ASR_DEVICE {self.asr_device!r}")
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, int) and not isinstance(value, bool) and value < 0:
                raise ValueError(f"{f.name} must not be negative")
        if self.pool_workers < 1 or self.pool_threads < 1:
            raise ValueError("pool workers and threads must be positive")
        if not 1 <= self.feed_interval_minutes <= 1440:
            raise ValueError("NOADCAST_FEED_INTERVAL_MINUTES must be 1..1440")


def settings_for_tests(data_dir: Path, **overrides) -> Settings:
    """Settings with auth disabled and everything under ``data_dir``."""
    base = Settings(
        data_dir=data_dir,
        allow_no_auth=True,
        signing_secret=b"test-signing-secret",
        asr_model_dir=data_dir / "models" / "tiny.en",
        log_format="text",
    )
    return replace(base, **overrides)
