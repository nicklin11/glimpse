"""Stage 12: report. Assemble the deliverable set and verify it on disk.

This stage is the reason `process` can exit 0 at all. Exit 0 in ADR-0001's table means
"all artefacts written and verified", and the second half of that is the whole point of
this module: **verification is a read of the filesystem, never an inference from an exit
code.** D3 records the run that produced zero frames and exited successfully — ffmpeg wrote
an empty file, ffmpeg exited 0, and the pipeline had nothing to check. So stage 12 stats
every artefact it expects and counts what is actually there.

## What it does, in order

1. **Promotes the final note.** Stages 10 and 11 each write their own file
   (`note.repaired.md`, `note.linked.md`). The deliverable is the last one. It is published
   as `note.md` so that both the bundle and any vault export carry the finished note under
   the one name a reader expects — and stage 7's original is preserved as `note.synth.md`,
   because "the note as stage 7 wrote it" is evidence about what the synthesizer did, and
   throwing it away to save a filename would destroy the only record of it.
2. **Verifies the required set.** Each entry states why it is required, so a failure names
   the reason instead of a filename.
3. **Counts frames against the manifest** when the source had video. A manifest listing 18
   timestamps and 17 files on disk is a failure, not a rounding difference.
4. **Records what is absent and why.** Optional artefacts — the tier-2 LLM transcript, the
   link reports — are reported as absent when they are legitimately absent. A verification
   that lists everything missing looks identical whether the thing is optional or broken.

## The vault is not touched here

D9, as amended 2026-10-03: the vault is an export target, not the artefact root. This
stage writes into the bundle and nowhere else. `Bundle.export_to_vault` performs the copy
when `--vault-path` was given, and verifies each destination against its source.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import audit, frames, lint, link, quality, repair, stages, synth

REPORT_NAME = "report.json"
PROVENANCE_NAME = "report-provenance.json"
ALL_ARTEFACTS = (REPORT_NAME, PROVENANCE_NAME)

#: The stage-7 note, kept under this name once stage 12 publishes the final one as
#: `note.md`. Losing it would leave no record of what the synthesizer actually produced.
SYNTH_NOTE_NAME = "note.synth.md"

#: Artefacts without which there is no deliverable, and why each one is load-bearing.
REQUIRED: dict[str, str] = {
    "note": "the deliverable itself",
    "transcript.json": "the normalised transcript the note was built from",
    "transcript.txt": "the human-readable transcript stage 9 audits numbers against",
    "transcript.raw.json": "the endpoint's unmodified payload; the only way to re-derive "
    "the transcript if the normaliser is wrong",
    lint.REPORT_NAME: "the mechanical gate that ran before the audit",
    audit.REPORT_NAME: "the audit; without it the note is unverified",
    repair.REPORT_NAME: "the fix log; D5 requires 'wrong' and 'changed' stay separable",
    frames.MANIFEST: "the record of which frames were extracted and when",
}

#: Artefacts that are legitimately absent. Recorded as absent, not as failures.
OPTIONAL: dict[str, str] = {
    quality.REPORT_NAME: "the quality gate runs on frames; an audio-only source has none",
    audit.LLM_TRANSCRIPT_NAME: "tier 2 did not run, so no model was called",
    link.REPORT_NAME: "the link stage writes no report when no terms were configured",
}


@dataclass
class Report:
    verified: list[dict] = field(default_factory=list)
    absent: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    frames_expected: int = 0
    frames_found: int = 0
    promoted: bool = False

    @property
    def ok(self) -> bool:
        """Nothing required is missing, and the frame count agrees with the manifest."""
        return not self.missing and self.frames_found == self.frames_expected

    def summary(self) -> str:
        if not self.ok:
            return f"FAILED: {len(self.missing)} missing, frames {self.frames_found}/{self.frames_expected}"
        return (
            f"{len(self.verified)} verified, {len(self.absent)} optional absent, "
            f"frames {self.frames_found}/{self.frames_expected}"
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "absent": self.absent,
            "missing": self.missing,
            "frames_expected": self.frames_expected,
            "frames_found": self.frames_found,
            "note_promoted": self.promoted,
        }


def promote_note(bundle_root: Path, final_note: Path, synth_note: Path) -> tuple[Path, bool]:
    """Publish the final note as `note.md`, keeping stage 7's original as `note.synth.md`.

    Returns (path, moved_original). The original is only moved when it is actually
    different, so a run where nothing was repaired or linked does not gain a pointless
    duplicate of itself.
    """
    target = bundle_root / synth.NOTE_NAME
    if not final_note.is_file() or final_note.resolve() == target.resolve():
        return target, False
    # Compare content, not identity. After one promotion note.synth.md necessarily differs
    # from the final note -- keeping it is the whole point -- so a path comparison never
    # matches here, and every later call would move the original a second time.
    if target.is_file() and target.read_bytes() == final_note.read_bytes():
        return target, False
    if synth_note.is_file():
        synth_note.replace(bundle_root / SYNTH_NOTE_NAME)
    elif target.is_file():
        target.replace(bundle_root / SYNTH_NOTE_NAME)
    shutil.copy2(str(final_note), str(target))
    return target, True


def _check(path: Path) -> tuple[bool, str]:
    """Present and non-empty. A zero-byte artefact is a failed write, not a small one."""
    if not path.is_file():
        return False, "missing"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"cannot stat: {exc}"
    if size == 0:
        return False, "zero bytes"
    return True, f"{size} B"


def count_frames(bundle_root: Path) -> tuple[int, int]:
    """(expected from the manifest, found on disk).

    Expected counts data rows, not lines: a header-only manifest is the D3 failure this
    whole stage exists to catch, and counting lines would call that a success.
    """
    manifest = bundle_root / frames.MANIFEST
    if not manifest.is_file():
        return 0, 0
    expected = len(frames.read_manifest(manifest))
    found = sum(1 for p in bundle_root.glob("*.jpg") if p.is_file())
    return expected, found


def run(
    bundle_root: Path,
    *,
    final_note: Path,
    synth_note: Path,
    artefacts: dict[str, Path],
    stream=None,
) -> Report:
    out = stream if stream is not None else sys.stdout
    root = Path(bundle_root)
    report = Report()

    published, promoted = promote_note(root, final_note, synth_note)
    report.promoted = promoted
    artefacts = dict(artefacts)
    artefacts["note"] = published

    for name, reason in REQUIRED.items():
        path = artefacts.get(name) or (root / name)
        ok, detail = _check(Path(path))
        entry = {"artefact": name, "why": reason, "detail": detail}
        if ok:
            report.verified.append(entry)
        else:
            report.missing.append(entry)

    for name, reason in OPTIONAL.items():
        path = artefacts.get(name) or (root / name)
        if not Path(path).is_file():
            report.absent.append({"artefact": name, "why_absent": reason})

    report.frames_expected, report.frames_found = count_frames(root)

    (root / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / PROVENANCE_NAME).write_text(provenance(report) + "\n", encoding="utf-8")

    out.write(
        f"  [{stages.IMPLEMENTED}/{stages.IMPLEMENTED}] report "
        f"{'OK' if report.ok else 'FAILED'}: {report.summary()}\n"
    )
    for entry in report.missing:
        out.write(f"           MISSING {entry['artefact']}: {entry['detail']} -- {entry['why']}\n")
    if report.frames_expected and report.frames_found != report.frames_expected:
        out.write(
            f"           frame count disagrees with {frames.MANIFEST}: "
            f"{report.frames_found} on disk, {report.frames_expected} listed\n"
        )
    return report


def provenance(report: Report) -> str:
    return json.dumps(
        {
            "stage": 12,
            "tool": "glimpse-report-v1",
            "requires_model": False,
            "verified_by": "filesystem stat, not the exit code of any earlier stage",
            "why": (
                "D3: a run produced zero frames and exited successfully, because ffmpeg "
                "wrote an empty file and exited 0. An exit code is a claim by the stage "
                "that failed; a stat is a measurement."
            ),
            "vault": "not written by this stage; export_to_vault copies and verifies (D9)",
            "ok": report.ok,
            "frames_expected": report.frames_expected,
            "frames_found": report.frames_found,
            "note_promoted": report.promoted,
        },
        indent=2,
        ensure_ascii=False,
    )
