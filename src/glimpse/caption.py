"""Stage 6: put each frame next to the words being spoken when it was on screen.

Deterministic, and deliberately so. The useful work here is temporal alignment: every frame
stage 5 passed gets the slice of transcript that was live while it was displayed. That is
arithmetic over timestamps, not inference, and putting a model in the middle of it would
make the one part of this stage that can be verified the one part that cannot be.

Alignment is by *overlap*, not by nearest-timestamp. A frame is on screen from the moment
the previous frame was replaced until it is replaced itself, so it corresponds to the
interval `[previous_pts, pts)` -- for the first frame, `[0, pts)`. Selecting segments that
merely start nearest to the frame would drop everything said while a slide sat there, which
for a 180 s slide gap is most of the lecture.

## What this stage does not do

It does not caption. ADR-0001 D4 assigns captioning to a VLM, and there is no vision client here.
A frame that cannot be described still gets its transcript window, its word timings and its
quality verdict, which is everything stage 7 needs to place it in the note. The absence is
recorded in provenance as `caption: null` with a reason, so a later consumer can tell the
difference between "not captioned" and "nothing there".

It also does not OCR. Recovering text from the frame is a separate concern from aligning
it, and adding a binary dependency for it would make stage 6 unable to run at all on hosts
that only need the alignment.
"""

from __future__ import annotations

import json
import sys
from . import stages  # noqa: F401 -- the progress line's denominator
from dataclasses import dataclass, field
from pathlib import Path

REPORT_NAME = "captions.json"
PROVENANCE_NAME = "caption-provenance.json"

#: A frame older than this relative to the transcript end has no words behind it at all.
#: Reported per frame rather than treated as an error: it is information about the source.
UNCOVERED = "no_transcript_coverage"

#: Above this share of uncovered frames the audio and the video are not describing the same
#: lecture, which is a muxing problem, not a captioning problem. ADR-0001 D5 exit 4.
UNCOVERED_SHARE_LIMIT = 0.30


@dataclass(frozen=True)
class Settings:
    uncovered_share_limit: float = UNCOVERED_SHARE_LIMIT
    #: Characters of transcript text carried per alignment. Enough for stage 7 to place the
    #: frame; the full transcript is a separate artefact and is read from there.
    max_chars: int = 4_000


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float
    text: str


@dataclass
class Alignment:
    """One frame, and the words that were live while it was up."""

    frame: str
    pts_ms: int
    image: str
    enhanced: str | None
    #: Half-open interval during which this frame was the one on screen.
    on_screen_ms: tuple[int, int]
    #: Transcript segments overlapping that interval.
    segment_indices: list[int] = field(default_factory=list)
    text: str = ""
    first_word_ms: float | None = None
    word_count: int = 0
    #: Always None until a VLM exists. Present as a key so consumers do not have to guess.
    caption: str | None = None
    caption_status: str = "NOT_CONFIGURED"
    quality_gate: str = ""
    content_type: str = ""
    crop_state: str = ""
    reason: str = ""

    @property
    def covered(self) -> bool:
        return bool(self.segment_indices)

    def as_dict(self) -> dict:
        return {
            "frame": self.frame,
            "pts_ms": self.pts_ms,
            "image": self.image,
            "enhanced": self.enhanced,
            "on_screen_ms": list(self.on_screen_ms),
            "segment_indices": self.segment_indices,
            "first_word_ms": self.first_word_ms,
            "word_count": self.word_count,
            "text": self.text,
            "caption": self.caption,
            "caption_status": self.caption_status,
            "quality_gate": self.quality_gate,
            "content_type": self.content_type,
            "crop_state": self.crop_state,
            "reason": self.reason,
        }


@dataclass
class Report:
    alignments: list[Alignment] = field(default_factory=list)
    #: Carried on the report, not read from `Settings` at property time. `ok` is a property
    #: with no access to the Settings that produced the alignments, and reading the class
    #: constant instead meant a caller who lowered the limit via Settings got it silently
    #: ignored -- the gate would run at the default and say so.
    uncovered_share_limit: float = UNCOVERED_SHARE_LIMIT

    @property
    def considered(self) -> list[Alignment]:
        """Frames stage 6 was allowed to align.

        A frame the quality gate rejected is not a candidate, so it must not count against
        A/V sync. One bad frame of sixteen is not evidence that the audio and the video are
        describing different lectures, and counting it would let a blur failure masquerade
        as a muxing failure.
        """
        return [a for a in self.alignments if a.caption_status != "GATED_OUT"]

    @property
    def ok(self) -> bool:
        """Coverage is a gate: too many frames with no transcript means broken A/V sync."""
        considered = self.considered
        if not considered:
            return False
        uncovered = sum(1 for a in considered if not a.covered)
        return uncovered / len(considered) <= self.uncovered_share_limit

    @property
    def uncovered(self) -> list[Alignment]:
        return [a for a in self.considered if not a.covered]

    @property
    def uncovered_share(self) -> float:
        considered = self.considered
        if not considered:
            return 1.0
        return sum(1 for a in considered if not a.covered) / len(considered)

    def summary(self) -> str:
        if not self.considered:
            return "no frames aligned"
        words = sum(a.word_count for a in self.considered)
        gated = len(self.alignments) - len(self.considered)
        tail = f", {gated} gated out by stage 5" if gated else ""
        return (
            f"{len(self.considered)} frames aligned, {words} words, "
            f"{len(self.uncovered)} without transcript coverage{tail}"
        )

    def explain(self) -> str:
        return "\n".join(
            f"  {a.frame}: on screen {a.on_screen_ms[0]}-{a.on_screen_ms[1]} ms, "
            f"{a.word_count} words ({a.reason})"
            for a in self.uncovered
        )


def read_manifest(manifest: Path) -> list[tuple[int, str]]:
    """`(pts_ms, filename)` from stage 4's manifest. Missing or short manifest -> empty."""
    if not manifest.is_file():
        return []
    rows: list[tuple[int, str]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip().isdigit():
            continue
        rows.append((int(parts[0]), parts[2].strip()))
    rows.sort()
    return rows


def read_segments(transcript: Path) -> list[Segment]:
    segments: list[Segment] = []
    if not transcript.is_file():
        return segments
    payload = json.loads(transcript.read_text(encoding="utf-8"))
    for raw in payload.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        text = (raw.get("text") or "").strip()
        try:
            index = int(raw.get("index"))
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            # Reported by stage 3 as an anomaly; silently swapping the pair here would
            # hide a real decoder problem behind a plausible-looking alignment.
            continue
        segments.append(Segment(index=index, start=start, end=end, text=text))
    segments.sort(key=lambda s: (s.start, s.end))
    return segments


def overlapping(segments: list[Segment], start_ms: int, end_ms: int) -> list[Segment]:
    """Segments whose interval meets `[start_ms, end_ms)`.

    `segments` is sorted by start time, so the scan stops at the first segment that begins
    after the window rather than walking the whole transcript. On a 4 500 s lecture that is
    1 500 comparisons per frame instead of 1 500 times the frame count.
    """
    out: list[Segment] = []
    for segment in segments:
        if segment.start * 1000 >= end_ms:
            break
        if segment.end * 1000 > start_ms:
            out.append(segment)
    return out


def _first_word_ms(raw_segments: list[dict], limit_ms: int) -> float | None:
    """Start of the first word at or after `limit_ms`, in seconds.

    Word timings exist because the frame boundary is a hard edge and a segment boundary is
    not: at a slide change the previous sentence is still audible. Without word timings the
    alignment would attribute words to the new frame that were spoken against the old one.

    Every overlapping segment is searched, not just the first. The segment straddling a
    frame boundary begins before it, so all of *its* words precede the limit; looking only
    there returns None for every frame but the first, which is a silent hole in the field
    rather than a missing measurement.

    None means no word started inside the window -- the window fell entirely inside a
    long segment. That is a fact about the transcript, so it stays None rather than being
    rounded off to the segment start.
    """
    best: float | None = None
    for raw in raw_segments:
        for word in raw.get("words") or []:
            try:
                start = float(word.get("start"))
            except (TypeError, ValueError):
                continue
            if start * 1000 >= limit_ms and (best is None or start < best):
                best = start
    return best


def align(
    manifest: Path,
    transcript: Path,
    quality_report: Path,
    images: Path,
    settings: Settings | None = None,
) -> Report:
    """Align every frame stage 5 passed to the words spoken while it was displayed."""
    settings = settings or Settings()
    report = Report(uncovered_share_limit=settings.uncovered_share_limit)

    frames = read_manifest(manifest)
    if not frames:
        return report

    transcript_payload = (
        json.loads(transcript.read_text(encoding="utf-8")) if transcript.is_file() else {}
    )
    segments = read_segments(transcript)

    quality: dict[str, dict] = {}
    if quality_report.is_file():
        for entry in json.loads(quality_report.read_text(encoding="utf-8")).get("frames", []):
            quality[entry.get("name", "")] = entry

    for position, (pts_ms, name) in enumerate(frames):
        # A frame with pts = T was on screen *from* T, until the next frame appeared. The
        # window is [pts_i, pts_{i+1}), not [pts_{i-1}, pts_i): a frame extracted at
        # 304 132 ms is the slide that appeared at 304 132 ms, and everything spoken before
        # that belongs to the previous frame.
        if position + 1 < len(frames):
            on_screen_end = frames[position + 1][0]
        else:
            # The last frame has no successor to bound it. Use the audio duration when the
            # transcript knows one, otherwise the median gap, so it is not left with a
            # zero-length window and reported as uncovered.
            on_screen_end = _tail_end_ms(frames, transcript_payload)
        gate = quality.get(name, {})
        enhanced = f"{Path(name).stem}_q.jpg"
        alignment = Alignment(
            frame=name,
            pts_ms=pts_ms,
            image=name,
            enhanced=enhanced if (images / enhanced).is_file() else None,
            on_screen_ms=(pts_ms, max(on_screen_end, pts_ms + 1)),
            quality_gate=gate.get("quality_gate", ""),
            content_type=gate.get("content_type", ""),
            crop_state=gate.get("crop_state", ""),
        )

        # Only frames the gate passed carry forward. A frame stage 5 rejected has no
        # business anchoring a section of the note.
        if alignment.quality_gate and alignment.quality_gate != "PASS":
            alignment.reason = f"stage 5 {alignment.quality_gate}"
            alignment.caption_status = "GATED_OUT"
            report.alignments.append(alignment)
            continue

        hits = overlapping(segments, pts_ms, max(on_screen_end, pts_ms + 1))
        alignment.segment_indices = [s.index for s in hits]
        alignment.word_count = sum(len((s.text or "").split()) for s in hits)  # reporting only
        pieces: list[str] = []
        used = 0
        for segment in hits:
            piece = segment.text.strip()
            if not piece:
                continue
            if used + len(piece) > settings.max_chars:
                break
            pieces.append(piece)
            used += len(piece) + 1
        alignment.text = " ".join(pieces)

        alignment.first_word_ms = _first_word_ms(
            _raw_segments(transcript_payload, alignment.segment_indices), pts_ms
        )
        if not alignment.covered:
            alignment.reason = UNCOVERED
        report.alignments.append(alignment)

    return report


def _tail_end_ms(frames: list[tuple[int, str]], payload: dict) -> int:
    """Where the final frame stops being on screen."""
    last = frames[-1][0]
    try:
        audio_ms = int(float(payload.get("audio_duration") or 0) * 1000)
    except (TypeError, ValueError):
        audio_ms = 0
    if audio_ms > last:
        return audio_ms
    gaps = sorted(b - a for (a, _), (b, _) in zip(frames, frames[1:]))
    if not gaps:
        return last
    return last + gaps[len(gaps) // 2]


def _raw_segments(payload: dict, indices: list[int]) -> list[dict]:
    """The raw segment dicts behind `indices`, carrying their word timings."""
    if not indices:
        return []
    wanted = set(indices)
    return [
        raw
        for raw in payload.get("segments") or []
        if isinstance(raw, dict) and raw.get("index") in wanted
    ]


def run(
    manifest: Path,
    transcript: Path,
    quality_report: Path,
    images: Path,
    destination: Path,
    *,
    settings: Settings | None = None,
    stream=None,
) -> Report:
    settings = settings or Settings()
    out = stream if stream is not None else sys.stdout
    report = align(manifest, transcript, quality_report, images, settings)
    destination.mkdir(parents=True, exist_ok=True)
    write(report, destination / REPORT_NAME, settings)
    (destination / PROVENANCE_NAME).write_text(provenance(settings) + "\n", encoding="utf-8")
    out.write(f"  [6/{stages.IMPLEMENTED}] caption  {report.summary()}\n")
    if report.uncovered:
        for line in report.explain().splitlines():
            out.write(f"         {line.strip()}\n")
    out.write(
        "         alignment is deterministic (segment overlap, word-timed boundary); "
        "captioning is NOT_CONFIGURED -- no vision client in this codebase\n"
    )
    return report


def write(report: Report, target: Path, settings: Settings) -> None:
    target.write_text(
        json.dumps(
            {
                "provenance": {
                    "stage": 6,
                    "tool": "glimpse-align-v1",
                    "alignment": "segment overlap with word-timed boundary",
                    "caption": None,
                    "caption_status": "NOT_CONFIGURED",
                    "uncovered_share": round(report.uncovered_share, 4),
                },
                "alignments": [a.as_dict() for a in report.alignments],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def provenance(settings: Settings) -> str:
    return json.dumps(
        {
            "stage": 6,
            "tool": "glimpse-align-v1",
            "method": "segment overlap; boundary at first word >= frame display start",
            "requires_model": False,
            "caption": "NOT_CONFIGURED (ADR-0001 D4 assigns this to a VLM; none is configured)",
            "ocr": "not attempted (no dependency; alignment does not need it)",
            "uncovered_share_limit": settings.uncovered_share_limit,
            "max_chars": settings.max_chars,
        },
        indent=2,
    )
