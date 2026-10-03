"""shipboard, kept as an optional backend.

Kept, not deleted. The payload it returns is byte-identical to whisper.cpp's native
`/inference` — measured, both are a top-level array with the same `words` keys and float
seconds, because shipboard is a passthrough proxy to `127.0.0.1:10302`, which on this host
is this machine.

So the dependency carries no normalisation and dropping it cannot change stage-3 output.
What is *not* visible from outside is whether shipboard owns **model lifecycle**:
`/load` returns 404 on the running container, so the routes that would let `glimpse` load
or release a model itself are not observable here. Until that is confirmed, this backend
stays. It is no longer mandatory: `check()` is only reached when it is the configured
backend, so a user without shipboard installed is unaffected.
"""

from __future__ import annotations

from pathlib import Path

from .. import runner
from .backends import BackendInfo

REMEDIATION = (
    "install: pipx install shipboard   (local checkout: pipx install -e ~/Coding/shipboard)"
)


class Shipboard:
    """Transcribes by shelling out to `shipboard process`."""

    name = "shipboard"

    def available(self) -> bool:
        import shutil

        return shutil.which("shipboard") is not None

    def info(self) -> BackendInfo:
        import shutil

        found = shutil.which("shipboard")
        return BackendInfo(
            name=self.name,
            target=found or "not on PATH",
            detail="subprocess: shipboard process --timestamps json",
            notes=("returns the whisper.cpp /inference payload unchanged",),
        )

    def check(self) -> None:
        # Exit 2 before any extraction work is done.
        runner.resolve(self.name, REMEDIATION)

    def run(self, wav: Path, *, timeout: float) -> bytes:
        self.check()
        return runner.run(
            self.name,
            ["process", str(wav), "--timestamps", "json"],
            remediation=REMEDIATION,
            timeout=timeout,
        )
