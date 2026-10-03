"""The managed work directory for one `glimpse process` run (ADR-0001 stage 2).

Stage 2's contract is "extract to a managed temp dir, **kept on failure** so a
failed run can be inspected". That is the whole reason this is a type rather than
a `tempfile.TemporaryDirectory` in a function body: `TemporaryDirectory` cleans
up in `__exit__` on the way out of the `with` block *including* the exception
path, which is precisely the behaviour the ADR rejects. A failed transcribe that
throws its extracted wav away costs a full re-extraction of a 73-minute source to
debug.

Retention is explicit, not automatic:

  * success -> the directory is removed, because nothing outside it is published
    until stage 12;
  * failure -> it is kept and its path printed, because the artefacts in it are
    the only evidence of what the stage actually produced;
  * `--keep-workdir` -> kept on success too, for inspection of a good run.

The directory is created with mode 0700 by `mkdtemp`. Lecture material is not
public data, and a predictable world-readable path under /tmp is the wrong
default even on a single-user box.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

WORKDIR_ENV = "GLIMPSE_WORKDIR"
PREFIX = "glimpse-"


class WorkDir:
    """A per-run scratch directory with explicit release or retain semantics."""

    def __init__(self, *, keep: bool = False, parent: Path | None = None) -> None:
        self.keep = keep
        base = parent or _parent_from_env()
        base.mkdir(parents=True, exist_ok=True)
        # mkdtemp already creates 0700; stated explicitly because the umask of
        # the calling shell otherwise decides it on some platforms.
        self.path = Path(tempfile.mkdtemp(prefix=PREFIX, dir=str(base)))
        self.path.chmod(stat.S_IRWXU)
        # `--keep-workdir` is retention requested up front, so it must already
        # read as retained: release() keys off this flag, and note() must not
        # claim a successful run threw its artefacts away when it did not.
        self.retained = self.keep
        self.retained_reason = "requested with --keep-workdir" if self.keep else None

    def release(self) -> None:
        """Success path: remove the directory unless it is retained."""
        if self.retained:
            return
        try:
            shutil.rmtree(self.path)
        except OSError as exc:
            # Reporting "removed" when the rmtree failed is exactly the
            # unverified outcome this module exists to refuse: a full lecture
            # transcript would stay in $TMPDIR under a claim that it is gone.
            self.retain(f"could not be removed: {exc}")

    def retain(self, reason: str | None = None) -> bool:
        """Keep the directory. Returns whether it is now retained."""
        self.retained = True
        if reason:
            self.retained_reason = reason
        return True

    def note(self) -> str:
        """One line describing where the artefacts are, or that the scratch is gone.

        This deliberately does not claim the run succeeded. Retention is decided when the
        pipeline finishes, before the exit code is: a degraded run removes its scratch
        just as a clean one does, and the first version of this line printed "run
        succeeded" next to exit 1 because the directory happened to be gone.
        """
        if not self.retained:
            return "work dir removed (scratch; the bundle holds the artefacts)"
        suffix = f" -- {self.retained_reason}" if self.retained_reason else ""
        return f"work dir KEPT for inspection: {self.path}{suffix}"

    def sub(self, *parts: str) -> Path:
        """A path inside the work dir, with parent directories created.

        Note what this does *not* do: create `parts[-1]`. For "audio.wav" that is right --
        the parent is the work dir. For a subdirectory it is a trap: `work.sub("quality")`
        returns a path whose own directory does not exist, and the first write into it
        raises FileNotFoundError. Use `dir()` for directories.
        """
        target = self.path.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def dir(self, *parts: str) -> Path:
        """A directory inside the work dir, created."""
        target = self.sub(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return target


def _parent_from_env() -> Path:
    """`GLIMPSE_WORKDIR` sets the parent of the managed dir, not the dir itself.

    A per-run subdirectory is still created under it: two concurrent runs on the
    same machine must not collide on `audio.wav`.
    """
    configured = os.environ.get(WORKDIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir())
