"""Stage 3 backends: one interface, several wire formats.

`TranscriptionBackend` returns **bytes**, not a `Transcript`. The bytes are parsed by
`core.parse`, which is shared, because the payload is the same in every backend:

- whisper.cpp native `POST /inference` returns a **top-level array** of segments with a
  `words` list;
- shipboard's `--timestamps json` returns that same array, unchanged — it is a passthrough
  proxy, so there was never a second format to parse;
- an OpenAI-compatible server returns an **object** with `segments`, each carrying
  `words` in the same shape, *if* the server honours `timestamp_granularities`.

Keeping the interface at the byte level means a backend cannot quietly reshape timings on
the way in. Normalisation happens once, in one place, and is testable on its own.

**Word timings are the reason the interface exists.** Stages 5-7 bind frames to paragraphs
by word span. A backend that returns segments without words does not degrade gracefully —
it removes the mechanism the rest of the pipeline is built on, while every segment still
looks plausible. So coverage is measured after parsing and reported, never inferred from
an exit code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .. import runner

ENV_BACKEND = "GLIMPSE_STT"

DEFAULT_ENDPOINTS = {
    "whispercpp": "http://127.0.0.1:10302",
    "openai": "http://127.0.0.1:10302",
}


@dataclass(frozen=True)
class BackendInfo:
    """What `doctor` prints and what the stage report records."""

    name: str
    target: str
    detail: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class TranscriptionBackend(Protocol):
    """Transcribe one 16 kHz mono wav. Return the raw payload; do not parse it."""

    name: str

    def info(self) -> BackendInfo:
        """Where this backend points, for `doctor` and the stage report."""
        ...

    def check(self) -> None:
        """Raise runner.DependencyError unless this backend is usable *now*.

        Must be able to distinguish "not configured" from "configured and unreachable".
        `shutil.which` cannot: it says a binary exists and nothing about its backend.
        """
        ...

    def run(self, wav, *, timeout: float) -> bytes:
        """Transcribe and return the raw payload."""
        ...


def _env_endpoint(name: str) -> str:
    return os.environ.get(f"GLIMPSE_{name.upper()}_URL", DEFAULT_ENDPOINTS[name]).rstrip("/")


def resolve(name: str | None = None) -> TranscriptionBackend:
    """Pick a backend. `GLIMPSE_STT` wins; otherwise whisper.cpp native, then shipboard.

    The default is deliberately an HTTP backend rather than a subprocess. shipboard's only
    remaining advantage is that it needs no configuration on this host, and it answers with
    the identical payload -- so it is the fallback, not the default.
    """
    from . import openai_compat, shipboard, whispercpp

    chosen = name or os.environ.get(ENV_BACKEND) or "auto"
    if chosen == "auto":
        for factory in (whispercpp.HttpWhisperCpp, shipboard.Shipboard):
            backend = factory()
            if backend.available():
                return backend
        raise runner.DependencyError(
            name="stt",
            code=2,
            message=(
                "no transcription backend is reachable; set GLIMPSE_STT to one of "
                "whispercpp, openai, shipboard"
            ),
            remediation=(
                "start whisper.cpp and set GLIMPSE_WHISPERCPP_URL, or install shipboard, "
                "or configure an OpenAI-compatible endpoint"
            ),
        )

    table: dict[str, type] = {
        "whispercpp": whispercpp.HttpWhisperCpp,
        "whisper.cpp": whispercpp.HttpWhisperCpp,
        "openai": openai_compat.OpenAICompat,
        "openai-compatible": openai_compat.OpenAICompat,
        "shipboard": shipboard.Shipboard,
    }
    if chosen not in table:
        raise runner.DependencyError(
            name="stt",
            code=1,
            message=f"unknown STT backend {chosen!r}",
            remediation=f"GLIMPSE_STT must be one of: {', '.join(sorted(table))}",
        )
    return table[chosen]()


__all__ = [
    "BackendInfo",
    "ENV_BACKEND",
    "TranscriptionBackend",
    "resolve",
]
