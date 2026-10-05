# Architecture decision records

Five documents. Each records a decision that was made once, with the reasoning that
produced it, and — where the reasoning was later contradicted by measurement — the
correction.

## What `D<n>` means

`D<n>` means "decision *n* **within this ADR**". The number is local to the file. It is
not a global ID, and the same number means different things in different ADRs:

| Reference | Means |
|---|---|
| `D4` in this repo, unqualified | **ambiguous** — do not write this |
| `ADR-0001 D4` | frame quality is gated; crop, not a better model |
| `ADR-0004 D4` | provenance is recorded in the artefact |

Write the full form whenever a reference leaves the file it lives in. Inside the file
itself, `D4` is fine.

## The five documents

| # | File | Decides | Lines | Read when |
|---|---|---|---|---|
| 0001 | [lecture-pipeline](0001-lecture-pipeline.md) | The twelve-stage pipeline and its nine decisions | ~870 | Always. This is the product. |
| 0002 | [distribution](0002-distribution.md) | Packaging, zipapps, and where the STT backend lives | ~220 | Packaging, install, or the "no runtime dependencies" rule |
| 0003 | [events](0003-events.md) | The pipeline emits events instead of rendering; progress semantics | ~95 | Working on progress, the event stream, or the terminal renderer |
| 0004 | [endpoints](0004-endpoints.md) | Model endpoint configuration and artefact provenance | ~90 | Configuring models. Defines the `GLIMPSE_*` surface. |
| 0005 | [caption-budget](0005-caption-budget.md) | Why stage 6 captions screen states, not frames | ~160 | Working on captions or on cost |

## Reading order

**1. [ADR-0001](0001-lecture-pipeline.md) — but only part of it.**

Read the header block, `## Context`, `## Decision` and `## The pipeline`. That is roughly
the first 230 lines and it is the specification.

**Stop there.** Everything from `## Amendment 2026-10-02` onward — six sections, about 640
lines — is a **changelog**, not a spec. It records what changed and what measurement forced
the change. It is valuable and it is not what you read to learn what the pipeline does.

The register in `## Decision` is written in the original wording and several entries are
stale by construction. **Every entry now carries a `Status:` line saying what it means
today** — read that line first. `D2` and `D9` are superseded; `D4` was corrected twice;
`D8` was decided but never implemented.

**2. [ADR-0004](0004-endpoints.md).** 90 lines, and it defines the configuration surface
every model-facing stage depends on. Cheapest high-value read in the set.

**3. [ADR-0003](0003-events.md).** The contract between the pipeline and anything that
wants to display what it is doing.

**4. [ADR-0005](0005-caption-budget.md).** Only when working on captions or cost.

**5. [ADR-0002](0002-distribution.md).** Only when packaging. Note that its `D3` is
superseded — shipboard is no longer referenced anywhere in `src/`.

## Cross-ADR dependencies

Not every ADR is independent. These are the couplings that matter:

- **ADR-0004 implements ADR-0001 D5 and D8.** `D3` there (`[vision]` / `[text]`
  configuration sections) is the design for the vision endpoint that ADR-0001 `D8`
  promised and nobody built. See issue #67.
- **ADR-0002 D3 depends on ADR-0001 D2**, which is superseded. The dependency chain
  terminated at the bottom; `D3`'s premise no longer holds.
- **ADR-0005 assumed stage 4 deduplicates to ~17 states. It produces 58.** The 17 was a
  hand-made selection from the baseline note's frames directory, not a pipeline measurement,
  and the decoded-frame figure it was divided into was also wrong (131 880 against a real
  135 602). Corrected in *Amendment 2026-10-05*; the decision survives, the arithmetic does
  not. Two lectures remain unmeasured, which is what D4 asks for.
- **ADR-0005 D2 was satisfied vacuously.** Its acceptance -- that a gate failure is visible --
  held for eight runs while the gate never fired in the pipeline (#82). An ADR criterion can
  be met by the unit under test and missed by the thing it governs; both are true and only
  one of them is the requirement.
- **ADR-0003 D4** assumes blocking work stays blocking. The endpoint backend that replaced
  shipboard may behave differently; re-measure before relying on it.

## Conventions

- An ADR's own `- **Status:**` line describes the **document**. `D<n>` status is separate
  and is stated per decision, next to the decision.
- Amendments are kept, not folded into the register. The measurements in them are the
  justification; deleting them would make the current text look arbitrary.
- A decision that was made and not implemented says so. `D8` says **DECIDED, NOT
  IMPLEMENTED**. "We decided to defer this" and "we decided not to do this" must never
  look the same.

## Stage status

The map from pipeline stage to implementing module to open/closed lives in
[`docs/stages.md`](../stages.md). It is the authority on what is actually built; the ADRs
record what was decided, which is not the same thing.