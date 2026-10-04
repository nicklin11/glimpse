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
| 6 | `caption` | `caption.py`, `llm.py` | yes | **yes** — verified live against the gateway, 2026-10-04 |
| 7 | `synth` | `synth.py` | yes | not yet verified on the lecture |
| 8 | `lint` | `lint.py` | yes | not yet verified on the lecture |
| 9 | `audit` | `audit.py` | yes | not yet verified on the lecture |
| 10 | `repair` | `repair.py` | yes | not yet verified on the lecture |
| 11 | `link` | `link.py` | yes | **yes** — `STAGE = 11`, ran in the 12-stage run |
| 12 | `report` | `report.py` | yes | **yes** — caught a bug in itself, first real input |

Stages 7–10 and 1–2 are not "broken" or "missing". They are written, imported by
`pipeline.run`, and covered by `tests/test_stages.py`. What they lack is a **verified run
over the lecture**, which is a different and cheaper claim to earn.

## Stage 6 — what was added, and what it is still not

Stage 6 was two halves: a deterministic alignment that worked, and a captioning half that
had **no vision client at all**. `llm.py` was text-only — `grep -niE 'image|vision|b64|base64|multimodal|image_url' src/glimpse/llm.py` returned nothing — and `caption.py` said so itself:

```
captioning is NOT_CONFIGURED -- no vision client in this codebase
```

That string is gone. Stage 6 now builds an OpenAI-compatible message carrying the frame as
a `data:` URI and calls the same `chat()` the text stages use; the transport was never the
missing part, only the message shape and the configuration were.

**Verified live**, 2026-10-04, against `http://100.64.0.2:8317/v1` with
`opencode-go/glm-5.3-flash`, on two real frames from lecture 1:

| frame | time | result |
|---|---|---|
| `f_0000000000.jpg` (53 KiB) | 1.5 s | `NO NEW INFORMATION` — a title screen with nothing the audio did not already carry |
| `f_0000617096.jpg` (163 KiB) | 4.6 s | a full transcription of the references page: four numbered courses, the first literature entry, and the screen-share banner occluding a line |

17 frames at that rate is roughly a minute of wall clock for the whole stage.

The second caption also shows the provenance chain working: `served_model` came back as
`glm-5.3-flash` while the requested id was `opencode-go/glm-5.3-flash`. Both are recorded,
and the disagreement is visible rather than resolved silently (#61).

### Failure behaviour, which changed deliberately

The old code wrote `caption_status: NOT_CONFIGURED` into the artefact and exited 0. Now:

| Situation | Behaviour |
|---|---|
| No vision endpoint | every frame marked `NO_ENDPOINT`, run **continues**, exit non-zero |
| Endpoint configured but refusing | per-frame `ERROR` with the endpoint's message |
| Every frame failed | exit 3, `DEPENDENCY_FAILED`, with the per-frame reasons |
| Caption returned but empty | `EMPTY`, counted as a failure |

The first row follows the contract stage 7 already has: `TemplateSynthesizer` exists so the
pipeline runs end to end with no model configured, and stage 6 must not be the stage that
breaks it. A note written without slide content is a worse note, not no note — but it is a
worse note the run must say out loud.

## Stage 5 is **not** open, despite what the code used to say

`resolve_estimator` registered three names. `vlm` raised `NotConfiguredError` on both
branches, so `GLIMPSE_BBOX_SOURCE=vlm` failed at the crop instead of being refused as the
unknown name it is. It has been removed from the registry (#67): a vision bbox source is a
real idea that is still unbuilt, and an unbuilt option should be absent rather than present
and broken.

Stage 5 itself works through `GeometricEstimator`, the default.

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

## Claims in the code that are now true

`src/glimpse/stages.py` states:

```python
IMPLEMENTED = 12
REMAINING_NOTE = "every stage in ADR-0001 § The pipeline is built"
```

Both were false while stage 6 had no vision client, and both are true as of #67. The count in
every progress line is an accurate claim again.

Two defects in those six lines were real and are fixed:

- **The citation was wrong.** The module said "every stage D3 lists". `D3` is about reading
  frames from the source; the twelve-stage list is in ADR-0001 § *The pipeline*. An instance
  of the unqualified `D<n>` problem in #66 where the number did not merely collide, it
  pointed at the wrong decision.
- **The module's own docstring gave the rule the constant broke** — "`[6/12] caption` on a
  six-stage pipeline is a claim about the software, not a formatting preference."

What `IMPLEMENTED = 12` still is not: a measurement. It is a declaration. If a stage is ever
opened again, this constant is what will keep claiming the stage is closed, and nothing in
the test suite catches that.