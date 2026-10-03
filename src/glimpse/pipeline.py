"""Stage orchestration for `glimpse process`.

Stages 1-4 live here. Stage 0 (`doctor`) runs in the CLI before this is called,
because its output is a rendered report rather than a pipeline artefact, and
stages 5-12 are not built yet.

**Progress is reported as stage lines, and the unbuilt stages are named.** D7's
reason for stage-level progress is that shipboard issues one blocking HTTP
request with no streaming, and a bar that reads 90% during a 23-minute transcribe
and then sits still is a lie about where the time went. The same argument applies
to the stages that do not exist yet: a run that stops after stage 3 and reports
"done" would misrepresent a partial pipeline as a finished one. So the last line
of every run states what is still missing and which issue tracks it.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import audio, audit, caption, frames, lint, probe, quality, repair, stages, stt, synth
from . import llm
from .bundle import Bundle
from .workspace import WorkDir

# Re-exported, not defined here. `quality` and `caption` write progress lines that need the
# same count, and they cannot import this module to ask -- `pipeline` imports them, so the
# dependency would be a cycle. `stages` holds it instead.
IMPLEMENTED = stages.IMPLEMENTED
FIRST_STAGE = stages.FIRST_STAGE
REMAINING_NOTE = stages.REMAINING_NOTE

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
    frames: list[Path]
    artefacts: dict[str, Path]
    reports: tuple[StageReport, ...]
    bundle: Bundle
    #: D4's verdict. `quality.ok` false means exit 4, reported by the caller.
    quality: quality.Report
    enhanced: list[Path]
    #: D5's coverage verdict. `caption.ok` false means the audio and the video are not
    #: describing the same lecture, which is exit 4 rather than a note with holes in it.
    caption: caption.Report
    #: The note. `synth.degraded` is reported, not hidden: a note written by the template
    #: synthesizer is a skeleton, and a reader has to be able to tell.
    note: synth.Note
    #: D5's deterministic gate on the note. `lint.ok` false is exit 4.
    lint: lint.Report
    #: D5's audit. `audit.ok` false is exit 5: the note is written and annotated, never
    #: discarded. An audit that could not run says so; it never reads as one that passed.
    audit: audit.Report
    #: What stage 10 could fix, and what it declined to.
    repair: repair.Report

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


def run(
    source: Path,
    work: WorkDir,
    *,
    bundle: Bundle | None = None,
    stream=None,
    note_title: str | None = None,
    glossary_path: str | None = None,
) -> RunResult:
    """Stages 1-4 on `source`.

    Intermediate artefacts land in `work`, which is scratch and is removed on success.
    Anything worth keeping is published into `bundle` as it is produced, so deleting the
    work dir can never take a deliverable with it -- and so the reverse cannot happen
    either, a deliverable written while the scratch dir is left behind.

    Raises runner.DependencyError for a missing or failed dependency, and
    runner.DependencyError with code 1 for an input the pipeline cannot use.
    Both are reported by the caller, which owns exit codes and rendering.
    """
    out = stream if stream is not None else sys.stdout
    bundle = bundle if bundle is not None else Bundle.open(source, overwrite=False)
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
        f"  [3/{IMPLEMENTED}] {'stt'.ljust(7)} starting  {artefact.duration:.0f}s of audio through "
        f"whisper.cpp on CPU, expect {_interval(lo, hi)}, no streaming (D7)\n"
    )
    out.flush()
    transcript = stt.transcribe(wav, audio_duration=artefact.duration)
    paths = stt.write(transcript, work.path)
    if bundle is not None:
        for key, path in paths.items():
            paths[key] = bundle.publish(key, path)
    reports.append(StageReport(3, "stt", time.monotonic() - began, transcript.summary()))
    out.write(reports[-1].line() + "\n")

    for anomaly in transcript.anomalies[:10]:
        out.write(f"           note: {anomaly}\n")
    if len(transcript.anomalies) > 10:
        out.write(f"           note: ... and {len(transcript.anomalies) - 10} more\n")

    # --- stage 4: frames ------------------------------------------------------
    # D3: frames are read from the source, never from a blurred intermediate. The
    # detect pass decodes the whole container and writes only timestamps, so the
    # blur cannot reach the output; the extract pass then seeks the original.
    began = time.monotonic()
    frames_dir = work.dir("frames")
    manifest, produced = frames.run(Path(source), frames_dir)
    reports.append(
        StageReport(4, "frames", time.monotonic() - began, frames.summary(manifest, produced))
    )
    out.write(reports[-1].line() + "\n")

    artefacts = dict(paths)
    artefacts["manifest"] = bundle.publish("manifest", manifest)
    # Frames go to the bundle's images/ so the note can reference them by a stable
    # relative path, and the manifest travels with them.
    #
    # The move happens *after* stage 5. It used to happen before, which handed stage 5 a
    # list of paths that no longer existed: all 16 frames failed with "cannot read the
    # frame size". The unit tests could not see it because the stubbed pipeline never
    # moves anything -- only the end-to-end run did.
    for extra in ("detect.json",):
        candidate = frames_dir / extra
        if candidate.is_file():
            artefacts[extra] = bundle.publish(extra, candidate)

    # --- stage 5: quality -----------------------------------------------------
    # D4. Runs on the frames stage 4 produced, not on the source, so the gate judges
    # what stage 6 will actually caption. D4's exit-4 path is signalled by
    # `quality_ok` rather than raised here: raising would skip the publishing of the
    # enhanced frames, and a failed gate is exactly when those frames are worth
    # looking at.
    began = time.monotonic()
    qs = quality.Settings()
    bbox_source = os.environ.get("GLIMPSE_BBOX_SOURCE", "geometric")
    quality.resolve_estimator(bbox_source)
    qreport = quality.run(produced, work.path, settings=qs, bbox_source=bbox_source, stream=out)

    # Publish the gated frames only now, so stage 5 read real files.
    for index, path in enumerate(produced):
        target = bundle.images / path.name
        shutil.move(str(path), str(target))
        bundle.record(f"frame{index}", target)
        artefacts[f"frame{index}"] = target
    reports.append(StageReport(5, "quality", time.monotonic() - began, qreport.summary()))

    qdir = work.dir("quality")
    quality.write(qreport, qdir / quality.REPORT_NAME, qs)
    artefacts[quality.REPORT_NAME] = bundle.publish(quality.REPORT_NAME, qdir / quality.REPORT_NAME)
    (qdir / quality.PROVENANCE_NAME).write_text(
        quality.provenance(qs, bbox_source) + "\n", encoding="utf-8"
    )
    artefacts[quality.PROVENANCE_NAME] = bundle.publish(
        quality.PROVENANCE_NAME, qdir / quality.PROVENANCE_NAME
    )

    enhanced: list[Path] = []
    for entry in qreport.passed:
        candidate = work.path / "enhanced" / quality.enhanced_name(entry.name)
        if not candidate.is_file():
            continue
        target = bundle.images / candidate.name
        shutil.move(str(candidate), str(target))
        bundle.record(f"quality:{entry.name}", target)
        artefacts[f"quality:{entry.name}"] = target
        enhanced.append(target)

    # --- stage 6: caption -----------------------------------------------------
    # Deterministic. Runs on the frames stage 5 passed and the transcript stage 3 wrote;
    # there is no model in this stage, by design, so the alignment can be checked against
    # the artefact rather than believed.
    began = time.monotonic()
    capdir = work.dir("caption")
    creport = caption.run(
        # The bundle's copies, not the work dir's. `publish` moved both, so the work-dir
        # paths are dangling -- the same move-then-read ordering bug stage 5 had.
        artefacts["manifest"],
        paths["json"],
        qdir / quality.REPORT_NAME,
        bundle.images,
        capdir,
        stream=out,
    )
    for extra in (caption.REPORT_NAME, caption.PROVENANCE_NAME):
        artefacts[extra] = bundle.publish(extra, capdir / extra)
    reports.append(StageReport(6, "caption", time.monotonic() - began, creport.summary()))

    # --- stage 7: synth ------------------------------------------------------
    # The first stage that may need a model. Two implementations of one Protocol; the
    # template one runs with nothing configured, so an unavailable endpoint costs the note's
    # quality rather than the note itself. Which one ran is in provenance, not in prose.
    began = time.monotonic()
    sdir = work.dir("synth")
    try:
        llm_config = llm.Config.from_env()
    except llm.NotConfiguredError:
        llm_config = None
    note = synth.run(
        artefacts["captions.json"],
        artefacts["txt"],
        Path(source),
        sdir,
        config=llm_config,
        title=note_title,
        stream=out,
    )
    for extra in (synth.NOTE_NAME, synth.REPORT_NAME, synth.PROVENANCE_NAME):
        artefacts[extra] = bundle.publish(extra, sdir / extra)
    reports.append(
        StageReport(
            7,
            "synth",
            time.monotonic() - began,
            f"{note.synthesizer}, {note.markdown.count(chr(10))} lines",
        )
    )

    # --- stage 8: lint --------------------------------------------------------
    # Deterministic, and every rule is a delimiter count or a string match. A model asked
    # whether `$...$` is balanced is strictly worse than a counter, so nothing here is one.
    began = time.monotonic()
    ldir = work.dir("lint")
    lreport = lint.run(artefacts[synth.NOTE_NAME], bundle.images, ldir, stream=out)
    for extra in (lint.REPORT_NAME, lint.PROVENANCE_NAME):
        artefacts[extra] = bundle.publish(extra, ldir / extra)
    reports.append(StageReport(8, "lint", time.monotonic() - began, lreport.summary()))

    # --- stage 9: audit --------------------------------------------------------
    # Tier 1 is deterministic and carries the load. Tier 2 is a zero-shot critic that sees
    # the note and nothing else -- a model reviewing its own output in the same context
    # ratifies the error it is shown.
    began = time.monotonic()
    adir = work.dir("audit")
    areport = audit.run(
        artefacts[synth.NOTE_NAME],
        artefacts["txt"],
        adir,
        glossary_path=Path(glossary_path) if glossary_path else None,
        config=llm_config,
        stream=out,
    )
    for extra in audit.ALL_ARTEFACTS:
        if (adir / extra).is_file():
            artefacts[f"audit/{extra}"] = bundle.publish(f"audit/{extra}", adir / extra)
    reports.append(StageReport(9, "audit", time.monotonic() - began, areport.summary()))

    # --- stage 10: repair -----------------------------------------------------
    began = time.monotonic()
    rdir = work.dir("repair")
    rreport = repair.run(
        artefacts[synth.NOTE_NAME],
        adir / audit.REPORT_NAME,
        rdir,
        glossary_path=Path(glossary_path) if glossary_path else None,
        lint_report_path=artefacts[lint.REPORT_NAME],
        stream=out,
    )
    for extra in (repair.REPAIRED_NOTE, repair.REPORT_NAME, repair.PROVENANCE_NAME):
        artefacts[f"repair/{extra}"] = bundle.publish(f"repair/{extra}", rdir / extra)
    reports.append(StageReport(10, "repair", time.monotonic() - began, rreport.summary()))

    return RunResult(
        source=Path(source),
        info=info,
        audio=artefact,
        transcript=transcript,
        frames=[artefacts[f"frame{i}"] for i in range(len(produced))],
        artefacts=artefacts,
        reports=tuple(reports),
        bundle=bundle,
        quality=qreport,
        enhanced=enhanced,
        caption=creport,
        note=note,
        lint=lreport,
        audit=areport,
        repair=rreport,
    )
