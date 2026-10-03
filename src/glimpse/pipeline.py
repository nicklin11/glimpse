"""Stage orchestration for `glimpse process`.

Stages 1-3 live here. Stage 0 (`doctor`) runs in the CLI before this is called,
because its output is a rendered report rather than a pipeline artefact, and
stages 4-12 are not built yet.

**Progress is reported as stage lines, and the unbuilt stages are named.** D7's
reason for stage-level progress is that shipboard issues one blocking HTTP
request with no streaming, and a bar that reads 90% during a 23-minute transcribe
and then sits still is a lie about where the time went. The same argument applies
to the stages that do not exist yet: a run that stops after stage 3 and reports
"done" would misrepresent a partial pipeline as a finished one. So the last line
of every run states what is still missing and which issue tracks it.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import audio, probe, stt
from .workspace import WorkDir

# Stage 0 is `doctor`, run by the CLI. Stages 4-12 are unbuilt (#3).
IMPLEMENTED = 3
FIRST_STAGE = 1
REMAINING_NOTE = (
    "stages 4-12 are not built yet (tracked in #3): frames, quality, "
    "captions, synth, lint, audit, repair, link, report"
)

# Multiplier on the upper bound of the stage-3 estimate. An interval that can
# collapse to a single number reads as a precision claim nobody has measured.
HEADROOM = 1.5


@dataclass(frozen=True)
class StageReport:
    index: int
    name: str
    seconds: float
    summary: str

    def line(self) -> str:
        return f"  [{self.index}/{IMPLEMENTED}] {self.name.ljust(7)} {self.seconds:7.1f}s  {self.summary}"


@dataclass(frozen=True)
class RunResult:
    source: Path
    info: probe.MediaInfo
    audio: audio.AudioArtefact
    transcript: stt.Transcript
    artefacts: dict[str, Path]
    reports: tuple[StageReport, ...]

    @property
    def seconds(self) -> float:
        return sum(r.seconds for r in self.reports)


def _interval(lo: float, hi: float) -> str:
    """One unit for the whole interval.

    Choosing per endpoint produced "15s-2min", which reads as two unrelated
    numbers. The unit is picked from the upper bound, so both ends agree.
    """
    if hi < 90:
        return f"{lo:.0f}s-{hi:.0f}s"
    # max(1, ...) because a 30 s bound still reads as "0min" on the low end.
    return f"{max(lo / 60, 0.1):.1f}min-{hi / 60:.0f}min"


def _estimate(audio_seconds: float) -> tuple[float, float]:
    """Wall-clock estimate for stage 3, in seconds.

    Short inputs are not faster per second of audio -- they are *slower*,
    because the whisper.cpp warm-up is a fixed cost that a long lecture
    amortises away. Measured here: 25 s of audio took 4.9 s (0.20x), 4520 s took
    257.2 s (0.057x). So below WARMUP_SECONDS the floor is the slow end; using
    the fast end there would print a lower bound 3.9x below the only measurement
    taken at that length, which is the D7 failure mode exactly.

    HEADROOM widens the upper bound so the interval is never degenerate: with
    the floor already at the slow end, a short clip would otherwise print the
    same number twice ("expect 5s-5s"), which reads as a claim of precision
    nobody has.
    """
    fast, slow = stt.RATE_RANGE
    floor = slow if audio_seconds < stt.WARMUP_SECONDS else fast
    return audio_seconds * floor, audio_seconds * slow * HEADROOM


def run(source: Path, work: WorkDir, *, stream=None) -> RunResult:
    """Stages 1-3 on `source`, writing artefacts into `work`.

    Raises runner.DependencyError for a missing or failed dependency, and
    runner.DependencyError with code 1 for an input the pipeline cannot use.
    Both are reported by the caller, which owns exit codes and rendering.
    """
    out = stream if stream is not None else sys.stdout
    reports: list[StageReport] = []

    # --- stage 1: probe -------------------------------------------------------
    began = time.monotonic()
    info = probe.probe(Path(source))
    probe.require_supported(info)
    reports.append(StageReport(1, "probe", time.monotonic() - began, info.summary()))
    out.write(reports[-1].line() + "\n")

    # --- stage 2: audio -------------------------------------------------------
    began = time.monotonic()
    wav = work.sub("audio.wav")
    artefact = audio.extract(Path(source), wav)
    summary = artefact.summary()
    warning = audio.check_coverage(info, artefact)
    if warning:
        summary += f"  [warn] {warning}"
    reports.append(StageReport(2, "audio", time.monotonic() - began, summary))
    out.write(reports[-1].line() + "\n")

    # --- stage 3: stt ---------------------------------------------------------
    began = time.monotonic()
    # D7: shipboard issues one blocking HTTP request with no streaming, so there
    # is nothing to report between here and the result line. Stating the
    # expected cost *before* starting is the only honest option; a bar that sits
    # silent through the dominant stage is the lie D7 is written against.
    lo, hi = _estimate(artefact.duration)
    out.write(
        f"  [3/3] {'stt'.ljust(7)} starting  {artefact.duration:.0f}s of audio through "
        f"whisper.cpp on CPU, expect {_interval(lo, hi)}, no streaming (D7)\n"
    )
    out.flush()
    transcript = stt.transcribe(wav, audio_duration=artefact.duration)
    paths = stt.write(transcript, work.path)
    reports.append(StageReport(3, "stt", time.monotonic() - began, transcript.summary()))
    out.write(reports[-1].line() + "\n")

    for anomaly in transcript.anomalies[:10]:
        out.write(f"           note: {anomaly}\n")
    if len(transcript.anomalies) > 10:
        out.write(f"           note: ... and {len(transcript.anomalies) - 10} more\n")

    return RunResult(
        source=Path(source),
        info=info,
        audio=artefact,
        transcript=transcript,
        artefacts=paths,
        reports=tuple(reports),
    )
