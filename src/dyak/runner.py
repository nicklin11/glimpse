"""Running an external dependency, and the exit-code contract around it.

`deps.py` answers "is this installed and does it run?" for `dyak doctor`. This
module answers the different question the stages ask: *I need this to produce a
file right now, so what happened?*

Two failure modes, kept apart exactly as in `deps.py`:

  * not on PATH            -> MISSING_DEPENDENCY (2), name + remediation line
  * present but non-zero   -> DEPENDENCY_FAILED (3), its stderr VERBATIM

**Verbatim means bytes.** The subprocess is captured without `text=True`, and the
caller writes the raw bytes through untouched. Decoding to str first would be a
lossy transcription of the failure: a dependency that reports a non-UTF-8 byte in
an error path, or that terminates mid-line, would have its last line mangled or
truncated by the very layer meant to report it faithfully (ADR-0001 D2). A dependency that
prints nothing to stderr is reported as having printed nothing, rather than being
silently represented by an empty blob the reader cannot tell apart from a dropped
one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import exitcodes as ec

# ffmpeg and ffprobe answer in milliseconds on a fast local run. STT is a
# lecture-length HTTP request, so its timeout is set by the caller.
DEFAULT_TIMEOUT = 120.0


class DependencyError(Exception):
    """A dependency could not be run, or ran and failed. Carries everything.

    `stderr` is the raw byte string the process emitted. It is empty when the
    process said nothing -- which is information, and is reported as such.
    """

    def __init__(
        self,
        name: str,
        code: int,
        message: str,
        *,
        stderr: bytes = b"",
        stdout: bytes = b"",
        remediation: str | None = None,
        argv: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.code = code
        self.message = message
        self.stderr = stderr
        # Kept apart from `stderr` on purpose. `report()` prints `stderr` in the
        # position ADR-0001 D2 reserves for the dependency's own stderr; feeding it
        # captured stdout there would present the wrong stream as verbatim.
        self.stdout = stdout
        self.remediation = remediation
        self.argv = argv or []


def emit_verbatim(data: bytes) -> bool:
    """Write a dependency's stderr to our stderr, unmodified. True if it wrote.

    No prefix, no re-wrapping, no per-line indentation. Every extra character we
    add is a character the dependency did not emit, and the whole point of ADR-0001 D2 is
    that the reader can trust these lines are the dependency's own.

    Falls back to a lossy decode only when our stderr has no binary buffer
    (a redirected StringIO, i.e. the test harness).

    The two flushes in this module are not redundant with the line buffering #100 added.
    #100 reconfigured the *text* streams at the CLI entrypoint; this writes through
    `sys.stderr.buffer`, the raw binary layer underneath the TextIOWrapper, which line
    buffering does not cover. #100's problem was a buffered *progress* log going silent for
    16 minutes; this is a dependency's stderr, printed at the moment it fails and followed
    immediately by a process exit.
    """
    if not data:
        return False
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is None:
        sys.stderr.write(data.decode("utf-8", errors="replace"))
    else:
        buffer.write(data)
        buffer.flush()
    return True


def report(exc: DependencyError) -> None:
    """Print a DependencyError in the ADR-0001 shape: our lines, then its bytes.

    The trailing `sys.stderr.flush()` covers the `print()` calls above, which go through the
    text layer that `emit_verbatim` bypasses. See its docstring for why this flush remains.
    """
    print(f"dyak: {exc.message}", file=sys.stderr)
    if exc.remediation:
        print(f"dyak:   remediation: {exc.remediation}", file=sys.stderr)
    if exc.stderr:
        emit_verbatim(exc.stderr)
    elif not exc.stdout:
        # Silence is a claim about the dependency; say that it was silence
        # rather than let it read as a dropped message.
        print(f"dyak:   ({exc.name} wrote nothing to stderr)", file=sys.stderr)
    if exc.stdout:
        # Labelled, because it is stdout and not the stderr ADR-0001 D2 promises above.
        print(f"dyak:   {exc.name} stdout:", file=sys.stderr)
        emit_verbatim(exc.stdout)
    sys.stderr.flush()


def resolve(name: str, remediation: str) -> str:
    """Locate `name` on PATH or raise MISSING_DEPENDENCY."""
    exe = shutil.which(name)
    if exe is None:
        raise DependencyError(
            name=name,
            code=ec.MISSING_DEPENDENCY,
            message=f"required dependency not found on PATH: {name}",
            remediation=remediation,
        )
    return exe


def run(
    name: str,
    args: list[str],
    *,
    remediation: str,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
) -> bytes:
    """Run `name args...`, return stdout as bytes, raise DependencyError otherwise."""
    exe = resolve(name, remediation)
    argv = [exe, *args]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DependencyError(
            name=name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{name} did not finish within {timeout:.0f}s",
            stderr=_as_bytes(exc.stderr),
            remediation=f"`{' '.join(args)}` hung; raise the timeout or check the backend",
            argv=args,
        ) from exc
    except OSError as exc:
        raise DependencyError(
            name=name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{name} is on PATH but could not be executed: {exc}",
            remediation=f"check permissions on {exe}",
            argv=args,
        ) from exc

    if proc.returncode != 0:
        raise DependencyError(
            name=name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{name} exited {proc.returncode} running `{' '.join(args)}`",
            stderr=proc.stderr or b"",
            argv=args,
        )
    return proc.stdout or b""


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8", "replace")
