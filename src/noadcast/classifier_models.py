"""Supported classification presets (dependency-free for spawned workers)."""

from typing import Literal

ClassifierModel = Literal["deepseek/deepseek-v4.1-flash", "qwen/qwen3.8-flash", "openai/gpt-6-luna"]
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
MODEL_IDS = (DEFAULT_MODEL, "qwen/qwen3.8-flash", "openai/gpt-6-luna")
OPENAI_DEFAULT_MODEL = "gpt-6-luna"
OPENAI_MODEL_IDS = (OPENAI_DEFAULT_MODEL,)
PROVIDER_MODEL_IDS = {"openrouter": MODEL_IDS, "openai": OPENAI_MODEL_IDS}


def thinking_for_model(model: str) -> str | None:
    if model == OPENAI_DEFAULT_MODEL:
        return "xhigh"
    return "high" if model == "openai/gpt-6-luna" else None
