"""Dependency detection for `glimpse doctor` (ADR-0001 stage 0).

Two failure modes, and keeping them apart is load-bearing:

  * not on PATH              -> MISSING_DEPENDENCY (2), name + remediation line
  * on PATH but errors out   -> DEPENDENCY_FAILED (3), its stderr VERBATIM

Collapsing them into one "not working" state would lose exactly the
information a user needs: whether to install something or fix something.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import exitcodes as ec

GATEWAY_ENV = "GLIMPSE_GATEWAY_URL"
VAULT_ENV = "GLIMPSE_VAULT"
DEFAULT_VAULT = Path.home() / "Documents/obs_notes"
PROBE_TIMEOUT = 10.0


@dataclass
class Check:
    """One dependency's verdict. `code` is the exit code if this check fails."""

    name: str
    ok: bool
    detail: str
    path: str | None = None
    version: str | None = None
    remediation: str | None = None
    stderr: str | None = None
    code: int = ec.OK
    fatal: bool = True

    @property
    def failed(self) -> bool:
        return not self.ok and self.fatal


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False)


def check_executable(
    name: str,
    probe: list[str],
    remediation: str,
    report: str = "version",
) -> Check:
    """Locate `name` on PATH, then prove it actually runs.

    `probe` is invoked only after PATH resolution succeeds, so a broken install
    is distinguishable from an absent one.

    `report="version"` shows the probe's first output line, which is only
    truthful when the probe IS a version command. For a tool with no --version
    flag, pass report="ok": printing argparse usage under a "version" heading
    tells the reader nothing and misleads them into thinking it is one.
    """
    exe = shutil.which(name)
    if exe is None:
        return Check(
            name=name,
            ok=False,
            detail="not found on PATH",
            remediation=remediation,
            code=ec.MISSING_DEPENDENCY,
        )

    try:
        proc = _run([exe, *probe])
    except subprocess.TimeoutExpired:
        return Check(
            name=name,
            ok=False,
            detail=f"timed out after {PROBE_TIMEOUT:.0f}s running `{' '.join(probe)}`",
            path=exe,
            remediation=f"the binary hangs on `{' '.join(probe)}`; check the install",
            code=ec.DEPENDENCY_FAILED,
        )
    except OSError as exc:
        return Check(
            name=name,
            ok=False,
            detail=f"could not execute: {exc}",
            path=exe,
            remediation=f"check permissions on {exe}",
            code=ec.DEPENDENCY_FAILED,
        )

    if proc.returncode != 0:
        # Verbatim: no paraphrase, no truncation beyond what the dependency said.
        return Check(
            name=name,
            ok=False,
            detail=f"`{' '.join(probe)}` exited {proc.returncode}",
            path=exe,
            stderr=proc.stderr.strip(),
            code=ec.DEPENDENCY_FAILED,
        )

    if report != "version":
        return Check(name=name, ok=True, detail="ok", path=exe, version="ok")

    raw = proc.stdout.strip()
    first = raw.splitlines()[0].strip() if raw else ""
    return Check(
        name=name,
        ok=True,
        detail="ok",
        path=exe,
        version=first or "ok",
    )


def check_ffmpeg() -> Check:
    return check_executable("ffmpeg", ["-version"], "install: pacman -S ffmpeg")


def check_ffprobe() -> Check:
    return check_executable("ffprobe", ["-version"], "install: pacman -S ffmpeg")


def check_shipboard() -> Check:
    """D2: STT is delegated, never reimplemented. whisper.cpp stays CPU-only.

    Probed with `--help` rather than a real transcription: doctor must not burn
    a lecture-length request to answer "is this installed". shipboard exposes no
    --version flag, so its usage output is not reported as a version.
    """
    return check_executable(
        "shipboard",
        ["--help"],
        "install: pipx install shipboard   (local checkout: pipx install -e ~/Coding/shipboard)",
        report="ok",
    )


def check_vault() -> Check:
    """D9: artefacts live in the vault, never in this repository."""
    vault = Path(os.environ.get(VAULT_ENV, str(DEFAULT_VAULT))).expanduser()
    if not vault.exists():
        return Check(
            name="vault",
            ok=False,
            detail=f"{vault} does not exist",
            remediation=f"create it, or point {VAULT_ENV} at the right path",
            code=ec.MISSING_DEPENDENCY,
        )
    if not vault.is_dir():
        return Check(
            name="vault",
            ok=False,
            detail=f"{vault} is not a directory",
            remediation=f"point {VAULT_ENV} at the vault directory",
            code=ec.MISSING_DEPENDENCY,
        )
    probe = vault / ".glimpse-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            name="vault",
            ok=False,
            detail=f"{vault} is not writable: {exc}",
            path=str(vault),
            remediation="fix ownership or permissions on the vault",
            code=ec.DEPENDENCY_FAILED,
        )
    return Check(
        name="vault",
        ok=True,
        detail="writable",
        path=str(vault),
        version="ok",
    )


def check_gateway() -> Check:
    """D8: the vision/audit backend. Unconfigured is not a failure."""
    url = os.environ.get(GATEWAY_ENV, "").strip()
    if not url:
        # ok=False because nothing was actually verified; fatal=False because an
        # unconfigured gateway is a state, not a fault. `failed` requires both.
        return Check(
            name="gateway",
            ok=False,
            detail=f"not configured (set {GATEWAY_ENV} to enable the vision/audit stages)",
            version="skipped",
            fatal=False,
        )
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        return Check(
            name="gateway",
            ok=False,
            detail=f"{url} returned HTTP {exc.code}",
            remediation="check the gateway is up and the model name is declared",
            code=ec.DEPENDENCY_FAILED,
        )
    except (urllib.error.URLError, OSError) as exc:
        return Check(
            name="gateway",
            ok=False,
            detail=f"{url} unreachable: {exc}",
            remediation="check the gateway is running and reachable from this host",
            code=ec.DEPENDENCY_FAILED,
        )
    return Check(
        name="gateway",
        ok=True,
        detail="reachable",
        path=url,
        version=f"HTTP {code}",
    )


def run_all(*, required: frozenset[str] | None = None) -> list[Check]:
    """Every check, with `required` names promoted to fatal.

    `required` exists because "the dependency is broken" and "this run needs
    that dependency" are different questions. `glimpse process` stages 0-3 use
    ffmpeg, ffprobe and shipboard and write nothing outside the managed work
    dir, so refusing to transcribe because the *vault* is missing -- a directory
    stage 12 will need in a later milestone -- is the wrong answer, and so is
    refusing because the stage-5 vision gateway is offline. Without this the
    preflight gate is broader than the run it guards.
    """
    checks = [
        check_ffmpeg(),
        check_ffprobe(),
        check_shipboard(),
        check_gateway(),
        check_vault(),
    ]
    if required is None:
        return checks
    wanted = frozenset(required)
    for check in checks:
        if check.name in wanted:
            check.fatal = True
        elif not check.ok:
            # Reported, but not a reason to refuse: print it with the rest so a
            # degraded environment is visible without being fatal.
            check.fatal = False
    return checks


# What `glimpse process` stages 0-3 actually invoke.
PROCESS_REQUIRES = frozenset({"ffmpeg", "ffprobe", "shipboard"})


# Precedence when several checks fail at once. MISSING wins over FAILED: a
# missing binary is the prerequisite, installing it may clear the others, and
# it is the one state with a definite remediation. Every failed check is still
# reported in full, so choosing an exit code loses no information either way.
_CODE_PRECEDENCE = (ec.MISSING_DEPENDENCY, ec.DEPENDENCY_FAILED)


def worst_code(checks: list[Check]) -> int:
    """The most actionable exit code across failed checks; OK if none failed."""
    codes = {c.code for c in checks if c.failed}
    for code in _CODE_PRECEDENCE:
        if code in codes:
            return code
    return ec.OK
