# Pipeline stages — what exists, what is open, what is verified

ADR-0001 records what was **decided**. This file records what is **built**. They are not
the same thing, and the difference is where the work actually is.

The stage list and the exit-code table are in
[ADR-0001 § The pipeline](../adr/0001-lecture-pipeline.md#the-pipeline).

## How to read the columns

**Code** — the module that implements the stage. Verified by reading the file.

**Code present** — `yes` means the module exists and is called from `pipeline.run`.
It does **not** mean the stage has been seen to work on a real lecture. See
[Verification](#verification-status).

## The table

| # | Stage | Code | Code present | Stage fully working |
|---|---|---|---|---|
| 0 | `doctor` | `deps.py`, `cli.py` | yes | yes — green with a real gateway |
| 1 | `probe` | `probe.py` | yes | not yet verified on the lecture |
| 2 | `audio` | `audio.py` | yes | not yet verified on the lecture |
| 3 | `stt` | `stt/` — `core`, `backends`, `whispercpp`, `openai_compat` | yes | **yes** — 4520 s transcribed via the proxy |
| 4 | `frames` | `frames.py` | yes | **yes** — 16 frames, deterministic across 3 runs |
| 5 | `quality` | `quality.py` — `GeometricEstimator` | yes | **yes** — gate measured, threshold 300 |
| 6 | `caption` | `caption.py` | **half** | **no — this is the open stage** |
| 7 | `synth` | `synth.py` | yes | not yet verified on the lecture |
| 8 | `lint` | `lint.py` | yes | not yet verified on the lecture |
| 9 | `audit` | `audit.py` | yes | not yet verified on the lecture |
| 10 | `repair` | `repair.py` | yes | not yet verified on the lecture |
| 11 | `link` | `link.py` | yes | **yes** — `STAGE = 11`, ran in the 12-stage run |
| 12 | `report` | `report.py` | yes | **yes** — caught a bug in itself, first real input |

Stages 7–10 and 1–2 are not "broken" or "missing". They are written, imported by
`pipeline.run`, and covered by `tests/test_stages.py`. What they lack is a **verified run
over the lecture**, which is a different and cheaper claim to earn.

## Stage 6 — the open one

Stage 6 is two halves. One works, one does not exist.

**Working — alignment.** `caption.run()` matches each frame to the transcript segments that
overlap its display window, with the boundary at the first word whose timestamp reaches the
frame start. Deterministic, no model involved, cached into `report` and `provenance`.

**Absent — captioning.** There is no vision client in this codebase. `llm.py` is text-only:

```
$ grep -niE 'image|vision|b64|base64|multimodal|image_url' src/glimpse/llm.py
(no matches)
```

The code says so itself at `caption.py:383`:

```
captioning is NOT_CONFIGURED -- no vision client in this codebase
```

and records `"caption": null, "caption_status": "NOT_CONFIGURED"` into the stage artefact.

Consequence for MVP: the conspectus is written without knowing what was on the slides.
Formulas that exist only on screen are absent from the note, and nothing downstream
notices. Tracked as issue #67.

Stage 5 is **not** open, despite `VLMEstimator` raising at `quality.py:521`. That class is
one of three registered names in `resolve_estimator`; the default is `geometric`
(`pipeline.py:245`), which is implemented and measured. `VLMEstimator` is dead code and
should be deleted, not repaired.

## Verification status

Three claims are load-bearing and currently unverified:

1. **A full 12-stage run over the real lecture has not completed successfully.** ADR-0001's
   amendment of 2026-10-04 records a 12-stage run, but stage 6 was a no-op in it and stage 3
   was served differently. A run that includes stage 6 has not happened.
2. **`tests/test_stages.py` and `tests/test_doctor.py` fail when `GLIMPSE_*` is exported**
   into the environment. Reproduced on pristine `main` — pre-existing, not a regression,
   filed as issue #63. The suites are not hermetic, which means "the tests pass" currently
   means "the tests pass in a clean shell".
3. **The output has not been compared against the hand-written baseline.** That comparison is
   the check that matters to the reader, and it has not been run.

## Claims in the code that are currently false

`src/glimpse/stages.py` states:

```python
IMPLEMENTED = 12
#: Stage 0 is `doctor`, run by the CLI. Every stage D3 lists is built
REMAINING_NOTE = "every stage D3 lists is built"
```

Both claims are false today — stage 6 is half built. The module's own docstring gives the
right rule and then breaks it:

> `[6/12] caption` on a six-stage pipeline is a claim about the software, not a formatting
> preference.

Two defects in the same six lines:

- **The count is a claim.** `IMPLEMENTED = 12` makes every progress line assert a stage
  exists. It becomes true when #67 lands; it is a lie until then.
- **The citation is wrong.** The 12-stage list is in ADR-0001 § *The pipeline*, not in
  `D3`. `D3` is about reading frames from the source. This is an instance of the
  unqualified `D<n>` problem in issue #66 — and here the number is not merely ambiguous,
  it points at the wrong decision.

Neither is fixed here. Changing `IMPLEMENTED` alters every progress line and the CLI's own
output, and that is a decision to make when #67 lands, not before.