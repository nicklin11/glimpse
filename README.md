# glimpse

One command turns a lecture recording into a readable, **independently audited**
Markdown note.

```
glimpse process ~/Videos/lectures/.../1_lecture_OCS.webm
glimpse audit note.md        # audit a note that already exists
glimpse doctor               # what is missing, what is broken
```

## What it does

- transcribes the recording (via `shipboard` / whisper.cpp),
- extracts the distinct on-screen states as sharp frames,
- reads the frames with a vision model and attaches them to the right paragraphs,
- synthesises the note,
- checks every formula mechanically, then audits the note in a **separate model
  context** that did not write it,
- links course terminology across lectures.

## What it does not do

- It does not recover the lecturer's blackboard work — it is not in the recording.
- It does not invent detail the transcript does not contain. Where the lecture was
  thin, the note says so.
- It does not make the audit infallible. A model that checks formulas is wrong in
  predictable ways, which is why findings must be cited and why mechanical checks sit
  underneath. See [ADR-0001, D6](docs/adr/0001-lecture-pipeline.md).

## Why an audit stage exists

An audit of a finished note found 8 high-confidence errors, 3 of them rank/dimension
errors. The worst: one section carried three mutually exclusive representations of
the same dynamical system, where the formulas contradicted both the note's own prose
and physics. The author had no way to see it.

Full write-up, with numbers: [`docs/adr/0001-lecture-pipeline.md`](docs/adr/0001-lecture-pipeline.md).

## Status

Implementation stage. ADR-0001 accepted; the pipeline is tracked in
[#3](https://github.com/nicklin11/glimpse/issues/3) with one sub-issue per stage.

| Stage | Status |
|---|---|
| 0 `doctor` | **done** — [`#4`](https://github.com/nicklin11/glimpse/issues/4) |
| 1 `probe` | **done** — shipped with [`#5`](https://github.com/nicklin11/glimpse/issues/5) |
| 2-3 `audio` + `stt` | **done** — [`#5`](https://github.com/nicklin11/glimpse/issues/5) |
| 4 `frames` | open, [`#7`](https://github.com/nicklin11/glimpse/issues/7) |
| 5 `quality` | open, [`#9`](https://github.com/nicklin11/glimpse/issues/9) |
| 6 `captions` | open, [`#10`](https://github.com/nicklin11/glimpse/issues/10) |
| 7 `synth` | open, tracked in [`#3`](https://github.com/nicklin11/glimpse/issues/3) |
| 8 `lint` | open, [`#6`](https://github.com/nicklin11/glimpse/issues/6) |
| 9 `audit` | open, [`#11`](https://github.com/nicklin11/glimpse/issues/11) |
| 10 `repair` | open, tracked in [`#3`](https://github.com/nicklin11/glimpse/issues/3) |
| 11 `link` | open, [`#8`](https://github.com/nicklin11/glimpse/issues/8) |
| 12 `report` | open, tracked in [`#3`](https://github.com/nicklin11/glimpse/issues/3) |

`glimpse doctor` works. `glimpse process` runs stages 0-3 — probe, extract audio,
transcribe — and **exits 1 until stages 4-12 exist**, because no note is written
yet and exit 0 means "all artefacts written and verified" in the table below.
`glimpse audit` exits 1 with a pointer to its tracking issue.

### Running the tests

```sh
python tests/test_doctor.py     # stage 0: the exit-code contract
python tests/test_stages.py     # stages 1-3: contracts and failure modes
ruff check src tests && ruff format --check src tests
```

Both suites are plain scripts, not pytest: no fixtures, no plugins, and no
network or real binaries — `runner.run` is stubbed. The real end-to-end run is a
command, not a test:

```sh
glimpse process ~/Videos/lectures/.../1_lecture_OCS.webm --keep-workdir
```

`--keep-workdir` is how a failure gets inspected. The work dir is removed when a
run succeeds and **kept when it fails**, with its path printed — otherwise
debugging a failed transcribe means re-extracting 73 minutes of audio to look at
the wav that was already there.

### Artefacts

Stages 1-3 write into the managed work dir, not the vault: stage 12 publishes.
Stage 3 writes three files with three distinct consumers — `transcript.raw.json`
(the shipboard payload verbatim, `tokens` and `avg_logprob` included),
`transcript.json` (normalised timings, plus run metadata), and `transcript.txt`
(`[hh:mm:ss]` per segment, for reading).

### Exit codes

The exit-code table in ADR-0001 is a contract, and `src/glimpse/exitcodes.py` is
its single source of truth so the document and the binary cannot drift:

| Code | Meaning |
|---|---|
| 0 | success; all artefacts written and verified on disk |
| 1 | usage error, including a subcommand not implemented in this build |
| 2 | missing dependency — named, with a remediation line |
| 3 | dependency present but failed — its stderr reproduced **verbatim** |
| 4 | quality gate failed — note written, report says why |
| 5 | audit found errors above threshold — note written and flagged |
| 130 | interrupted (128 + SIGINT) — work dir kept |

An uncaught internal error — a bug in glimpse itself — deliberately has no row.
It propagates as a traceback, which is louder than any summary line. The work
dir is retained and its path printed before it does.

## Dependencies

`ffmpeg` / `ffprobe`, [`shipboard`](https://github.com/nicklin11/shipboard) (which
brings whisper.cpp), and a model gateway for the vision and audit stages. Run
`glimpse doctor` to check.

Two failure modes are kept apart on purpose: *not installed* (exit 2, with a
remediation line) and *installed but broken* (exit 3, with its own stderr
reproduced verbatim). Collapsing them into one "not working" state would lose
exactly the information a user needs — whether to install something or fix
something.

`glimpse process` gates only on what it actually invokes: `ffmpeg`, `ffprobe`
and `shipboard`. The vault and the model gateway are reported but **not**
enforced, because stages 0-3 write nothing to the vault and never call the
gateway — a run must not refuse to transcribe because the stage-5 vision backend
happens to be down. `glimpse doctor` still treats both as fatal, because its job
is to report the whole environment.

The vault location defaults to `~/Documents/obs_notes` and the model gateway is
unconfigured by default. Override with `GLIMPSE_VAULT` and `GLIMPSE_GATEWAY_URL`.

## Install

```sh
pipx install -e ~/Coding/glimpse
```

## Privacy

Lecture content stays in your vault. Frames contain whatever the conferencing UI
shows and are gitignored; nothing in this repository may contain participant names,
personal IPs or hostnames.