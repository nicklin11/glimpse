# ADR 0006: Frame classification — the VLM labels frames now, a local zero-shot model later

- **Status:** Proposed
- **Date:** 2026-10-05
- **Proposal:** [#85](https://github.com/nicklin11/glimpse/issues/85)
- **Scope:** Post-MVP direction. Records what is decided now and what is deferred.

## Context

Stage 4 emits frames that carry non-lecture content: a desktop, a browser, the Start menu, a
notification covering the screen. Stage 5 cannot reject them: it measures focus, and a sharp
Start menu passes. A time filter cannot reject them either, because a lecturer paging through
the notes being explained produces the same burst of short-lived frames (seconds each) as
transient clutter. Whether a frame is lecture material is a question about its content.

Measured on the 58-frame run of lecture 1 (`manifest.tsv`): 23 frames live under 10 s, 12 bursts
of 2-5 frames within 20 s, 17 frames are the forced `max_gap` frames at +180 s. Issue #83:
17 of 58 frames caption as `NO NEW INFORMATION`, and whether those refusals are correct has
not been checked against the video.

## Decision

### D1 — Now: the caption call returns a frame class, and nothing is deleted

Stage 6's existing call returns one more field, `frame_class`:
`lecture_material` | `not_material` | `uncertain`. No second model call, no new dependency,
ADR-0002's single-dependency constraint is untouched.

Frames classed `not_material` are **marked in the manifest and the run report, not removed**
(ADR-0005 D2). A page of notes that the lecturer opened is lecture material and a wrong
removal is a missing piece of the note; a redundant frame only costs tokens.

### D2 — Deferred: a local zero-shot image classifier in front of the VLM

A local model (zero-shot, no training; there are 58 frames per lecture and no labelled set)
labels frames before any frame leaves the machine.

It is not built now because the blocker is elsewhere (#84: the captions never reach stage 7)
and because the cost argument is weak: stage 6 is ~$0.0006 per frame on the measured gateway.

### D3 — Triggers for building D2

Any one of:

1. **Privacy.** Frames of a desktop or browser tab must not be sent to a hosted endpoint.
   This is the strongest reason: the audio already stays local (ADR-0004), and the frames
   that leak are exactly the non-lecture ones.
2. **Volume.** State counts grow by an order of magnitude on other lectures (ADR-0005 D4).
3. **Measured VLM labelling error** above an agreed rate on a hand-labelled sample.

## Alternatives rejected

- **Delete frames by lifetime.** Rejected: it deletes paged notes.
- **Train a classifier.** Rejected: no labelled data; 58 frames per lecture.
- **Classifier now.** Rejected: adds a heavy dependency to fix a symptom while the larger
  defect is that captions do not reach the note.

## Consequences

**Good.** No dependency change. Misclassification is visible and reversible.

**Bad.** Until D2, non-lecture frames are still sent to the VLM endpoint.

**Open.** The label set is a guess. It needs a hand-labelled sample of one lecture's frames
before D1's prompt wording is trusted.

Relates to ADR-0002 (distribution), ADR-0004 (endpoints), ADR-0005 (caption budget).