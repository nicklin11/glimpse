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
| 2-3 `audio` + `stt` | open, [`#5`](https://github.com/nicklin11/glimpse/issues/5) |
| 4 `frames` | open, [`#7`](https://github.com/nicklin11/glimpse/issues/7) |
| 5 `quality` | open, [`#9`](https://github.com/nicklin11/glimpse/issues/9) |
| 6 `captions` | open, [`#10`](https://github.com/nicklin11/glimpse/issues/10) |
| 8 `lint` | open, [`#6`](https://github.com/nicklin11/glimpse/issues/6) |
| 9 `audit` | open, [`#11`](https://github.com/nicklin11/glimpse/issues/11) |
| 11 `link` | open, [`#8`](https://github.com/nicklin11/glimpse/issues/8) |

`glimpse doctor` works. `glimpse process` and `glimpse audit` exist as
subcommands but exit 1 with a pointer to their tracking issue — they are not
silently absent, and not falsely present.

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

## Dependencies

`ffmpeg` / `ffprobe`, [`shipboard`](https://github.com/nicklin11/shipboard) (which
brings whisper.cpp), and a model gateway for the vision and audit stages. Run
`glimpse doctor` to check.

Two failure modes are kept apart on purpose: *not installed* (exit 2, with a
remediation line) and *installed but broken* (exit 3, with its own stderr
reproduced verbatim). Collapsing them into one "not working" state would lose
exactly the information a user needs — whether to install something or fix
something.

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