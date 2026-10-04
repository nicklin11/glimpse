"""The output bundle: what a run leaves behind.

Separate from `workspace.py` because the two have opposite lifecycles. The work dir is
scratch: it is removed on success unless asked otherwise. The bundle is the deliverable
and must survive. Folding them together would mean either keeping 137.9 MiB of extracted
audio on every successful run, or deleting the note.

**The default is `$XDG_STATE_HOME/glimpse/<lecture>`, not `./output/<lecture>`.** ADR-0001 D9
exists because artefacts written into a working directory end up in whatever repository
the user happened to be standing in. A CWD-relative default reintroduces that one level
down: run it from a git checkout and a 137.9 MiB wav plus a 3.7 MB transcript land in the
tree. XDG_STATE_HOME is per-user, per-purpose, and the conventional home for exactly this.

The vault is an **export target**, not the root. `export_to_vault` copies finished files
and verifies each one landed. Two write paths with different semantics is how a tool ends
up with half a note in the vault, so there is only ever one writer and the destination is
checked.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

NOTE_NAME = "note.md"
AUDIT_NAME = "audit.md"
IMAGES = "images"


def state_root() -> Path:
    """`$XDG_STATE_HOME/glimpse`, or `~/.local/state/glimpse`."""
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".local/state"
    return base / "glimpse"


def slugify(name: str) -> str:
    """A filesystem-safe directory name.

    Cyrillic is kept: the lectures here are `Оптимальные СУ`, and transliterating them to
    `Optimal Control Systems` would mean the directory does not match its contents. Only
    characters that are genuinely unusable are replaced.
    """
    cleaned = "".join(c if (c.isalnum() or c in " .-_") else "-" for c in name.strip())
    cleaned = "-".join(cleaned.split())
    return cleaned.strip("-.") or "lecture"


@dataclass
class Bundle:
    """One run's output directory."""

    root: Path
    created: bool = False
    artefacts: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        source: Path | str | None = None,
        *,
        output_dir: str | None = None,
        overwrite: bool = False,
    ) -> Bundle:
        """Resolve the bundle root and create it.

        Precedence: `--output-dir` > `GLIMPSE_OUTPUT_DIR` > `$XDG_STATE_HOME/glimpse`.
        The name comes from the source file's stem unless an explicit directory was given,
        in which case that directory *is* the bundle root -- `--output-dir /tmp/x` must not
        produce `/tmp/x/1_lecture_OCS`, which is a different path than the user asked for.
        """
        explicit = output_dir or os.environ.get("GLIMPSE_OUTPUT_DIR", "").strip() or None
        if explicit:
            root = Path(explicit).expanduser().resolve()
        else:
            stem = slugify(Path(source).stem) if source else "lecture"
            root = (state_root() / stem).resolve()

        if root.exists() and overwrite:
            shutil.rmtree(root)
        existed = root.exists()
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root, created=not existed)

    @property
    def images(self) -> Path:
        target = self.root / IMAGES
        target.mkdir(parents=True, exist_ok=True)
        return target

    def path(self, name: str) -> Path:
        return self.root / name

    def record(self, key: str, path: Path) -> Path:
        self.artefacts[key] = Path(path)
        return Path(path)

    def publish(self, key: str, path: Path, *, name: str | None = None) -> Path:
        """Move a work-dir artefact into the bundle and remember it.

        `shutil.move` across filesystems is a copy plus an unlink; if the source is on
        another mount the copy can fail halfway, which is why the destination is verified
        afterwards rather than assumed. A 3.7 MB transcript is cheap to re-run; a
        half-written note is not something to discover later.
        """
        source_path = Path(path)
        if not source_path.exists():
            return source_path
        destination = self.root / (name or source_path.name)
        try:
            shutil.move(str(source_path), str(destination))
        except OSError:
            # Different mount, or a rename that is not permitted. Fall back to a copy.
            shutil.copy2(str(source_path), str(destination))
        self.record(key, destination)
        return destination

    def summary(self) -> str:
        if not self.artefacts:
            return "bundle empty"
        total = 0
        for path in self.artefacts.values():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return f"{len(self.artefacts)} artefacts, {total / 1_048_576:.1f} MiB"

    def export_to_vault(self, vault: Path, *, subdir: str | None = None) -> list[Path]:
        """Copy finished files into the vault, verifying each one arrived.

        A copy, never a move: the bundle stays the single writer, and a failed export
        leaves an intact bundle rather than a half-consumed one.
        """
        root = Path(vault).expanduser() / subdir if subdir else Path(vault).expanduser()
        copied: list[Path] = []
        for key, source in sorted(self.artefacts.items()):
            if not source.is_file():
                continue
            target_dir = root / IMAGES if source.parent.name == IMAGES else root
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            shutil.copy2(str(source), str(target))
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                raise OSError(
                    f"export of {key} did not verify: {target} is missing or a different size"
                )
            copied.append(target)
        return copied
