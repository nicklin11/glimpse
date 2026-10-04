# ADR 0005: Captioning budget — distinct screen states, not decoded frames

- **Status:** Accepted
- **Date:** 2026-10-03
- **Proposal:** [#16](https://github.com/nicklin11/glimpse/issues/16)
- **Scope:** MVP. The cost control for stage 6.

> **`D<n>`** = decision *n* within *this* ADR. The number is local to this file and
> collides with numbers in ADR-0001 and ADR-0004; references from anywhere else must be
> written `ADR-000N Dn`. Reading order and index: [`docs/adr/README.md`](README.md).

## Context

The requirement is "assess the frames". "All frames" has two readings that differ by **four
orders of magnitude** on lecture 1 (4396 s, 30 fps):

| Reading | Count | Cost at the measured gateway price |
|---|---|---|
| every **decoded** frame | ~131 880 | not a cost, a different program |
| every **distinct screen state** after stage 4 | **17** | ~$0.01 per lecture |

17 distinct states over 73 minutes is **measured, not estimated** (ADR-0001 D4; manifest at
`mscs/Проектирование оптимальных систем управления/frames/Лекция 1. 01.10.26/manifest.tsv`).

## Decision

### D1 — The captioning budget is whatever stage 4 produces; there is no sampling knob

Stage 6 captions **every** distinct state that passes stage 5. No sampling policy, because at
17 frames there is nothing to sample and **a sampling rule is a decision made without data**.

The deduplication in stage 4 is what makes the visual budget small. That is the reason #7's
two-pass design matters beyond frame quality: **it is the cost control.**

### D2 — Frames rejected by the quality gate are recorded, not dropped

Stage 5 rejects frames; stage 6 does not caption them. Both must appear in the run output and
in the artefact set with the gate's verdict and reason, so "17 states, 3 uncaptioned" is
visible. **A pipeline that silently captions 14 of 17 reports a completeness it does not
have.**

Stage 12 verifies exactly this property on the way out: frames counted on disk are compared
against the manifest's data rows, so a manifest that lists 18 and a directory holding 17 is a
failure rather than a rounding difference.

### D3 — Completeness is reported per run

States detected, passed quality, captioned, cache hits, failed — with the model id (ADR-0004
D4). This is the number that answers "did it look at everything", and it is the one to read
before trusting a note's visual claims.

### D4 — The 30 fps figure is an assumption, not a measurement

D1's cost argument rests on a state count measured on **one** lecture. Before this budget
forecasts anything, stage 4 must run on three lectures of different lengths and record the
state counts. If one produces 400 states, D1 is revisited and the sampling decision then has
data behind it.

## Alternatives rejected

- **Sample every Nth detected state.** Rejected for now, on D4's grounds — no data yet. It
  becomes the right answer the moment state counts justify it, and it belongs in stage 4
  (which decides what is emitted), not stage 6.
- **Caption every decoded frame.** Rejected on cost by a factor of ~7700 against D1.
- **Let the VLM choose which frames matter.** Rejected: a second model call per frame to save
  a first one, while stage 4's deduplication is already exact and free.

## Consequences

**Good.** The visual budget is small enough to be free and complete enough to trust.

**Bad.** 17 states over 73 minutes means **this** lecture is visually sparse (ADR-0001 D4). A
text-dense recording will produce more. D4 exists because that number is unmeasured for other
lectures.

**Honest limit, unchanged by this ADR.** Interpolation adds no information; if on-screen text
is too small to resolve, cropping harder cannot recover it. The quality gate must report that
rather than pretend otherwise.

## Acceptance

- Stage 6 captions every frame that passed stage 5, and reports detected / passed /
  captioned / failed counts.
- A quality-gate failure is visible in the run output and in the artefact set.
- Three lectures measured for state count before this budget forecasts cost. **Open.**

Implements ADR-0001 D4 and D8. Part of #3.
## Amendment 2026-10-05 — the state count is 58, not 17, and the "17" was never a pipeline measurement

The cost argument above rests on **17 distinct screen states**. That number is wrong, and it was
never what this ADR claimed it was.

**Where 17 came from.** `mscs/Проектирование оптимальных систем управления/frames/Лекция 1. 01.10.26/`
— 18 files, hand-made, cited at line 23. Those are the frames a person selected for their own
notes. Stage 4 does not reproduce that selection; it emits every distinct screen state the
deduplicator finds, including slide transitions, build steps and dim frames. The two numbers
were never the same kind of thing and the ADR treated one as a measurement of the other.

**What stage 4 actually produces**, on the same lecture:

```
detected   58 frames
passed     57   (1 dark-canvas, explicitly non-fatal)
captioned  58   <- see below
```

**The decoded-frame figure was also wrong.** The context table says `~131 880`, derived from
4396 s at 30 fps. The file is 4520.1 s and the container reports `nb_frames=135602`.

| | ADR-0005 said | measured |
|---|---|---|
| decoded frames | ~131 880 | **135 602** |
| distinct states | 17 | **58** |
| ratio | ~7 800x | **~2 340x** |
| visual budget | ~$0.01 | 396 k input tokens |

### D5 — The real budget, measured from `caption_trace`

From stage 6's per-frame provenance (`a8cee65`), one 75-minute lecture, cold cache:

```
58 calls      opencode-go/glm-5.3-flash -> served as glm-5.3-flash
395 801       prompt tokens in
17 225        completion tokens out
446.8 s       wall clock, attempts=1 on every call
~6 826        input tokens per frame
```

~6.8 k of that is the image at model resolution; the aligned transcript is the rest. The VLM is
reading the slide, which is what the stage exists to do, and writing a short description. The
cost is the vision, not the output length.

**D1 still holds.** "The budget is whatever stage 4 produces; there is no sampling knob" is
unaffected — 58 is still small, and a sampling rule would still be a decision made without
data. What changed is the size of the number, by a factor of 3.4.

### D6 — D2's acceptance was passing vacuously

D2 requires that frames rejected by the gate are "recorded, not dropped", so that "N states, M
uncaptioned" is visible. Its acceptance line reads *"a quality-gate failure is visible in the
run output and in the artefact set"*.

Between runs 3 and 8 that was true and meaningless. The gate was a no-op in the pipeline
(#82): all 58 frames were captioned and `quality_gate` was `""` on every one, so there was no
failure to be visible about. `GATED_OUT` existed, was tested directly against `align()`, and
was unreachable in production.

Stage 6 read a work-dir path that `Bundle.publish` had already moved, `align()` read a missing
quality report as "no gate information", and `if alignment.quality_gate and ... != "PASS"` was
falsely false for every frame. Stage 7's `!= "GATED_OUT"` guard had nothing to discard for the
same reason.

So D2 was satisfied by a test that called `align()` with a quality file that exists, and
unsatisfied by the pipeline. Both statements are true and the ADR recorded only the first.

### D4 — partly discharged

D4 asks for three lectures of different lengths before this budget forecasts cost. **One is
measured** (58 states over 4520 s). Two remain open, and the extrapolation in "Consequences" —
"17 states over 73 minutes means this lecture is visually sparse" — was wrong: 58 states is
2.6x denser than believed, so this lecture is **not** visually sparse, and the caution it
carried does not apply to it.
