"""Configuration loading. One YAML file, one dotted-access helper."""
from __future__ import annotations

import random
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config(dict):
    """dict with attribute access, so cfg.crawl.delay_ms reads naturally."""

    def __getattr__(self, name):
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return Config(value) if isinstance(value, dict) else value


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh))


def resolve(relative: str) -> Path:
    """Project-relative path -> absolute, creating the parent directory."""
    p = ROOT / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def sleep_span(delay_ms) -> float:
    """Randomised politeness delay, in seconds."""
    lo, hi = delay_ms
    return random.uniform(lo, hi) / 1000.0
