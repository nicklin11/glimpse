# ADR 0001: One-command lecture pipeline (video -> audited note)

- **Status:** Accepted
- **Date:** 2026-10-02
- **Scope:** MVP. GUI and local vision are explicitly later milestones.

## Context

A lecture arrives as a video or audio file. The goal is a single Markdown note that
is readable, usable for exam preparation, and — critically — **mathematically
trustworthy**.

The synthesis step is performed by an LLM from a noisy ASR transcript. That step
produces fluent prose that can contain sign errors, rank/dimension errors and
internally inconsistent formulations. The author cannot reliably detect these:
it does not know when it is wrong.

This was measured, not assumed. An independent audit of a finished note found 8
high-confidence errors, including one where three representations of the same
dynamical system contradicted each other and the note's own prose (see
[Evidence](#evidence-the-error-that-justifies-this-pipeline)).

The pipeline therefore needs two properties that pull in opposite directions:

1. It must be **automated to one command**, because the manual path (transcribe,
   extract frames, write note, link terms, review) is long enough that it does not
   get done every lecture.
2. Its output must be **verified by something other than its author**, because the
   author's blind spot is exactly the thing we need checked.

## Decision

### D1 — The name carries no meaning; the interface does

The package is `snitchin`. The subcommand surface carries the meaning:

```
snitchin process <video|audio>   # full pipeline
snitchin audit <note.md>        # audit an existing note
snitchin doctor                 # report missing/conflicting dependencies
```

Rationale: a CLI binary name cannot teach another agent what a tool does. `--help`,
the repository description and the skill description can. Names containing `ai`,
`auto` or `smart` were rejected — they make a promise that cannot be verified before
running the tool, and they go stale when the backing model changes.

### D2 — snitchin depends on `shipboard`, and does not reimplement STT

STT is delegated: `shipboard process PATH` already transcribes an existing file to
stdout. whisper.cpp runs **CPU-only by design**, which keeps roughly 1.5 GiB of VRAM
free for llama-swap. A second STT stack inside snitchin would duplicate that
configuration and silently compete for the same resources.

**Error propagation is part of the contract.** snitchin captures shipboard's stderr
and surfaces it verbatim. If `shipboard` is absent, `snitchin doctor` says so with a
remediation line; `process` refuses to start rather than silently degrading. A tool
that swallows its dependency's errors is worse than no tool, because the failure
looks like success.

### D3 — Frames are read from the source, never from a blurred intermediate

Two passes:

1. `detect()` — decode at reduced width (640 px), blur, `mpdecimate`, `select` on
   change. Blur here is **only** a detector pre-filter.
2. `extract_at()` — seek the original source per timestamp, emit a single 1600 px
   frame at q=2.

Two bugs were found and fixed during this work, and are recorded because they are
the kind that recur:

- The blur was originally in the output path, so every emitted frame was blurred.
  Measured Laplacian variance was 1–3.6 (blurred) versus 686–3483 (correct).
- `select='gte(t-prev_selected_t,GAP)'` silently selected **zero** frames:
  `prev_selected_t` initialises to NaN, `t - NaN` is NaN, the comparison is false,
  and ffmpeg does not fail — it writes an empty file and exits 0. Fixed with
  `isnan(prev_selected_t) + gte(...)`.
- `mpdecimate` thresholds are absolute sums over 8x8 blocks, so a blur radius tuned
  for 640 px is wrong at 1280 px. The radius now scales with the detection width.

A 27-minute run that produced zero frames exited successfully. Any pipeline that
reports success must verify artefact counts on disk, not the exit code.

### D4 — Frame quality is gated, and the fix is crop, not a better model

Frames are poor because the screen is mostly UI chrome, not because the model is
weak. Measured: 17 distinct states over 73 minutes.

The pipeline asks the VLM for the document bounding box, crops to it, upscales the
crop 2x, applies an unsharp mask, and then gates the result on a sharpness measure
(Laplacian variance). Below threshold, the frame is re-cropped and re-measured.

Honest limit: the source is VP8 at 1.6 Mbps. Interpolation adds no information. If
the on-screen text is small, it cannot be recovered, and the quality gate should
**report** that rather than pretend otherwise.

### D5 — The audit is a separate model context, and it never rewrites

The auditor receives the finished note and the raw transcript, and reviews the note.
It is not asked to check its own work; it did not write it.

Findings format is enforced:

- every finding carries an **exact quote** from the note and a citation into the
  transcript;
- every finding carries a confidence level;
- the auditor is explicitly permitted to answer "not sure";
- **a finding without a citation is discarded.**

The auditor does not edit the note. It emits a findings list. Repairs are a separate
action with their own log, so that "what was wrong" and "what was changed" stay
distinguishable.

### D6 — Math verification is layered, mechanical layers first

An LLM asked to check formulas is **unreliably** correct. The reason it worked here is
that the prompt carried an explicit rank/dimension clause; two of the errors it found
were exactly the class an LLM is worst at. That is a fragile foundation, so the
architecture does not rest on it:

| Layer | Catches | Determinism |
|---|---|---|
| LaTeX render check | formulas that do not compile | exact |
| Rank/dimension check | scalar vs vector, index mismatches | exact on the covered subset |
| Cross-lecture contradiction | same term defined differently in two notes | high |
| Agent audit | sign errors, `⪰` vs `succ`, physically impossible claims | probabilistic |

The measured run supports this ordering: the mechanical layers cover the
rank/dimension class, and the agent layer caught what no script can — the sign of the
gravitational term, a semidefinite matrix asserted to give a unique global minimum,
and a non-symmetric matrix asserted to be positive definite.

### D7 — Progress is stage-level in the MVP, and the reason is a code fact

`shipboard` sends **one** blocking HTTP request to whisper.cpp and waits; there is no
streaming and no per-segment callback. A per-segment progress bar is not available
without a change on the whisper server side.

So MVP shows stage-level progress with weights derived from measured cost, and the
audit stage dominates: ~20 minutes and ~106k context tokens, versus ~$0.01 for
vision. A progress bar that showed 90% during STT and then sat still for 20 minutes
would be a lie about where the time goes.

### D8 — The vision backend is pluggable; local is deferred

Default backend is the gateway (`opencode-go/deepseek-v4-flash-vision-exp`), verified
working on Russian PDF pages, ~$0.01 per 73-minute lecture. A local llama.cpp +
mmproj backend is a configuration swap, not a rewrite — but it is not the default,
because a 7B VLM on this hardware reads dense Russian technical text worse.

### D9 — Artefacts live in the vault, not in this repository

Transcripts, frames and audit reports are outputs, and they live beside the note in
the Obsidian vault. The repository carries code, ADRs and issue history only.

Frames are gitignored (`mscs/**/frames/`) — they are large, regenerable, and contain
names visible in the conferencing UI. Nothing in the repository may contain a
participant name, a personal IP or a hostname.

## The pipeline

```
snitchin process ~/Videos/lectures/.../1_lecture_OCS.webm

  0  doctor      verify ffmpeg/ffprobe, shipboard, gateway reachability, vault
  1  probe       ffprobe: streams, duration, codecs -> decide audio-only or not
  2  audio       extract 16 kHz mono wav to a managed temp dir (kept on failure)
  3  stt         shipboard process -> raw transcript
                 shipboard currently discards segment timestamps  -> see #2
  4  frames      two-pass detect/extract -> frames/<name>/*.jpg + manifest.tsv
  5  quality     VLM returns document bbox -> crop, upscale, unsharp, gate
  6  captions    VLM caption per frame, cached by (frame hash, model id)
  7  synth       apply academic-lecture-synthesizer to raw + captions
  8  lint        LaTeX render check + rank/dimension check       [mechanical]
  9  audit       separate context, note + transcript, cited findings
 10  repair      apply confirmed findings -> fix log             [separate step]
 11  link        mscs-termlink, idempotent, must not link inside math
 12  report      note + audit report + fix log in the vault

  progress bar spans 0-12, weighted by measured cost
```

Exit codes distinguish causes, because a pipeline that fails silently is worse than
one that is loud:

| Code | Meaning |
|---|---|
| 0 | success; all artefacts written and verified |
| 1 | usage error |
| 2 | missing dependency (named, with remediation) |
| 3 | dependency failed (its stderr is reproduced verbatim) |
| 4 | quality gate failed — note written, audit report says why |
| 5 | audit found errors above threshold; note written and **flagged** |

## Evidence: the error that justifies this pipeline

Section 7.3 of the finished lecture 1 note contained three mutually exclusive
representations of the Kapitza pendulum. The ODE carried `+(g/l)·sin θ`, and the
(2,1) element of both `A` matrices carried the same sign — while the prose stated the
correct eigenvalues.

Verified numerically at g=9.81, l=1, k=0.3:

```
as written (+g/l):  theta=0 -> lambda = ±2.99, -3.29   => SADDLE
correct  (-g/l):     theta=0 -> lambda = -0.15 ± 3.13i => STABLE
```

The formulas contradicted the note's own text and contradicted physics. Two further
rank/dimension errors were of the same species: a scalar added to a vector, and a
`(1x2)(2x1)` product described as yielding a vector — the latter written explicitly as
a demonstration that dimensions *did* agree.

The errors were fixed and re-verified: with the corrected sign, the lower equilibrium
is stable (centre at k=0) and the upper is a saddle, matching both the prose and the
physics.

## Consequences

**Good**

- Formula defects that the author cannot see are found before the note is used.
- Findings are citable and attributable: synthesis defects (absent from the raw
  transcript) are separable from mis-transcribed lecturer speech.
- The pipeline is honest about its own limits — quality gates report failure instead
  of producing plausible-looking output.

**Bad**

- The audit is the slowest stage by a wide margin (~20 min). This is a real cost and
  the progress bar must not hide it.
- An audit that is wrong is a *worse* failure than no audit, because a human who
  cannot check formulas will trust it. This is why D5 requires citations and D6 puts
  mechanical layers underneath: the agent layer is never the only line of defence.
- Frame quality is capped by the source bitrate, and 17 states over 73 minutes means
  this input is visually sparse. The pipeline must not oversell visual recovery.

## Not doing (MVP boundaries)

- GUI. Later milestone; the CLI is the product.
- Local VLM as the default vision backend.
- Audio-only input path beyond what the video probe naturally produces.
- Cross-lecture RAG. The term catalog and Backlinks already provide cross-lecture
  indexing; a vector index would duplicate it.
- Automatic repair without review (D5).

## Open questions

1. Which model should be the default auditor? `mimo-v2.6-flash` was measured on one
   note and passed, but one note is not a benchmark. The right test is planted errors
   with a measured recall, run across candidate models.
2. Should the audit result block the note's write, or annotate it? Currently the
   proposal is annotate-and-flag (exit 5), which trades away a hard guarantee for
   never losing work.
3. Is `mmproj` worth evaluating at all, given the frames are already sharp?
EOF