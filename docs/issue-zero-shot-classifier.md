**Title:** Milestone: local zero-shot frame classifier — filter non-lecture frames before they leave the machine

**Labels:** `enhancement`, `stage6`, `post-mvp`

## Problem

Stage 4 emits frames that are not lecture material: a desktop, a browser, the Start menu, a
notification covering the screen. Stage 5 cannot reject them, because it measures focus only
and a sharp Start menu passes. A time filter cannot reject them either: a lecturer paging
through the notes they are explaining produces the same burst of 1-3 s frames as transient
clutter, so a lifetime threshold would delete real content.

Whether a frame is lecture material is a question about what is in it.

## Measurements (lecture 1, 58-frame run; `manifest.tsv`)

```
frames                                   58
forced max_gap frames (+180 s)           17
lifetime < 10 s                          23   (40%)
lifetime < 20 s                          27   (47%)
bursts of 2-5 frames within <20 s        12
refused by the VLM (#83)                 17 of 58, ~116 k of 396 k input tokens
```

The 17 forced frames and the 17 refusals are different sets; the equal count is a coincidence.
Lifetime for the last frame assumes the recording ends at ~4520 s.

## Proposal

Decided now, in ADR-0006 D1: the stage 6 caption call returns `frame_class`
(`lecture_material` | `not_material` | `uncertain`). Frames classed `not_material` are marked
in the manifest and the run report, never deleted (ADR-0005 D2).

This milestone is D2: a **local zero-shot** image classifier that labels frames before any
frame goes to a hosted endpoint. No training: there are 58 frames per lecture and no labelled
set.

## Why a milestone, not a task

It breaks ADR-0002's single-dependency, single-zipapp constraint. Candidate runtimes
(`onnxruntime`, `torch`) and model weights are heavy; their real size has **not** been
measured. That needs its own ADR amendment (ADR-0002 is amended, not replaced).

## Triggers — build this when any one holds

- [ ] **Privacy:** frames of a desktop or browser tab must not reach a hosted endpoint. (Audio already stays local; the frames that leak are exactly the non-lecture ones.)
- [ ] **Volume:** state counts grow by an order of magnitude on other lectures (ADR-0005 D4).
- [ ] **Measured VLM labelling error** above an agreed rate on a hand-labelled sample.

## Prerequisites (not part of this milestone)

- #84 — captions reach stage 7. Without it a better filter improves nothing visible.
- A hand-labelled sample of one lecture's frames, to measure both the VLM's labels and any
  local model against. Issue #83 notes that nobody has checked the 17 refusals.

## Tasks

- [ ] Hand-label one lecture's 58 frames: `lecture_material` / `not_material` / `uncertain`
- [ ] Measure the VLM `frame_class` against the labels (ADR-0006 D1)
- [ ] Measure a candidate local model against the same labels; record install size and per-frame time
- [ ] Amend ADR-0002: what the optional dependency costs, and how `glimpse doctor` reports it
- [ ] Implement as an optional extra (`pip install glimpse[classify]`), off by default
- [ ] Report per run: frames classified, per class, and which classifier ran (provenance, ADR-0004)

## Acceptance

- Core install (`numpy` only) and the zipapp build still work without the extra.
- No frame is removed: every `not_material` frame stays on disk and is listed in the report.
- Recall of `lecture_material` on the labelled sample is reported next to precision. A missed
  slide is worse than a redundant frame, so recall is the number that gates this.

## Out of scope

Deleting frames by lifetime. Training a classifier. Choosing the model before the labelled
sample exists.

Relates to ADR-0002, ADR-0004, ADR-0005, ADR-0006; #83, #84.
