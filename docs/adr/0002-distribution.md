# ADR 0002: Distribution boundary — what ships, what stays external

- **Status:** Accepted
- **Date:** 2026-10-03
- **Proposal:** [#13](https://github.com/nicklin11/dyak/issues/13)
- **Scope:** MVP packaging. Publishes nothing to any index.

> **`D<n>`** = decision *n* within *this* ADR. The number is local to this file and
> collides with numbers in ADR-0001 and ADR-0004; references from anywhere else must be
> written `ADR-000N Dn`. Reading order and index: [`docs/adr/README.md`](README.md).

## Context

The requirement is a self-contained program, not a package on a public index.

Measured on lecture 1 (`1_lecture_OCS.webm`, 4396 s container, 4520 s of audio): ffmpeg
6.4 s, whisper.cpp 257.2 s, and the Python orchestration layer from process start to stage 1
— under 100 ms. **The runtime cost is entirely native subprocesses.**

One measured fact decides most of the design:

- `dyak` imports the standard library plus **exactly one third-party module**: `numpy`,
  in `quality.py`, for stage 5's MEGE measurement at the frame's native resolution. The
  complete import set in `src/` is `argparse`, `json`, `os`, `shutil`, `stat`, `subprocess`,
  `sys`, `tempfile`, `time`, `urllib.error`, `urllib.request`, `dataclasses`, `pathlib`,
  `numpy`.

`dependencies = ["numpy>=1.26"]` in `pyproject.toml`.

**This ADR was first written claiming `dependencies = []` and a pure-stdlib import set. Both
were true when the proposal was drafted and false when the ADR was landed.** numpy was added
for the quality gate: measuring MEGE per frame in pure Python is a ~20 s pass over 1.4 M
pixels, which would make the gate slower than speech recognition. The claim was copied from
the proposal without re-reading `pyproject.toml`, which is the kind of unchecked carry-over
that turns a decision record into fiction.

## Decision

### D1 — No interpreter bundler

Not PyInstaller, not Nuitka. Both wrap the interpreter and every imported module into an
archive, and `--onefile` additionally **extracts that archive to a temp directory on every
start**. That trades a measured sub-100 ms startup for a permanent 1–2 s penalty, a
40–80 MB artefact, and a build step whose output cannot be reviewed with `diff`.

The pipeline's cost is 257 s of whisper.cpp. Paying 2 s back at every start, to ship 80 MB
duplicating an already-installed interpreter, is the wrong trade.

### D2 — Deliver as zipapps

`python -m zipapp` produces a single executable file from a source tree, with no archive
extraction on start.

The original premise was "no third-party imports", and with numpy present it is false. The
decision survives, because what zipapp actually buys is **no extraction step on every start**
— that property does not depend on the dependency count. numpy is vendored into the archive
alongside the source, and its import cost is paid once against a warm page cache.

The constraint this imposes is real and worth stating: the dependency set is what someone has
to keep the zipapp buildable from, and it is one module wide by measurement rather than by
taste. A second runtime dependency needs the cost argument in D1 re-run against it.

### D3 — `shipboard` is an external runtime dependency, checked by `doctor`, never bundled

**Status: SUPERSEDED.** The premise is dead. This decision rests on ADR-0001 D2, which was
superseded on 2026-10-03; shipboard is no longer a mandatory dependency, and
`grep -rn shipboard src/dyak/` returns **nothing** — no module requires, probes or
mentions it. The distribution logic below (never vendor, never a `dependencies = []` entry,
exit 2 with a remediation line) survives as a rule but has no current subject.

Retained because the reasoning is sound and applies to whatever the backend turns out to be:
a backend must never be vendored into the zipapp or installed by the install step.

ADR-0001 D2 decides the delegation: STT belongs to shipboard and dyak does not reimplement
it. This ADR adds only the distribution consequence:

- shipboard is **not** vendored into the zipapp, **not** a `dependencies = [...]` entry, and
  **not** installed by the install step;
- its absence is **exit 2 (`MISSING_DEPENDENCY`)** with a remediation line naming the exact
  install command — implemented in stage 0;
- `process` refuses to start rather than degrading, because a tool that swallows its
  dependency's errors looks identical to a tool that worked.

**Why not bundle it.** PEP 517 has no declarative hook for "install another application".
Bundling shipboard, or installing it from `setup.py`, both mean arbitrary code in the
installer's context that uninstall does not account for.

### D4 — The STT backend is local, and `doctor` probes the configured URL

**Status:** in force. Corrected 2026-10-04: the proxy path given below is wrong. The script
lives at `~/Coding/shipboard/src/shipboard/assets/scripts/whisper_wake_proxy.py`, not
`~/Coding/shipboard/scripts/whisper_wake_proxy.py`. The topology and the
`~/.config/shipboard/shipboard.toml` pointer (`http://100.64.0.1:10301/inference`) were
re-verified and still hold.

Measured on this host, 2026-10-03:

```
charoite  (this machine; tailscale IP 100.64.0.1)
├── docker container whisper-local    ->  127.0.0.1:10302
│     ghcr.io/ggml-org/whisper.cpp:main-vulkan, image pinned by digest
└── whisper-tailnet-proxy.service     ->  100.64.0.1:10301
      python3 ~/Coding/shipboard/scripts/whisper_wake_proxy.py
      ^ this is what whisper_url in ~/.config/shipboard/shipboard.toml points at
```

An earlier draft claimed the backend was "not on this host at all" because the configured URL
is a tailnet address. **That was wrong.** Everything is on this machine; the tailnet address
exists so other tailnet machines can reach the proxy, whose own unit description reads
*"Tailnet-only on-demand proxy for the **local** Whisper container"*.

Consequences, the opposite of what the wrong claim implied:

- the probe is a **loopback request to the configured URL**, not a network dependency, and
  costs one GET against `/health`;
- nothing about running a lecture leaves the machine, so a VPN outage is **not** a reason
  for the run to fail;
- the proxy is **optional** — `whisper_url` may equally be `127.0.0.1:10302`, bypassing the
  tailnet hop entirely.

`doctor` reports it as a **separate** check from `shipboard` itself. Not fatal by default,
because a wrong URL is a configuration the user may not care about until stage 3 — but
reported loudly enough that it is not discovered 30 minutes into a run. `shutil.which` alone
cannot catch any of this.

### D5 — The install burden belongs to shipboard, not to dyak

Setting up a second machine takes nine steps, and **four of them exist only because shipboard
is not packaged as a package**:

```
pacman -S ffmpeg
pipx install dyak
git clone … ~/Coding/shipboard          # not an install
pipx install -e ~/Coding/shipboard      # editable, path-dependent
docker volume create whisper-local-data # compose names it as external
docker compose up -d                    # from inside the checkout
scripts/install.sh                      # hardcodes ~/Coding/shipboard
shipboard setup                         # writes whisper_url
dyak doctor
```

Two things are already solved and must not be mistaken for remaining work: the ggml and VAD
models download themselves inside the container, and the image is pinned by digest. There is
no model step and no tag drift.

The four path-dependent steps are a packaging defect in shipboard, tracked as
[shipboard#9](https://github.com/nicklin11/shipboard/issues/9). **dyak does not absorb
it.** Copying a compose file or a model path into dyak would create a second owner of the
backend, and the two would drift.

**Rejected alternative: call the inference endpoint directly instead of going through
shipboard.** Measured against the table above, it removes **zero** install steps — the
container must be deployed either way — while adding a second configuration owner and
requiring reimplementation of the idle-sleep and wake behaviour shipboard already ships. The
hop is not the cost.

## The editable-install trap

Encountered on this host after numpy was added: `pipx install -e` creates the venv and
resolves `pyproject.toml`'s dependencies **once**. Later commits that edit `pyproject.toml`
do not re-resolve anything. The console script points at the updated source, so the code is
current, and the run dies at import time:

```
File "/home/existingloner/.local/bin/dyak", line 3, in <module>
    from dyak.cli import main
ModuleNotFoundError: No module named 'numpy'
```

The failure looks like a broken install and is actually a stale one — the two are
distinguished by whether `pyproject.toml` changed since the venv was made. After any
dependency change: `pipx install --force -e <path>`.

`doctor` names it in one command, which is the cheapest argument for stage 0 existing at
all: it reported `numpy importable` and would have reported the missing module by name
instead of leaving the traceback as the first diagnostic.

## Alternatives rejected

- **Publish to PyPI.** Out of scope by request. Recorded so it is not rediscovered as a
  blocker: `pipx install` from git needs no index, and ADR-0001 records that the name
  `dyak` collides there.
- **PyInstaller onefile.** See D1.
- **Shipboard as a declared `pip` dependency.** Would put a second application inside
  dyak's dependency closure and make `pipx uninstall dyak` leave it behind. Rejected by
  D3.
- **Install-time side effects.** See D3.
- **whisper.cpp as dyak's only STT dependency.** See D5, last paragraph.

## Consequences

**Good.** One artefact to copy, no dependency closure to reason about. The code stays
diffable. Cold start stays flat. Updating shipboard is `pipx upgrade`, not a dyak release.

**Bad.** A fresh machine is not ready in one command, and the reason is shipboard's
packaging, not this project's. Stated rather than hidden: `doctor` is stage 0 of every run,
and a missing dependency is exit 2 with a command to paste, not a crash.

## Retractions from the proposal

Two claims made in earlier drafts of the proposal are **retracted**, both checked against the
host rather than assumed:

- *"the checkout has uncommitted local changes"* — false. It is clean and fully pushed.
- *"the timestamps work lives on an unmerged branch"* — was true, and was the real issue:
  [shipboard#8](https://github.com/nicklin11/shipboard/issues/8). Fixed 2026-10-03 by
  squash-merging PR #7 (`cd378ba`); the checkout is now on `main` and the installed editable
  shipboard resolves against merged code.

## Open question, closed: whisper.cpp directly?

The proposal left this open. It stays **closed against** for now, on the evidence in D5: a
whisper.cpp-direct path removes zero install steps and costs a second configuration owner
plus a reimplementation of wake/idle. If it is ever added it must be a **configuration swap
in the same place ADR-0004 D3 defines for vision and text endpoints** — not a second code
path, and not a second owner of the backend.

## Acceptance

- `dyak` runs from a zipapp with no virtualenv and no `pip install`.
- `pyproject.toml` keeps `dependencies` at numpy and nothing else. **Was `[]`; falsified by
  the quality gate, see Context.**
- Removing shipboard from `PATH` produces exit 2, not a traceback, and `doctor` prints the
  install command.
- The pipeline reaches `shipboard` only through `shipboard process PATH` (ADR-0001 D2).
- No install step writes into `$HOME`.
- `doctor` distinguishes "shipboard missing" from "configured STT URL unreachable" (D4).
- No compose file, model path or container name appears anywhere in this repository (D5).

Implements the D2 requirement from ADR-0001. Part of #3.