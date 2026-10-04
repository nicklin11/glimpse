# glimpse

Turns a lecture recording into a structured, **independently audited** Markdown note.

```
glimpse process lecture.webm
glimpse doctor
```

## Install

```
git clone https://github.com/nicklin11/glimpse
cd glimpse
pipx install .
```

Python 3.11+, `ffmpeg`/`ffprobe` on `PATH`, and one Python dependency (`numpy`, for the
sharpness gate).

If `glimpse` dies at import with `ModuleNotFoundError`, the environment is stale rather than
broken: `pipx install -e` resolves dependencies once, so a later change to `pyproject.toml`
does not reach it. Re-install with `pipx install --force -e <path>`.

As a single file with no install, provided `python3` already has numpy:

```
python -m zipapp src -m glimpse.cli:main -o glimpse -p "/usr/bin/env python3"
./glimpse doctor
```

## Configure

Stage 3 transcribes through an endpoint. Three backends:

| backend | selected by | speaks | where the audio goes |
|---|---|---|---|
| `whispercpp` | autodetected, preferred | whisper.cpp native `POST /inference` | **stays on this machine** |
| `openai` | `GLIMPSE_STT=openai` | OpenAI-compatible `/v1/audio/transcriptions` | **leaves this machine** |

`GLIMPSE_STT=auto` prefers a local whisper.cpp and falls back to the OpenAI-compatible
endpoint, so with both configured the local one keeps the recording on the host.
`glimpse doctor` states which was selected and whether it answers.

| variable | purpose |
|---|---|
| `GLIMPSE_STT` | `auto`, `whispercpp` or `openai` |
| `GLIMPSE_WHISPERCPP_URL` | default `http://127.0.0.1:10302` |
| `GLIMPSE_WHISPERCPP_LANGUAGE` | pin a language; unset means autodetect |
| `GLIMPSE_OPENAI_KEY`, `GLIMPSE_OPENAI_MODEL` | for a hosted endpoint |
| `GLIMPSE_LLM_ENDPOINT` | OpenAI-compatible base URL for synthesis and audit |
| `GLIMPSE_LLM_MODEL` | model id sent in the request |
| `GLIMPSE_LLM_KEY` | optional; unset means no `Authorization` header |
| `GLIMPSE_VLM_ENDPOINT`, `GLIMPSE_VLM_MODEL`, `GLIMPSE_VLM_KEY` | stage 6 frame captioning; each falls back to its `GLIMPSE_LLM_*` counterpart |
| `GLIMPSE_VAULT` | the Obsidian vault to export into |
| `GLIMPSE_SETTINGS` | the settings *file*, overriding `$XDG_CONFIG_HOME/glimpse/settings.toml` |
| `GLIMPSE_TERMS_DIR` | the term vocabulary for stage 11; defaults to `<vault>/mscs/_terms` |
| `GLIMPSE_OUTPUT_DIR` | where the bundle goes |

### whisper.cpp

Any build serving `POST /inference` works; point `GLIMPSE_WHISPERCPP_URL` at it. Two things
worth knowing before debugging:

- **It does not speak the OpenAI protocol.** `/v1/models` and `/v1/audio/transcriptions` are
  404, which is why both wire formats are implemented here.
- **It must be told `response_format=verbose_json`.** The default returns only
  `{"text": ...}` — no word timings, without which a frame cannot be bound to the paragraph it
  belongs to. The run **fails loudly** when the response carries no timings, because a
  whole-text response looks like success and is not.

A 404 on `/health` is tolerated; some builds serve `/inference` without a health route.

### Transcripts are not reproducible

Two runs over the same audio produce different text, including `threads=1` and after a
container restart. Do not diff transcripts between runs.

What is stable is the timings, which is what the pipeline consumes, and frame extraction,
which is deterministic. Measured word-level agreement across runs of a 120 s excerpt:
**94.7%–99.4%**, with numeric and symbol tokens identical. Differences are singular/plural,
dropped function words, and proper nouns — and proper nouns in a technical lecture are the
course terms a note is built on. That is what the glossary and the audit stage are for.

## Output

```
$XDG_STATE_HOME/glimpse/<lecture>/     (default: ~/.local/state/glimpse/<lecture>)
├── note.md                             the deliverable: synthesised, repaired, linked
├── note.synth.md                       stage 7's original, kept as evidence
├── transcript.json                     structured, with word timings
├── transcript.raw.json                 the endpoint's payload, untouched
├── transcript.txt                      plain text
├── manifest.tsv                        frame index: timestamp, path, reason
├── detect.json                         the ffmpeg filter chain and settings actually used
├── lint.json                           mechanical gate, before the audit
├── audit.json                          findings, each with its quoted line
├── repair.json                         what was changed and what was declined
├── link.json                           wikilinks inserted
├── report.json                         what stage 12 verified, and what it did not find
└── images/                             extracted frames
```

Every stage also writes a `*-provenance.json` recording what produced its output — the backend
and its parameters for transcription, the model and tokens for the LLM stages, the input and
the ffmpeg that measured it for the first two. The answer to "why is this note different from
the last run" is in the bundle, not in a log.

`--output-dir DIR` names the bundle itself. Scratch, including the 137.9 MiB `audio.wav`, goes
to a temporary directory removed when the run succeeds; `--keep-workdir` keeps it.

## Export to an Obsidian vault

The note lands in your vault by default, into `<vault>/glimpse/`, and without `images/` — a
lecture bundle is 59 MiB, 50 of it frames, and the note refers to no frame file. The frames
stay in the bundle, where the audit and a later re-run look for them.

| | how the vault is resolved |
|---|---|
| 1 | `--vault-path DIR` |
| 2 | `$GLIMPSE_VAULT` |
| 3 | the `vault` key in `$XDG_CONFIG_HOME/glimpse/settings.toml` |
| 4 | `~/Documents/obs_notes` |

On a first run with nothing configured, the vault used is written to the settings file and
printed. `--vault-subdir REL`, `--vault-images` and `--no-vault-export` adjust the copy.

The vault is a **copy target**, not the output root. Every file is verified after copying; one
that lands short is an error rather than a silent success. Your bundle is left intact. A vault
path that does not exist is reported and named, not created.

## Cost

Measured on `1_lecture_OCS.mp4` (AV1 1080p, 4520 s):

| | wall |
|---|---|
| transcription | 306 s (0.068x realtime) |
| frame extraction | 313 s (0.069x realtime) |
| frame captioning | 447 s for 58 frames, 396 k input tokens |
| everything else | 252 s |
| **total** | **1318 s** |

Frame captioning is the one stage whose cost scales with model pricing: 58 vision calls,
~6.8 k input tokens each. Everything else is fixed by the length of the recording.

## Exit codes

| code | meaning |
|---|---|
| 0 | every stage ran and every artefact it claimed is on disk |
| 1 | usage error, or the run finished and could not verify what it wrote |
| 2 | a dependency is missing |
| 3 | a dependency is installed and failing -- currently only stage 3 |
| 4 | a quality gate rejected the output |
| 5 | the audit found error-tier findings in the note |

Codes 2 and 3 are raised by the STT backend, which is the only dependency whose absence stops a
run. A model endpoint that is unreachable or hangs does **not** produce exit 3: at stage 6 every
frame fails to caption, at stage 7 synthesis fails, and at stage 9 tier 2 records a
`critic/unavailable` WARN and degrades to tier 1. Measured on a refused endpoint:

```
[9/12] audit  4 rules, 0 errors, 1 warnings, tier 2 ran, 1 findings, 1/2 sections reviewed
        WARN critic/unavailable: http://127.0.0.1:1/v1/chat/completions unreachable: [Errno 111]
        report ok: True
```

That is a deliberate degradation rather than an oversight -- the audit still produced a result --
but it means **exit 0 does not imply a model was reachable**. A run that reports 0 and shows no
`critic/unavailable` warning did use a model; a run that reports 0 with that warning audited
tier 1 only.
| 130 | interrupted |

The codes are the contract. `glimpse` does not report success for work it did not do. Exit 0
is conditional on stage 12, which stats the filesystem rather than trusting the stages that
would have failed, and on a model having actually written the note — without an endpoint,
stage 7 emits a template skeleton and the process exits **1** instead of reporting a note
nobody reviewed.

## Design decisions

[`docs/adr/`](docs/adr/README.md) records the decisions and the measurements that overturned
earlier ones.

## Licence

MIT. See [LICENSE](LICENSE).