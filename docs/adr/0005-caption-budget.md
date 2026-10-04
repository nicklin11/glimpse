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