# Running glimpse — configuration, flags, subcommands

This file is the full reference. [`tests/test_claims.py`](../tests/test_claims.py) checks that
every `GLIMPSE_*` variable the code reads and every flag and subcommand `cli.py` registers
appears here, so the table cannot rot silently.

Three sources of configuration, in the order the code consults them:

1. an explicit CLI flag (`--vault-path`, `--terms-dir`, ...)
2. the corresponding `GLIMPSE_*` environment variable
3. the settings file: `$GLIMPSE_SETTINGS`, else `$XDG_CONFIG_HOME/glimpse/settings.toml`
   (one key today, `vault`; written on a first run with nothing configured)

Real per-layer precedence across all fields is ADR-0002 territory and tracked in
[#52](https://github.com/nicklin11/glimpse/issues/52).

## Process: environment variables

| variable | read by | purpose, default |
|---|---|---|
| `GLIMPSE_STT` | `stt/backends.py` | backend selection: `auto` (default), `whispercpp`, `openai`. `auto` probes whisper-cpp first, so with both configured the local one keeps the recording on the host; neither reachable exits 2 |
| `GLIMPSE_WHISPERCPP_URL` | `stt/backends.py` | whisper.cpp `POST /inference` endpoint; default `http://127.0.0.1:10302` |
| `GLIMPSE_WHISPERCPP_LANGUAGE` | `stt/whispercpp.py` | pin the language; unset means autodetect |
| `GLIMPSE_OPENAI_URL` | `stt/backends.py` | hosted STT endpoint (OpenAI-compatible `/v1/audio/transcriptions`); default same port |
| `GLIMPSE_OPENAI_KEY` | `stt/openai_compat.py` | key for the hosted STT endpoint |
| `GLIMPSE_OPENAI_MODEL` | `stt/openai_compat.py` | model id; default `whisper-1` |
| `GLIMPSE_LLM_ENDPOINT` | `llm.py`, `deps.py` | OpenAI-compatible base URL for synthesis (stage 7) and audit (stage 9) |
| `GLIMPSE_LLM_MODEL` | `llm.py`, `deps.py` | model id sent in the request |
| `GLIMPSE_LLM_KEY` | `llm.py` | optional; unset means no `Authorization` header |
| `GLIMPSE_VLM_ENDPOINT` | `llm.py` | stage 6 frame captioning; unset falls back to `GLIMPSE_LLM_ENDPOINT` |
| `GLIMPSE_VLM_MODEL` | `llm.py` | as above, falls back to `GLIMPSE_LLM_MODEL` |
| `GLIMPSE_VLM_KEY` | `llm.py` | as above, falls back to `GLIMPSE_LLM_KEY` |
| `GLIMPSE_VAULT` | `deps.py` | the Obsidian vault the note is exported into |
| `GLIMPSE_SETTINGS` | `deps.py` | the settings *file*, overriding the XDG path |
| `GLIMPSE_TERMS_DIR` | `link.py` | atomic term notes for stage 11; default `<vault>/mscs/_terms` |
| `GLIMPSE_OUTPUT_DIR` | `bundle.py` | where the bundle goes; default `$XDG_STATE_HOME/glimpse/<lecture>`, or `./output/<lecture>` when `XDG_STATE_HOME` is unset |
| `GLIMPSE_WORKDIR` | `workspace.py` | parent of the managed work dir (scratch: `audio.wav` etc.); default `$TMPDIR` |
| `GLIMPSE_BBOX_SOURCE` | `pipeline.py` | stage 5 estimator name; the only registered value is `geometric` (default) |

`GLIMPSE_GATEWAY_URL` is not read anywhere: the check that read it was removed (see the
comment in `deps.py`). Standard Python/XDG variables — `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
`TMPDIR` — are also consulted, each with a documented fallback.

## `glimpse doctor`

Reports missing or conflicting dependencies and exits 2/3 with the reason and remediation.
Run it before a run, not after a failure.

## `glimpse process`

```
glimpse process [path] [options]
```

| flag | meaning |
|---|---|
| `path` | video or audio file |
| `--workdir DIR` | parent directory for the managed work dir (default: `$TMPDIR`, or `GLIMPSE_WORKDIR`) |
| `--output-dir DIR` | where to write the output bundle (default: `$XDG_STATE_HOME/glimpse/<lecture>`, or `./output/<lecture>` if unset) |
| `--overwrite` | replace an existing output bundle instead of writing into it |
| `--vault-path DIR` | copy the finished bundle into this vault; never a pipeline blocker. Defaults to the configured vault, and the run writes into it either way — use `--no-vault-export` to keep the note in the bundle only |
| `--no-vault-export` | finish the bundle without copying it into the vault |
| `--vault-images` | also copy `images/` into the vault. Off by default: the frames are most of the bytes (50 of 59 MiB on lecture 1) and the note refers to none of them, so they stay in the bundle as the pipeline's evidence |
| `--vault-subdir REL` | where inside the vault the bundle lands, relative to it (default: `<vault>/glimpse/`). Not optional in practice: a bundle is ~188 files, and the vault root holds the user's own notes |
| `--keep-workdir` | keep the work dir even when the run succeeds |
| `--glossary FILE` | ASR term glossary for stages 9 and 10, in the `\| variant \| canonical \| domain \| date \|` format. Without it the term-drift rule finds nothing and the audit says so |
| `--terms-dir DIR` | atomic term notes for stage 11. Default: `$GLIMPSE_TERMS_DIR`, then `<vault-path>/mscs/_terms`. Without it the note is written unlinked and the stage says so |

## `glimpse audit`

```
glimpse audit [note]
```

Run the audit stage against an existing note. Registered, not implemented in this build.

## `glimpse term`

```
glimpse term link    [paths...] [--write] [--terms-dir DIR] [--vault-path DIR]
glimpse term index   [--write] [--only-used] [--terms-dir DIR] [--vault-path DIR]
glimpse term check   [--terms-dir DIR] [--vault-path DIR]
```

Ported from `mscs-termlink`. `link` injects wikilinks into Markdown notes (dry run unless
`--write`); `index` reports which lectures mention which term and writes `_index.md` on
`--write` (`--only-used` omits terms used nowhere); `check` runs diagnostics on the terms
directory and the lecture notes. The terms directory resolves from `--terms-dir`, then
`$GLIMPSE_TERMS_DIR`, then `<--vault-path>/mscs/_terms`.
