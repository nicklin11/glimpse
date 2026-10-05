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

Stage 3 transcribes through an endpoint. Two backends:

| backend | selected by | speaks | where the audio goes |
|---|---|---|---|
| `whispercpp` | autodetected, preferred | whisper.cpp native `POST /inference` | **stays on this machine** |
| `openai` | `GLIMPSE_STT=openai` | OpenAI-compatible `/v1/audio/transcriptions` | **leaves this machine** |

The default, `GLIMPSE_STT=auto`, is a selection mode rather than a third backend: it probes
whisper.cpp first and takes the first that answers, so with both configured the local one keeps the
recording on the host. With neither reachable it exits 2. `glimpse doctor` states which was
selected and whether it answers.

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

### Transcripts are not reproducible across configurations

Consecutive runs on the same server, with the same environment and the same language pin, give
byte-identical transcripts — verified by the sha256 in `stt-provenance.json`, matching across
runs. Change something and they are not: two runs of a 120 s excerpt under different
`threads` settings agreed on **94.7%–99.4%** of words, with numeric and symbol tokens identical
and differences falling on singular/plural, dropped function words and proper nouns.

Frame extraction is deterministic. Timings are stable. The text is a function of the
configuration, so `stt-provenance.json` records the parameters next to the digest — check
`parameters` before blaming a model for a transcript difference.

Proper nouns in a technical lecture are the course terms a note is built on, which is what the
glossary and the audit stage are for.

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

## What the note is made of

The note is built from the transcript. Per-frame captions are written to `captions.json` in the
bundle; `stage 7` receives the frame filenames for each section, not their descriptions, so no
caption text and no image appears in the note. `images/` stays in the bundle.

## Cost

Measured on `1_lecture_OCS.mp4` (AV1 1080p, 4520 s). Two runs of the same file, the second
after the first:

| | cold | warm |
|---|---|---|
| transcription | 298 s (0.066x realtime) | 298 s |
| frame extraction | 319 s (0.071x realtime) | 319 s |
| frame captioning | 447 s, 58 vision calls, 396 k input tokens | ~0 s, 0 calls |
| everything else | ~252 s | ~364 s |
| **total** | **1318 s** | **981 s** |

Transcription and extraction are deterministic. Captioning is cached on `(frame bytes, model
id)`, so a second run over unchanged frames and the same model makes zero vision requests --
`captions.json` records `source: cached` per frame. A different model id is a different cache.

Cold: ~6.8 k input tokens per frame, almost all of it the image.

## Exit codes

| code | meaning |
|---|---|
| 0 | every stage ran and every artefact it claimed is on disk |
| 1 | usage error, or the run finished and could not verify what it wrote |
| 2 | a dependency is missing |
| 3 | a dependency is installed and failing |
| 4 | a quality gate rejected the output |
| 5 | the audit found error-tier findings in the note |
| 130 | interrupted (Ctrl-C); the work dir is kept |

## What exit 0 does not tell you

A reachable model is not implied by exit 0. Three cases degrade instead of failing, and each
leaves evidence in the bundle:

| where | condition | what you see |
|---|---|---|
| stage 6 | vision endpoint down | **exit 3.** `caption_outcome.ok` is `captioned + reused > 0`, checked before the quality gate so a dead endpoint is not reported as soft crops |
| stage 9 | tier-2 critic down | **exit 0** with a `critic/unavailable` WARN; the audit degrades to tier 1 |
| stage 6 | some frames failed | **exit 0**; per-frame `caption_status: ERROR` with the endpoint error in `caption_trace.detail` |

The third is the one to check for. `CaptionOutcome.ok` is a zero test, not a threshold, so 56 of
57 captioned is a success. Read the counts on the `[6/12] caption` line, or count `caption_trace`
entries whose `source` is `error`.

Verified on a refused endpoint:

```
[6/12] caption  1 frames aligned, 2 words, 0 without transcript coverage
         no frame was captioned -- see caption-provenance.json
         status='ERROR' source='error' detail='... [Errno 111] Connection refused'
```

## Design decisions

[`docs/adr/`](docs/adr/README.md) records the decisions and the measurements that overturned
earlier ones.

## Licence

MIT. See [LICENSE](LICENSE).