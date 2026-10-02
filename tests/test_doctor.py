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


gld.shutil.which = fake_which
gld.subprocess.run = fake_run

# the gateway must never be probed against the real network in this test
os.environ.pop(gld.GATEWAY_ENV, None)
tmp = Path(tempfile.mkdtemp())
os.environ[gld.VAULT_ENV] = str(tmp)


def restore():
    gld.shutil.which = real_which
    gld.subprocess.run = real_run


# --- 1. everything present -> 0, with path and version listed -----------------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
text = out.getvalue()
check("all present -> exit 0", rc == ec.OK, f"rc={rc}")
check("nothing on stderr when healthy", err.getvalue() == "", err.getvalue())
check("every dependency is listed",
      all(n in text for n in ("ffmpeg", "ffprobe", "shipboard", "gateway", "vault")),
      text)
check("resolved path is reported", "/usr/bin/ffmpeg" in text, text)
check("version is reported", "ffmpeg version 7.1.1-1" in text, text)
check("unconfigured gateway is skipped, not failed", "[skip]" in text, text)

# --- 2. missing dependency -> 2, named, with remediation ----------------------
which_set = {"ffmpeg", "ffprobe"}  # shipboard gone
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["doctor"])
msg = err.getvalue()
check("missing dep -> exit 2", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
check("missing dep is named", "shipboard" in msg, msg)
check("missing dep carries a remediation line", "remediation:" in msg, msg)
check("remediation names the install command", "pipx install" in msg, msg)
check("present deps still reported as ok", "ok" in out.getvalue())

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
check("stderr reproduced verbatim (full first line)",
      "ffmpeg: error while loading shared libraries: libavutil.so.60: "
      "cannot open shared object file" in msg, msg)
check("stderr reproduced verbatim (indented line, quotes intact)",
      "  extra indented line: '{\"a\": 1}'" in msg, msg)
check("a present-but-broken dep is NOT reported as missing",
      "remediation: pacman" not in msg, msg)
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
check("absent vault -> 2",
      gld.check_vault().code == ec.MISSING_DEPENDENCY, gld.check_vault().detail)

a_file = tmp / "a-file"
a_file.write_text("x")
os.environ[gld.VAULT_ENV] = str(a_file)
check("vault that is a file -> 2",
      gld.check_vault().code == ec.MISSING_DEPENDENCY, gld.check_vault().detail)

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
check("vault probe cleans up after itself",
      not (tmp / ".glimpse-write-probe").exists())

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
check("shipboard usage output is NOT labelled a version",
      "usage: shipboard" not in text, text)
check("shipboard still reports ok + path",
      "[ok  ] shipboard" in text and "/usr/bin/shipboard" in text, text)

# ffmpeg DOES have a real version line, so it must still be reported
check("a real version command is still reported",
      "ffmpeg version 7.1.1-1" in text, text)
check("ffprobe version reported separately",
      "ffprobe version 7.1.1-1" in text, text)

# --- 8. unimplemented subcommands point at their issue, not a dead end -------
for cmd, issue in (("process", "#3"), ("audit", "#11")):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = glc.main([cmd])
    check(f"`{cmd}` -> exit 1", rc == ec.USAGE, f"rc={rc}")
    check(f"`{cmd}` names its tracking issue", issue in err.getvalue(), err.getvalue())
    check(f"`{cmd}` suggests doctor", "doctor" in err.getvalue(), err.getvalue())

# unknown subcommand is a usage error
try:
    with redirect_stderr(io.StringIO()):
        glc.main(["nonsense"])
    check("unknown subcommand rejected", False, "accepted")
except SystemExit as exc:
    check("unknown subcommand rejected", exc.code != 0, f"code={exc.code}")

restore()

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all doctor checks passed")