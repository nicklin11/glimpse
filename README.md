# glimpse

Turns a lecture recording into a structured, **independently audited** Markdown note.

One dependency (`numpy`, for the sharpness gate), plus `ffmpeg` and a transcription backend.

```
glimpse process lecture.webm
glimpse doctor
```

## Documentation

| | |
|---|---|
| [`docs/adr/README.md`](docs/adr/README.md) | The five architecture decisions, what `D<n>` means, and the order to read them in |
| [`docs/stages.md`](docs/stages.md) | Which of the twelve stages are built, which are open, which have been verified on a real lecture |

The ADRs record what was **decided**; `docs/stages.md` records what is **built**. Where the
two disagree, the disagreement is the work.

## Status: all 12 stages built, one gap that needs an endpoint

`glimpse process` runs end to end. Exit 0 is conditional on three things, all of which are
checked rather than assumed: every stage ran, every artefact it claimed is on disk (stage 12
stats the filesystem — an exit code is a claim by the stage that would have failed), and a
model actually wrote the note.

| stage | does | state |
|---|---|---|
| 0 | `doctor`: report what is missing and what to do | works |
| 1 | probe the recording | works |
| 2 | extract 16 kHz mono audio | works |
| 3 | transcribe, with word-level timings | works — 0.070x realtime on 4520 s |
| 4 | extract distinct on-screen frames | works |
| 5 | document-bbox crop, upscale, unsharp, sharpness gate | works |
| 6 | align frames to transcript words, then caption them with a vision model | alignment works; the captioning call is **not built** — no vision client exists |
| 7 | synthesise the note | works; falls back to a template without an endpoint |
| 8 | check every formula mechanically | works |
| 9 | audit in a separate model context | tier 1 works; tier 2 needs an endpoint |
| 10 | repair what the audit found, mechanically only | works |
| 11 | link terminology across lectures | works |
| 12 | verify the bundle, then report | works |

Two honest limits, neither of which is a stub dressed up as finished:

- **Stage 6 has no vision client.** Alignment and frame-to-word binding work and are
  exercised; the captioning call itself does not exist. The run reports
  `captioning is NOT_CONFIGURED`.
- **No model endpoint means no synthesis and no tier-2 audit.** Without one, stage 7 emits a
  template skeleton and the process exits **1** rather than reporting a note nobody reviewed.
  A template has eight headings and no synthesis behind it, and calling that success is the
  silent-degradation failure the design is written against.

### Verified on a real lecture

`1_lecture_OCS.mp4`, AV1 1080p, 4520 s of audio, all twelve stages, this host. The run is in
the 2026-10-04 amendment of ADR-0001, including the three defects it found in the code that
produced it.

## Install

```
git clone https://github.com/nicklin11/glimpse
cd glimpse
pipx install .
```

Requires Python 3.11+ and `ffmpeg`/`ffprobe` on `PATH`.

If `glimpse` dies at import with `ModuleNotFoundError`, the venv is stale rather than
broken: `pipx install -e` resolves dependencies once, so a later change to
`pyproject.toml` does not reach the existing environment. Re-install with
`pipx install --force -e <path>`. `glimpse doctor` names the missing module directly.

### As a single file, with no install

```
python -m zipapp src -m glimpse.cli:main -o glimpse -p "/usr/bin/env python3"
./glimpse doctor
```

**This works only if `python3` already has numpy importable.** `zipapp` archives a source
tree; it does not install anything, and `src/` contains no numpy. The command above was
verified on this host, where the system Python happens to have numpy 2.5.3 — the archive's
shebang resolves `import numpy` against system site-packages. On a machine without it, stage 5
dies at import while `--help` still works, because `--help` never reaches the gate.

For an archive that stands alone, vendor the dependency in first (install numpy into a
throwaway prefix, copy it into the tree, then zip) — which is the actual cost of the
dependency, and the reason ADR-0002 records the import set as a constraint on the zipapp
build rather than an incidental detail.

## Configure transcription

Stage 3 delegates to an **endpoint**. Three backends ship:

| backend | selected by | speaks | where the audio goes |
|---|---|---|---|
| `whispercpp` | autodetected, preferred | whisper.cpp native `POST /inference` | **stays on this machine** |
| `openai` | `GLIMPSE_STT=openai` | OpenAI-compatible `/v1/audio/transcriptions` | **leaves this machine** |

`GLIMPSE_STT=auto` (the default) prefers a local whisper.cpp and falls back to the
OpenAI-compatible endpoint. That order is deliberate: with both configured, the local one
keeps the recording on the host. Whatever actually ran is recorded in the transcript, and
`glimpse doctor` states it — `audio stays on this machine` or `audio and transcripts leave
this machine` — so the choice is never something you have to infer from a config file.

There is no subprocess backend. A third-party proxy was removed in #44: it added no
normalisation this code did not already do, and it imposed a 300-second ceiling that killed
requests for lectures longer than about 65 minutes. See the 2026-10-04 amendment of ADR-0001.

| variable | purpose |
|---|---|
| `GLIMPSE_STT` | `auto`, `whispercpp` or `openai` |
| `GLIMPSE_WHISPERCPP_URL` | default `http://127.0.0.1:10302` |
| `GLIMPSE_WHISPERCPP_LANGUAGE` | pin a language; unset means autodetect |
| `GLIMPSE_OPENAI_KEY`, `GLIMPSE_OPENAI_MODEL` | for a hosted endpoint |
| `GLIMPSE_LLM_ENDPOINT` | OpenAI-compatible base URL for synthesis, audit and repair |
| `GLIMPSE_LLM_MODEL` | model id sent in the request |
| `GLIMPSE_LLM_KEY` | optional; unset means no `Authorization` header |
| `GLIMPSE_VLM_ENDPOINT`, `GLIMPSE_VLM_MODEL`, `GLIMPSE_VLM_KEY` | stage 6 frame captioning. Each falls back to its `GLIMPSE_LLM_*` counterpart, so a single model serving both needs two variables, not four |
| `GLIMPSE_VAULT` | the Obsidian vault to export into; see [Export](#export-to-an-obsidian-vault) |
| `GLIMPSE_SETTINGS` | the settings *file*, overriding `$XDG_CONFIG_HOME/glimpse/settings.toml`. Named for the file, not a directory — also how the test suites keep themselves out of `$HOME` |
| `GLIMPSE_TERMS_DIR` | the term vocabulary for stage 11; defaults to `<vault>/mscs/_terms` |

`glimpse doctor` prints which backend was selected and whether it answers.

There is no `GLIMPSE_GATEWAY_URL`. It was documented here and read by a preflight check
that reported the model endpoint under the name `gateway`, so every successful run opened
with a false "gateway is unavailable" line (#68).

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

### The container may not be running

whisper.cpp is normally started by something else — on this host the `whisper-local`
container, stopped after five minutes of silence by an idle timer. A direct request to
`127.0.0.1:10302` may be refused if nothing has woken it:

```
docker start whisper-local
```

`glimpse doctor` tells you whether the endpoint answers before stage 3 spends six seconds
extracting audio to find out.

If the container you use goes through a wake proxy that applies a request timeout, point
`GLIMPSE_WHISPERCPP_URL` at the container directly rather than the proxy. A 300-second
timeout is below what a 75-minute lecture needs, and a proxy that stops the container
mid-request turns a long transcription into an error with no output.

## Output

```
$XDG_STATE_HOME/glimpse/<lecture>/     (default: ~/.local/state/glimpse/<lecture>)
├── note.md                             the deliverable: synthesised, repaired, linked
├── note.synth.md                       stage 7's original, kept as evidence
├── transcript.json                     structured, with word timings
├── transcript.raw.json                 the endpoint's payload, untouched
├── transcript.txt                      plain text
├── manifest.tsv                        frame index: timestamp, path, reason
├── detect.json                         the ffmpeg filter actually used
├── lint.json                           mechanical gate, before the audit
├── audit.json                          findings, each with its quoted line
├── repair.json                         what was changed and what was declined
├── link.json                           wikilinks inserted
├── report.json                         what stage 12 verified, and what it did not find
├── report-provenance.json
└── images/                             extracted frames
```

Override with `--output-dir DIR` or `GLIMPSE_OUTPUT_DIR`. `--output-dir` names the bundle
itself — `--output-dir /tmp/x` writes to `/tmp/x`, not `/tmp/x/<lecture>`.

Not a working-directory-relative default: artefacts written next to you land in whatever
repository you happen to be standing in.

Scratch — including the 137.9 MiB `audio.wav` — goes to a temporary directory that is
removed when the run succeeds. `--keep-workdir` keeps it.

## Export to an Obsidian vault

The note lands in your vault by default. Nothing to pass:

```
glimpse process lecture.webm
```

It goes to `<vault>/glimpse/`, not the vault root — a lecture bundle is ~170 files, 145 of
them frames under `images/`, and the vault root is where your own notes live.

**Where the vault comes from**, most specific first:

| | how |
|---|---|
| 1 | `--vault-path DIR` |
| 2 | `$GLIMPSE_VAULT` |
| 3 | the `vault` key in `$XDG_CONFIG_HOME/glimpse/settings.toml` |
| 4 | `~/Documents/obs_notes` |

On a first run with nothing configured, the vault that was used is written to the settings
file and the path is printed. To change it later, edit that file, or set `$GLIMPSE_VAULT`.

```
--vault-subdir REL     where inside the vault, relative to it (default: glimpse/)
--no-vault-export      finish the bundle without copying it anywhere
```

The vault is a **copy target**, not the output root. Every file is verified after copying;
one that lands short is an error rather than a silent success. Your bundle is left intact. A
vault path that does not exist is reported and named, not created — otherwise a typo
materialises an empty directory that `glimpse doctor` then certifies as healthy.

## What one lecture costs

Measured end to end on `1_lecture_OCS.mp4` (AV1 1080p, 4520 s) on this host:

| Stage | Wall |
|---|---|
| 1 `probe` | 0.1 s |
| 2 `audio` | 3.5 s |
| 3 `stt` | 315.4 s (0.070x realtime) |
| 4 `frames` | 319.2 s |
| 5-12 | **7.7 s combined** |
| **total** | **645.9 s** |

98.8% of the wall clock is two stages that report nothing until they finish. Everything the
pipeline does to the note — quality gating, alignment, synthesis, lint, audit, repair,
linking, verification — is 1.2%.

Model cost on this run: **zero**, because no endpoint was configured. Synthesis fell back to
a template and the audit's tier 2 did not run. With a vision endpoint at the measured gateway
price the visual budget is ~$0.01 per lecture (ADR-0005: 16 distinct screen states, against
~131 880 decoded frames).

Both figures are host-specific and one is a measurement that cleared a threshold rather than
evidence of scale — see the 2026-10-04 amendment in ADR-0001.

## Exit codes

| code | meaning |
|---|---|
| 0 | every stage ran and every artefact it claimed is on disk |
| 1 | usage error, or the run finished and could not verify what it wrote |
| 2 | a dependency is missing |
| 3 | a dependency is installed and failing |
| 4 | a quality gate rejected the output |
| 5 | the audit found error-tier findings in the note |
| 130 | interrupted |

The codes are the contract. `glimpse` does not report success for work it did not do.

Two of these are about verification rather than production, and they are the reason exit 0
means something:

- **0 is conditional on stage 12**, which stats the filesystem. An earlier version of this
  pipeline reported success after ffmpeg had written an empty file and exited 0 — a
  27-minute run that extracted zero frames. An exit code is a claim by the stage that
  would have failed; a `stat` is a measurement.
- **5 outranks 4** when both apply, because it is the more specific statement about the
  deliverable. Both messages print regardless of which code comes out.

## Design decisions

[`docs/adr/0001-lecture-pipeline.md`](docs/adr/0001-lecture-pipeline.md) records the
decisions and, more usefully, the measurements that overturned earlier ones:

- whisper.cpp on this host does **not** serve the OpenAI-compatible `/v1/*` routes — an
  OpenAI-only backend cannot run there, so both wire formats are implemented;
- transcription is **not reproducible run to run**: two runs of the same endpoint on the same
  audio disagree on how the transcript is split into segments (14 vs 15 on a 45 s clip), while
  the text itself matched word for word. Measured in #49; a claim that two code paths returned
  byte-identical payloads was falsified by it, because whisper.cpp disagrees with *itself*;
- transcripts cannot be reproduced run to run.

Each changed the design, and each is written down with the evidence that changed it.

## Licence

MIT. See [LICENSE](LICENSE).