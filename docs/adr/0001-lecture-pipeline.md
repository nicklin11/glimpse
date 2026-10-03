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

The package is `glimpse`. The subcommand surface carries the meaning:

```
glimpse process <video|audio>   # full pipeline
glimpse audit <note.md>         # audit an existing note
glimpse doctor                  # report missing/conflicting dependencies
```

Rationale: a CLI binary name cannot teach another agent what a tool does. `--help`,
the repository description and the skill description can. Names containing `ai`,
`auto` or `smart` were rejected — they make a promise that cannot be verified before
running the tool, and they go stale when the backing model changes.

**Amendment 2026-10-02 — renamed `glimpse` → `glimpse`.** The rule above is unchanged;
only the name is. Recorded because this section previously named `glimpse` as the
choice, and an ADR that names a different package than the repository ships is a
document that actively misleads.

Two honest notes on the new name:

- It sits closer to the line the rule draws than `glimpse` did. A "glimpse" suggests
  a partial view, and this tool produces a complete, audited note. The mitigation is
  the same one that always applied: the description, `--help` and the skill text
  carry the meaning, and they are what another agent reads first.
- `glimpse` is **taken on PyPI** ("Hierarchical visual models in C++ and Python").
  That is irrelevant for a `pipx install` from git, and only becomes a problem if
  the package is ever published under that name. Recorded now so the collision is not
  rediscovered later.

### D2 — glimpse depends on `shipboard`, and does not reimplement STT

STT is delegated: `shipboard process PATH` already transcribes an existing file to
stdout. whisper.cpp runs **CPU-only by design**, which keeps roughly 1.5 GiB of VRAM
free for llama-swap. A second STT stack inside glimpse would duplicate that
configuration and silently compete for the same resources.

**Error propagation is part of the contract.** glimpse captures shipboard's stderr
and surfaces it verbatim. If `shipboard` is absent, `glimpse doctor` says so with a
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
glimpse process ~/Videos/lectures/.../1_lecture_OCS.webm

  0  doctor      verify ffmpeg/ffprobe, shipboard, gateway reachability, vault
  1  probe       ffprobe: streams, duration, codecs -> decide audio-only or not
  2  audio       extract 16 kHz mono wav to a managed temp dir (kept on failure)
  3  stt         shipboard process --timestamps json -> raw transcript
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
| 130 | interrupted (128 + SIGINT); work dir kept |

An uncaught internal error — a bug in glimpse — has no row on purpose: it
propagates as a traceback. The work dir is retained and its path printed first.

## Amendment 2026-10-02 — stages 0-3 shipped, and two corrections

Recorded because the decision text above described a pipeline whose stage 3 was
still blocked, and an ADR that describes a state the code has left behind is a
document that actively misleads.

**Stage 3 is no longer blocked.** `shipboard#6` landed
([PR #7](https://github.com/nicklin11/shipboard/pull/7)). Two of its premises were
measured wrong, and both corrections changed the consumer:

- the default `response_format=json` returns **only** `{"text": ...}` — segments
  exist solely under `verbose_json`, so there was nothing to read off the plain
  transcript. Timestamps are consumed from `--timestamps json`, never reparsed
  out of stdout text;
- `start`/`end` are **seconds as floats**, not milliseconds.

Per-**word** timings come with the same payload. Stages 5-7 bind frames to
paragraphs by word span rather than segment bounds, because segment edges are
decoder artefacts and will not line up with paragraph structure. The `word`
field holds BPE pieces (`" Ин"`, `"ст"`, `"ит"`), so they are carried verbatim
and never detokenised: reassembling them needs whisper.cpp's exact vocabulary,
and a wrong guess corrupts the transcript while leaving every timestamp
plausible.

**Stage 1 ships with stage 2-3.** The stage table assigns no issue to `probe`,
but stage 2 has nothing to extract from without it, so it shipped inside #5
rather than being deferred or, worse, hardcoded. An audio-only input is
**rejected** (exit 1), not accepted: *Not doing* already excludes it, and stage 4
has no frames to extract from an audio file.

**Measured stage costs**, for D7's weights. One end-to-end run on lecture 1
(`1_lecture_OCS.webm`, 4396 s container, 4520 s of audio):

| Stage | Wall | Rate |
|---|---|---|
| 1 `probe` | 0.0 s | — |
| 2 `audio` | 6.4 s | 138 MiB wav written |
| 3 `stt` | 257.2 s | **0.057x realtime** |

STT still dominates the implemented stages by ~40x over extraction, and the
audit (~20 min) will dominate the whole pipeline. Note the short-clip trap: a
25 s probe measured 0.20x because the whisper.cpp warm-up is a fixed cost that a
four-minute lecture amortises away. A progress bar weighted from short-clip
numbers would overestimate STT by 3.5x.

The run produced 1677 segments and 21957 timed words, speech covering 99.88% of
the audio, with zero anomalies.

**The preflight gate is scoped to what the run uses.** Stage 0 as a gate must
not be broader than the stages it guards: `ffmpeg`, `ffprobe` and `shipboard` are
fatal for `process`, the vault and the gateway are not. Stages 0-3 write only into
the managed work dir and never call the gateway, so refusing to transcribe
because stage 12's output directory is missing — or because stage 5's VLM backend
is offline — would be a gate protecting nothing. `glimpse doctor` keeps both
fatal, because its job is to report the whole environment.

**`glimpse process` exits 1 until stage 12 exists.** Exit 0 means "all artefacts
written and verified" in the table above, and no note is produced yet, so a
successful partial run claiming 0 would be the silent degradation D2 exists to
prevent. The work dir is kept on failure and its path printed: a failed
transcribe that discards its extracted wav costs a 73-minute re-extraction to
debug.

## Amendment 2026-10-03 — stage 4 shipped, and the frame set is not reproducible without a record

`frames` was not written from scratch. It is a move of `lecture-frames`
(`~/.local/bin`, 294 lines), which was the only place the D3 blur fix existed.

**The port is byte-identical to the script it replaces.** Verified by running both on
`1_lecture_OCS.webm` and comparing: `manifest.tsv` is identical, all 16 frames compare
equal under `cmp`, total 2 382 320 bytes on both sides. Three independent runs today
produced the same 16 timestamps, so detection is deterministic here.

**Measured stage cost.** Stage 4 is **287 s** end to end: 232 s detect (a full decode of
the container) and ~55 s extract. That is **0.065x realtime** and it puts stage 4 within
11% of stage 3's 257 s. The amendment above said STT "still dominates the implemented
stages by ~40x over extraction" — true for audio, **no longer true for the pipeline**:

| Stage | Wall | Rate |
|---|---|---|
| 1 `probe` | 0.0 s | — |
| 2 `audio` | 6.4 s | 138 MiB wav written |
| 3 `stt` | 257.2 s | 0.057x realtime |
| 4 `frames` | 287.0 s | **0.065x realtime** |

Stage 4 does not consume the audio at all, so it cannot be folded into stage 3's budget
and cannot be amortised by a longer lecture any differently.

**D7 requires a note that this cost is paid twice if it is not reused.** Detect is the
whole-container decode; re-rendering frames at a different output width must not pay it
again, so `run()` takes `reuse_manifest=True` to skip the detect pass. That is the only
reason the manifest format was kept at all.

**The stored manifest in the vault is not a golden file, and the port found out why.**
`frames/Лекция 1. 01.10.26/manifest.tsv` lists **17** states; the same ffmpeg build
(n9.0.2, 2026-09-22) on the same unmodified source lists **16**, with only 9 timestamps
in common. Same binary, same input, 28 hours apart — so the difference is the *settings*,
not the environment, and the manifest alone does not record which settings. `run()` now
writes `detect.json` beside it: the resolved filter string, every `Settings` field, the
ffmpeg build, and whether the manifest was reused. Without that record a later reader
cannot tell whether the frames or the code changed, and the set cannot be reproduced.

**The retry for ffmpeg's input-open failure was masking a different fault.** The ported
script retried `Error opening input` four times unconditionally, and on this host that
masked a **renamed parent directory**: ffmpeg reported "No such file or directory" five
times in a row for a file that was simply at a new path, and the script's own message
blamed ffmpeg for what was a missing input. `run()` re-checks `source.is_file()` before
each attempt and raises **exit 1, usage** if the path is gone. Only a source that is
verified present on every attempt still earns the retry, and its message says so.

**Zero frames is a failure, not an empty success.** The ported script wrote a
header-only manifest and exited 0. That is exactly the D3 failure mode — artefact counts
verified on disk, never inferred from an exit code — and it is how a 27-minute extraction
could produce nothing and still report success. `run()` raises **exit 3** on a detector
that selects nothing, and again, one level deeper, when ffmpeg exits 0 having written no
frame file.

**Stage 4 does not honour `--max`.** The script's `--max` cap on frame count was dropped:
it samples the *detector's* output before extraction, so a capped run still pays the
whole 232 s decode and then discards work. Sampling belongs to stage 6 (ADR-0005), where
the frames are read by a VLM and the cost is real money.

## Amendment 2026-10-03 — D2 delegated to an endpoint, D9 to a local bundle

Two decisions changed because the code was measured rather than reasoned about.

### D2 — STT is delegated to an *endpoint*, not to a utility

`TranscriptionBackend` is the boundary: `check()` answers "can you transcribe", `run()`
returns the raw payload, and nothing above the backend parses anything. shipboard is one
implementation and is **no longer mandatory**; `doctor` probes whichever backend is
configured.

**The premise this replaced was wrong on this host.** whisper.cpp does not serve the
OpenAI-compatible routes:

```
GET  /health                    -> 200
GET  /v1/models                 -> 404
POST /v1/audio/transcriptions   -> 404
POST /inference                 -> 200
```

An OpenAI-only backend cannot run here, so both wire formats exist: the native
`/inference`, and an OpenAI-compatible adapter for hosted endpoints.

**shipboard does normalise the envelope.** `/inference` returns an object
(`{task, language, duration, text, segments, ...}`); shipboard returns a bare array. The
segment contents are identical — same `words` keys, same float seconds — but a parser
written for either shape alone would have failed on the other. `core.parse` accepts both
and records the envelope as an anomaly, so a payload that changes shape is visible rather
than silent.

**D2's verbatim-diagnostics guarantee is preserved, not dropped.** shipboard is a
subprocess and had stderr; an HTTP backend has none. The intent — never swallow a backend's
diagnostics — is carried over as the verbatim response body plus the transport-level
message, and HTTP status failures attach the response body as `stderr`. Losing the guarantee
silently would have turned a backend failure into an empty transcript with exit 0.

**The endpoint is not reproducible.** Measured, 25 s clip, three runs per configuration:

| request fields | identical 3x | segment counts |
|---|---|---|
| shipboard's own | no | 11, 11, 11 |
| `temperature` + `no_timestamps` | no | 11, 11, 11 |
| `threads=1` | no | 10, 11, 11 |
| `threads=2` / `threads=4` | no | 11, 11, 11 / 11, 11, 10 |
| after a fresh container restart | no | 11, 10, 11 |

Differences are punctuation and a segment that appears or does not, on **identical
timings**. Ruled out: thread count down to 1, language on or off, temperature on or off,
container state. Cause not identified. This is why two runs over the same 4520 s audio
produced 1677 and then 1520 segments.

Consequences, both of which change what "verified" means here:

- `transcript.json` is **not** a reproducible artefact. D3's rule — verify on disk, never
  infer from an exit code — still holds, but "the same run" is not a stable reference.
- Frame-to-paragraph binding uses word spans, and the timings *were* stable across
  non-identical runs, so stages 5-7 are unaffected. Only the wording moves.

Stage 4 is the opposite: `frames` output is deterministic, three runs gave the same 16
timestamps. A regression check can still be exact on the frame half.

### D9 — the vault is an export target, not the artefact root

Deliverables go to a **bundle**: `--output-dir`, else `$XDG_STATE_HOME/glimpse/<lecture>`.
The vault, when given, is copied into.

**Not `./output/<lecture>`.** D9 exists because artefacts written into a working directory
end up in whatever repository the user is standing in. A CWD-relative default reintroduces
that one level down — a 137.9 MiB wav and a 3.7 MB transcript in a git checkout. XDG
state is per-user, per-purpose, and the conventional home for run output that should
survive.

Cyrillic directory names are preserved rather than transliterated. The lectures here are
`Оптимальные СУ`; rendering that as `Optimal Control Systems` would give a directory whose
name does not match its contents.

**One writer, one verification.** `export_to_vault` copies and then compares each
destination against its source. Two write paths with different semantics is how a tool
ends up with half a note in the vault; a copy that lands short raises rather than being
reported as done, and the bundle — the single writer — is left intact.

The work dir and the bundle are separate because their lifecycles are opposite. The work dir
is scratch, removed on success. `audio.wav` deliberately stays there: it is 137.9 MiB,
reproducible from the source in 6.3 s, and nothing downstream reads it.

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