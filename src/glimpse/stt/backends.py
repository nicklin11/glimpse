"""Stage 3 backends: one interface, several wire formats.

`TranscriptionBackend` returns **bytes**, not a `Transcript`. The bytes are parsed by
`core.parse`, which is shared, because the payload is the same in every backend:

- whisper.cpp native `POST /inference` returns a **top-level object** (`verbose_json`) with a
  `segments` array, each segment carrying a `words` list;
- an OpenAI-compatible server returns the same shape, *if* the server honours
  `timestamp_granularities`.

Keeping the interface at the byte level means a backend cannot quietly reshape timings on
the way in. Normalisation happens once, in one place, and is testable on its own.

Both remaining backends are HTTP endpoints. There is no subprocess backend: the one there was
— shipboard — added nothing this code did not already do (`core.parse` unwraps
`segments` either way) and imposed a 300 s ceiling it could not lift. See issue #49, which
also corrects an earlier claim that the two payloads were byte-identical. They are not:
whisper.cpp disagrees with **itself** run to run.

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
    """Pick a backend. `GLIMPSE_STT` wins; otherwise whisper.cpp native, then OpenAI.

    Both candidates are HTTP endpoints. `auto` probes them in that order and takes the first
    that answers, because a local whisper.cpp keeps the audio on the machine and a configured
    OpenAI-compatible endpoint does not -- so the local one is preferred when both are up.
    """
    from . import openai_compat, whispercpp

    chosen = name or os.environ.get(ENV_BACKEND) or "auto"
    if chosen == "auto":
        for factory in (whispercpp.HttpWhisperCpp, openai_compat.OpenAICompat):
            backend = factory()
            if backend.available():
                return backend
        raise runner.DependencyError(
            name="stt",
            code=2,
            message="no transcription backend is reachable; set GLIMPSE_STT to one of "
            "whispercpp, openai",
            remediation=(
                "start whisper.cpp and set GLIMPSE_WHISPERCPP_URL, or point "
                "GLIMPSE_OPENAI_URL at an OpenAI-compatible endpoint"
            ),
        )

    table: dict[str, type] = {
        "whispercpp": whispercpp.HttpWhisperCpp,
        "whisper.cpp": whispercpp.HttpWhisperCpp,
        "openai": openai_compat.OpenAICompat,
        "openai-compatible": openai_compat.OpenAICompat,
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
