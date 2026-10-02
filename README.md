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

Design stage. ADR-0001 accepted; implementation tracked in [#1](https://github.com/nicklin11/glimpse/issues/1).

## Dependencies

`ffmpeg` / `ffprobe`, [`shipboard`](https://github.com/nicklin11/shipboard) (which
brings whisper.cpp), and a model gateway for the vision and audit stages. Run
`glimpse doctor` to check.

## Privacy

Lecture content stays in your vault. Frames contain whatever the conferencing UI
shows and are gitignored; nothing in this repository may contain participant names,
personal IPs or hostnames.