# glimpse

Turns a lecture recording into a structured, **independently audited** Markdown note.

No third-party Python packages. Python's standard library plus `ffmpeg`.

```
glimpse process lecture.webm
glimpse doctor
```

## Status: stages 0-4 only

`glimpse process` **exits 1** until the remaining stages are built. Stages 1-4 run and
produce artefacts; stages 5-12 do not exist yet. This is deliberate — the exit code
distinguishes "failed" from "not built", and no stage is stubbed to look finished.

| stage | does | state |
|---|---|---|
| 0 | `doctor`: report what is missing and what to do | works |
| 1 | probe the recording | works |
| 2 | extract 16 kHz mono audio | works |
| 3 | transcribe, with word-level timings | works |
| 4 | extract distinct on-screen frames | works |
| 5 | read frames with a vision model | not built |
| 6 | synthesise the note | not built |
| 7 | link terminology across lectures | not built |
| 8 | check every formula mechanically | not built |
| 9 | audit in a separate model context | not built |
| 10 | repair what the audit found | not built |
| 11 | link references | not built |
| 12 | final report | not built |

What you get today is a transcript with word timings and a set of frames, in an output
directory. Not a note. This section says so rather than a screenshot implying otherwise.

## Install

```
git clone https://github.com/nicklin11/glimpse
cd glimpse
pipx install .
```

Requires Python 3.11+ and `ffmpeg`/`ffprobe` on `PATH`.

### As a single file, with no install

```
python -m zipapp src -m glimpse.cli:main -o glimpse -p "/usr/bin/env python3"
./glimpse doctor
```

## Configure transcription

Stage 3 delegates to an **endpoint**. Three backends ship:

| backend | selected by | speaks |
|---|---|---|
| `whispercpp` | autodetected | whisper.cpp native `POST /inference` |
| `openai` | autodetected, or `GLIMPSE_STT=openai` | OpenAI-compatible `/v1/audio/transcriptions` |
| [`shipboard`](https://github.com/nicklin11/shipboard) | autodetected | `shipboard process --timestamps json`, by the same author, MIT |

`GLIMPSE_STT=auto` (the default) probes an HTTP endpoint first and falls back to
`shipboard`. Whatever actually ran is recorded in the transcript, so a silent fallback shows
up in the output rather than surfacing later as a mystery.

| variable | purpose |
|---|---|
| `GLIMPSE_STT` | `auto`, `whispercpp`, `openai` or `shipboard` |
| `GLIMPSE_WHISPERCPP_URL` | default `http://127.0.0.1:10302` |
| `GLIMPSE_WHISPERCPP_LANGUAGE` | pin a language; unset means autodetect |
| `GLIMPSE_OPENAI_KEY`, `GLIMPSE_OPENAI_MODEL` | for a hosted endpoint |
| `GLIMPSE_GATEWAY_URL` | vision model for stage 5, when that stage exists |

`glimpse doctor` prints which backend was selected and whether it answers.

### whisper.cpp

Any build serving `POST /inference` works. Start one however you prefer — the official
server image, a package, a systemd unit — and point `GLIMPSE_WHISPERCPP_URL` at it. The
default is `http://127.0.0.1:10302`.

What that endpoint does and does not do, measured on the build this was developed against:

```
GET  /health                    -> 200
GET  /v1/models                 -> 404
POST /v1/audio/transcriptions   -> 404
POST /inference                 -> 200   -> object: {task, language, duration, text, segments, ...}
```

Two consequences worth knowing before you debug it:

- **It does not speak the OpenAI protocol.** `/v1/models` and `/v1/audio/transcriptions`
  are both 404. An OpenAI-compatible client pointed here will find nothing. Both wire
  formats are implemented here for that reason.
- **It must be told `response_format=verbose_json`.** The default returns only
  `{"text": ...}` — no timings. Stages 5-7 need per-word timings to bind a frame to the
  paragraph it belongs to. `glimpse` requests the verbose format, and **fails loudly**
  when the response carries no word timings, because a whole-text response looks like
  success and is not.

A 404 on `/health` is tolerated: some builds serve `/inference` without a health route,
and treating that as "unreachable" would break on a build that transcribes perfectly well.

### Transcripts are not reproducible

Two runs over the same audio produce different text. Measured on a 25 s clip, three runs
per configuration: identical 3/3 was false for every parameter set tried, including
`threads=1` and after a container restart.

How much this costs is smaller than it sounds. Five runs of the same 120 s excerpt:

- word-level agreement against a baseline: **94.7% – 99.4%**
- numeric/symbol token sequence: **identical**
- differences are singular/plural, dropped function words, and proper nouns

The *timings* are stable, which is what stages 5-7 actually consume. Segment counts move
(50-55 across five runs of the same excerpt) without the words moving much.

Two runs over one 73-minute lecture gave 1677 and 1520 segments. Do not diff transcripts
between runs. Frame extraction, by contrast, is deterministic.

Proper nouns are the category that suffers most, and in a technical lecture those are the
course terms a note is built on. A glossary and the audit stage exist for that reason.

### shipboard

[github.com/nicklin11/shipboard](https://github.com/nicklin11/shipboard) — MIT, by the
same author. On-demand local speech-to-text for Linux desktops: a whisper.cpp server that
sleeps when idle, freeing ~1.5 GiB of VRAM, and wakes on the first request, plus a
compositor-agnostic dictation daemon.

`glimpse` uses it as an optional backend and does not require it. If you already run
shipboard for dictation, `glimpse process` will use the same container and the same GPU:

```
pipx install shipboard
shipboard backend up
```

Two things shipboard provides that are worth knowing when transcribing a 73-minute
lecture rather than dictating a sentence:

- **The container may not be running.** The wake proxy on port 10301 starts it on demand
  and the idle-stop timer stops it after five minutes of silence, so a direct request to
  `127.0.0.1:10302` may be refused. Point `GLIMPSE_WHISPERCPP_URL` at the proxy if that
  is how you run it.
- **`--timestamps json` is not a format flag, it is the only path with timings.** The
  plain `shipboard process` output is plain text; the JSON form is what carries per-word
  `start`/`end`. `glimpse` requests that form and fails loudly if the word timings are
  absent.

## Output

```
$XDG_STATE_HOME/glimpse/<lecture>/     (default: ~/.local/state/glimpse/<lecture>)
├── transcript.json                   structured, with word timings
├── transcript.raw.json               the endpoint's payload, untouched
├── transcript.txt                    plain text
├── manifest.tsv                      frame index: timestamp, path, reason
├── detect.json                       the ffmpeg filter actually used
└── images/                           extracted frames
```

Override with `--output-dir DIR` or `GLIMPSE_OUTPUT_DIR`. `--output-dir` names the bundle
itself — `--output-dir /tmp/x` writes to `/tmp/x`, not `/tmp/x/<lecture>`.

Not a working-directory-relative default: artefacts written next to you land in whatever
repository you happen to be standing in.

Scratch — including the 137.9 MiB `audio.wav` — goes to a temporary directory that is
removed when the run succeeds. `--keep-workdir` keeps it.

## Export to an Obsidian vault

```
glimpse process lecture.webm --vault-path ~/Documents/obs_notes/course/lecture-1
```

The vault is a **copy target**, not the output root. Every file is verified after copying;
one that lands short is an error rather than a silent success. Your bundle is left intact.

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | usage error — or a stage that is not built yet |
| 2 | a dependency is missing |
| 3 | a dependency is installed and failing |
| 4 | a quality gate rejected the output |
| 5 | the audit found things |
| 130 | interrupted |

The codes are the contract. `glimpse` does not report success for work it did not do.

## Design decisions

[`docs/adr/0001-lecture-pipeline.md`](docs/adr/0001-lecture-pipeline.md) records the
decisions and, more usefully, the measurements that overturned earlier ones:

- whisper.cpp on this host does **not** serve the OpenAI-compatible `/v1/*` routes — an
  OpenAI-only backend cannot run there, so both wire formats are implemented;
- `shipboard` normalises the response envelope, returning a bare array where `/inference`
  returns an object;
- transcripts cannot be reproduced run to run.

Each changed the design, and each is written down with the evidence that changed it.

## Licence

MIT. See [LICENSE](LICENSE).