#!/usr/bin/env python3
"""Regression: `glimpse doctor` honours the ADR-0001 exit-code contract.

Stage 0 exists to pin the error contract before four other stages grow their own
ad-hoc reporting. So the thing under test is mostly the distinction between:

  * not on PATH            -> 2, named, with a remediation line
  * on PATH but failing    -> 3, its stderr reproduced VERBATIM
  * not configured at all  -> not a failure (the gateway is optional config)

PATH and subprocess are stubbed; nothing here touches the real network or the
real binaries.
"""

import io
import os
import subprocess
import sys
import tempfile
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from glimpse import cli as glc  # noqa: E402
from glimpse import deps as gld  # noqa: E402
from glimpse import exitcodes as ec  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


# --- stubs -------------------------------------------------------------------
PRESENT = {"ffmpeg", "ffprobe", "shipboard"}
real_which = gld.shutil.which
real_run = gld.subprocess.run

which_set: set[str] = set(PRESENT)
run_rc = 0
# Keyed by executable: a single shared blob would make ffmpeg report
# shipboard's output as its own version.
run_stdout: dict[str, str] = {
    "ffmpeg": "ffmpeg version 7.1.1-1 Copyright (c) 2000-2024 the FFmpeg developers\n",
    "ffprobe": "ffprobe version 7.1.1-1 Copyright (c) 2007-2024\n",
    "shipboard": "usage: shipboard [-h] [--send] [--seconds SECONDS] [--file FILE]\n",
}
run_stderr = ""


def fake_which(name):
    if name in which_set:
        return f"/usr/bin/{name}"
    return None


def fake_run(argv, **kwargs):
    out = run_stdout.get(Path(argv[0]).name, "")
    return subprocess.CompletedProcess(argv, run_rc, out, run_stderr)


import glimpse.stt as gld_stt  # noqa: E402
import glimpse.stt.whispercpp as glwsc  # noqa: E402

gld.shutil.which = fake_which
gld.subprocess.run = fake_run

# The gateway must never be probed against the real network in this test, and neither
# must the STT endpoint. Before this pin, `doctor` reached whatever was listening on
# 127.0.0.1:10302, so the suite's verdict depended on container state.
os.environ.pop(gld.GATEWAY_ENV, None)
real_stt_backend = os.environ.pop(gld_stt.ENV_BACKEND, None)
os.environ[gld_stt.ENV_BACKEND] = "whispercpp"
real_stt_urlopen = glwsc.urllib.request.urlopen

stt_reachable = True


def fake_stt_urlopen(request, timeout=None):
    if not stt_reachable:
        raise urllib.error.URLError(OSError(111, "Connection refused"))
    return _HealthResponse()


class _HealthResponse:
    def read(self, _n=-1):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


glwsc.urllib.request.urlopen = fake_stt_urlopen
tmp = Path(tempfile.mkdtemp())
os.environ[gld.VAULT_ENV] = str(tmp)


def restore():
    gld.shutil.which = real_which
    gld.subprocess.run = real_run
    glwsc.urllib.request.urlopen = real_stt_urlopen
    if real_stt_backend is None:
        os.environ.pop(gld_stt.ENV_BACKEND, None)
    else:
        os.environ[gld_stt.ENV_BACKEND] = real_stt_backend


# --- 1. everything present -> 0, with path and version listed -----------------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
text = out.getvalue()
check("all present -> exit 0", rc == ec.OK, f"rc={rc}")
check("nothing on stderr when healthy", err.getvalue() == "", err.getvalue())
check(
    "every dependency is listed",
    all(n in text for n in ("ffmpeg", "ffprobe", "stt", "shipboard", "gateway", "vault")),
    text,
)
check("resolved path is reported", "/usr/bin/ffmpeg" in text, text)
check("version is reported", "ffmpeg version 7.1.1-1" in text, text)
check("unconfigured gateway is skipped, not failed", "[skip]" in text, text)

# --- 2. the STT endpoint is unreachable -> 3, named, with remediation ---------
# This replaced the old "shipboard is not on PATH -> exit 2" case. A PATH check could not
# detect an endpoint that is configured and down, which is the failure that actually costs
# a 6 s audio extraction before stage 3 reports it.
stt_reachable = False
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
msg = err.getvalue()
check("unreachable STT endpoint -> exit 3", rc == ec.DEPENDENCY_FAILED, f"rc={rc}")
check("the failing check is named", "stt" in msg, msg)
# The detail line (with the target URL) is rendered on stdout by _render; stderr
# carries the name and the remediation.
check("the unreachable target is named", "127.0.0.1" in out.getvalue(), out.getvalue())
check("the remediation names the env var", "GLIMPSE_WHISPERCPP_URL" in msg, msg)
check("shipboard is no longer the reason", "pipx install shipboard" not in msg, msg)
check(
    "present deps still reported as ok",
    "[ok  ] ffmpeg" in out.getvalue() and "[ok  ] ffprobe" in out.getvalue(),
    out.getvalue(),
)

# A binary genuinely missing still earns exit 2, distinct from a broken endpoint.
which_set = {"ffmpeg"}  # ffprobe gone, shipboard is not consulted any more
stt_reachable = True
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
msg = err.getvalue()
check("missing binary -> exit 2", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
check("missing binary is named", "ffprobe" in msg, msg)
check("its remediation names the package", "pacman -S ffmpeg" in msg, msg)
which_set = set(PRESENT)

# --- 3. present but failing -> 3, stderr VERBATIM ----------------------------
which_set = set(PRESENT)
run_rc = 1
# distinctive, multi-line, with leading whitespace and punctuation that a
# paraphrase would mangle.
run_stderr = (
    "ffmpeg: error while loading shared libraries: libavutil.so.60: "
    "cannot open shared object file\n"
    "  extra indented line: '{\"a\": 1}'\n"
)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
msg = err.getvalue()
check("present-but-failing -> exit 3", rc == ec.DEPENDENCY_FAILED, f"rc={rc}")
check("failure names the dependency", "ffmpeg" in msg, msg)
check(
    "stderr reproduced verbatim (full first line)",
    "ffmpeg: error while loading shared libraries: libavutil.so.60: "
    "cannot open shared object file" in msg,
    msg,
)
check(
    "stderr reproduced verbatim (indented line, quotes intact)",
    "  extra indented line: '{\"a\": 1}'" in msg,
    msg,
)
check("a present-but-broken dep is NOT reported as missing", "remediation: pacman" not in msg, msg)
run_rc = 0
run_stderr = ""

# --- 4. missing beats failing when both are true ------------------------------
which_set = {"ffmpeg"}  # shipboard missing, ffprobe missing
run_rc = 1
run_stderr = "boom\n"
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
check("worst-code wins over present-but-failing", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
run_rc = 0
run_stderr = ""

# --- 5. vault: missing path, not a dir, unwritable ---------------------------
which_set = set(PRESENT)
os.environ[gld.VAULT_ENV] = str(tmp / "nope")
check(
    "absent vault -> 2", gld.check_vault().code == ec.MISSING_DEPENDENCY, gld.check_vault().detail
)

a_file = tmp / "a-file"
a_file.write_text("x")
os.environ[gld.VAULT_ENV] = str(a_file)
check(
    "vault that is a file -> 2",
    gld.check_vault().code == ec.MISSING_DEPENDENCY,
    gld.check_vault().detail,
)

# unwritable: chmod 500 leaves the dir non-writable for a non-root user
locked = tmp / "locked"
locked.mkdir()
locked.chmod(0o500)
os.environ[gld.VAULT_ENV] = str(locked)
c = gld.check_vault()
if os.geteuid() == 0:
    check("unwritable vault -> 3 (skipped: running as root)", True, "root bypasses perms")
else:
    check("unwritable vault -> 3", c.code == ec.DEPENDENCY_FAILED, f"{c.code} {c.detail}")
locked.chmod(0o700)
os.environ[gld.VAULT_ENV] = str(tmp)

# the write probe must not leave litter behind
check("vault probe cleans up after itself", not (tmp / ".glimpse-write-probe").exists())


# --- 6. gateway: configured but unreachable -> 3, no real network in tests ----
def boom_urlopen(url, timeout=None):
    raise gld.urllib.error.URLError("connection refused")


real_urlopen = gld.urllib.request.urlopen
gld.urllib.request.urlopen = boom_urlopen
os.environ[gld.GATEWAY_ENV] = "http://127.0.0.1:1/inference"
c = gld.check_gateway()
check("unreachable gateway -> 3", c.code == ec.DEPENDENCY_FAILED, f"{c.code}")
check("unreachable gateway explains itself", "unreachable" in c.detail, c.detail)
check("unreachable gateway has a remediation", bool(c.remediation), str(c.remediation))
gld.urllib.request.urlopen = real_urlopen
os.environ.pop(gld.GATEWAY_ENV, None)

# --- 7. a tool with no --version must not report usage text AS a version -----
# shipboard has no --version flag; `shipboard --help` prints argparse usage.
# Labelling that "version" tells the reader nothing and looks like a bug.
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
text = out.getvalue()
check("shipboard usage output is NOT labelled a version", "usage: shipboard" not in text, text)
# shipboard is no longer mandatory, so it is not probed when another backend is configured.
# Probing it anyway would fail `doctor` for an optional component on someone else's machine.
check(
    "an unconfigured shipboard is skipped, not probed",
    "not the configured STT backend" in text and "/usr/bin/shipboard" not in text,
    text,
)
check(
    "the configured endpoint is what gets probed",
    "127.0.0.1:10302" in text,
    text,
)

# ffmpeg DOES have a real version line, so it must still be reported
check("a real version command is still reported", "ffmpeg version 7.1.1-1" in text, text)
check("ffprobe version reported separately", "ffprobe version 7.1.1-1" in text, text)

# --- 8. unimplemented subcommands point at their issue, not a dead end -------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["audit"])
check("`audit` -> exit 1", rc == ec.USAGE, f"rc={rc}")
check("`audit` names its tracking issue", "#11" in err.getvalue(), err.getvalue())
check("`audit` suggests doctor", "doctor" in err.getvalue(), err.getvalue())

# `process` IS implemented, but only for stages 0-3. With no path it is still a
# usage error, and it must print the form rather than a dead end.
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process"])
check("`process` with no path -> exit 1", rc == ec.USAGE, f"rc={rc}")
check(
    "`process` with no path prints the form",
    "glimpse process PATH" in err.getvalue(),
    err.getvalue(),
)

# unknown subcommand is a usage error, and must not surface as argparse's own 2,
# which is MISSING_DEPENDENCY in the ADR table
try:
    with redirect_stderr(io.StringIO()):
        rc = glc.main(["nonsense"])
    check("unknown subcommand -> exit 1, not 2", rc == ec.USAGE, f"rc={rc}")
except SystemExit as exc:
    check("unknown subcommand -> exit 1, not 2", False, f"argparse exited {exc.code} uncaught")

restore()

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all doctor checks passed")
