"""Entry point and subcommand dispatch (ADR-0001 D1).

The surface is the documentation: a binary name cannot teach another agent what
a tool does, so `glimpse --help` and each subcommand's help can.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import exitcodes as ec
from . import pipeline as glp
from . import runner
from .deps import PROCESS_REQUIRES, Check, run_all, worst_code
from .workspace import WorkDir

# Subcommands and stages that are not built yet. Each names its tracking issue
# so the failure is a pointer, not a dead end.
PENDING: dict[str, str] = {
    "audit": "stage 9, tracked in #11",
}

# `process` is implemented for stages 0-4 only. It still exits non-zero when
# those stages succeed, because no note was produced -- see _process.
PROCESS_INCOMPLETE = (
    f"stages 0-{glp.IMPLEMENTED} completed; {glp.REMAINING_NOTE}. No note was written."
)


def _render(checks: list[Check]) -> None:
    width = max((len(c.name) for c in checks), default=4)
    for c in checks:
        if c.ok:
            mark = "ok  "
        elif c.fatal:
            mark = "FAIL"
        else:
            mark = "skip"
        print(f"  [{mark}] {c.name.ljust(width)}  {c.detail}")
        if c.path:
            print(f"           {'path'.ljust(width)}  {c.path}")
        if c.version and c.version != "ok":
            print(f"           {'version'.ljust(width)}  {c.version}")


def _doctor(_args: argparse.Namespace) -> int:
    """Stage 0. Reports what is missing, what is present, and what to do."""
    checks = run_all()
    _render(checks)

    failed = [c for c in checks if c.failed]
    if not failed:
        print("\nglimpse: all required dependencies present.")
        return ec.OK

    print("", file=sys.stderr)
    for c in failed:
        print(f"glimpse: missing or broken dependency: {c.name}", file=sys.stderr)
        # The remediation is printed for *every* failure, not only for exit 2. It used to
        # be gated on MISSING_DEPENDENCY, which meant a dependency that is installed and
        # unreachable -- the case an endpoint backend introduces -- was named and then
        # abandoned with no indication of what to do about it.
        if c.remediation:
            print(f"glimpse:   remediation: {c.remediation}", file=sys.stderr)
        # D2: the dependency's own stderr, reproduced verbatim.
        if c.stderr:
            print(f"glimpse:   {c.name} stderr:", file=sys.stderr)
            for line in c.stderr.splitlines():
                print(f"glimpse:     {line}", file=sys.stderr)
    return worst_code(checks)


def _preflight() -> int:
    """Stage 0 as a gate: return the exit code, or OK if the run may start.

    Only the dependencies stages 0-4 actually invoke are fatal here. The vault
    and the gateway are reported but not enforced: stage 12 and stage 5 need
    them, and neither runs yet. Refusing to transcribe because the vision
    gateway happens to be down would make the gate broader than the run it
    guards.
    """
    checks = run_all(required=PROCESS_REQUIRES)
    failed = [c for c in checks if c.failed]
    degraded = [c for c in checks if not c.ok and not c.failed]

    for c in degraded:
        # Printed before the early return below: a gateway that is down is not a
        # reason to refuse this run, but it is still a fact the user wants
        # before stage 5 rather than after it.
        print(
            f"glimpse: note: {c.name} is unavailable, but stages 0-{glp.IMPLEMENTED} do not use it",
            file=sys.stderr,
        )
    if not failed:
        return ec.OK

    print("glimpse: refusing to start; run `glimpse doctor` for detail.", file=sys.stderr)
    for c in failed:
        print(f"glimpse: {c.name}: {c.detail}", file=sys.stderr)
        if c.remediation:
            print(f"glimpse:   remediation: {c.remediation}", file=sys.stderr)
    return worst_code(checks)


def _pending(name: str) -> int:
    print(
        f"glimpse: `{name}` is not implemented yet ({PENDING[name]}).\n"
        f"glimpse: run `glimpse doctor` for what is available.",
        file=sys.stderr,
    )
    return ec.USAGE


def _process(args: argparse.Namespace) -> int:
    """Stages 0-3: probe -> audio -> stt. Stages 4-12 are not built.

    Exits `USAGE` (1) even when every implemented stage succeeds. Exit 0 in
    ADR-0001 means "all artefacts written and verified", and no note exists yet,
    so claiming success would be the exact silent-degradation failure D2 is
    written against. This follows the precedent stage 0 set: invoking what this
    build cannot do is a usage error.
    """
    if not args.path:
        print("glimpse: process needs a recording to work on.", file=sys.stderr)
        print("glimpse:   usage: glimpse process PATH", file=sys.stderr)
        return ec.USAGE

    code = _preflight()
    if code != ec.OK:
        return code

    parent = Path(args.workdir).expanduser() if args.workdir else None
    try:
        work = WorkDir(keep=args.keep_workdir, parent=parent)
    except OSError as exc:
        # --workdir is user-supplied, so a bad parent is a usage error rather
        # than an internal one, and it is far more likely than a real mkdtemp
        # failure. Checked here because nothing exists yet to retain.
        print(f"glimpse: cannot use the work directory: {exc}", file=sys.stderr)
        print(
            "glimpse:   remediation: --workdir must name an existing writable directory",
            file=sys.stderr,
        )
        return ec.USAGE
    try:
        result = glp.run(Path(args.path).expanduser(), work)
    except runner.DependencyError as exc:
        work.retain(exc.message)
        runner.report(exc)
        print(f"glimpse: {work.note()}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        work.retain("interrupted by the user")
        print(f"glimpse: {work.note()}", file=sys.stderr)
        return ec.INTERRUPTED
    except Exception as exc:  # noqa: BLE001 -- retain, print where, then propagate
        work.retain(f"{type(exc).__name__}: {exc}")
        # An unexpected exception reaches the user as a traceback. Without this
        # line the traceback would be the only thing they see, and the retained
        # artefacts -- the reason retention exists -- would be unfindable.
        print(f"glimpse: {work.note()}", file=sys.stderr)
        raise
    else:
        work.release()

    print()
    print(f"  source     {result.source}")
    print(f"  transcript {result.transcript.summary()}")
    print(f"  stages {glp.FIRST_STAGE}-{glp.IMPLEMENTED} done in {result.seconds:.1f}s")
    print(f"glimpse: {PROCESS_INCOMPLETE}")
    print(f"glimpse: {work.note()}")
    # Artefact paths are printed only when the directory still exists. Pointing
    # the reader at files that release() just deleted is worse than saying
    # nothing: it looks like an output location and is not one.
    if work.retained:
        for name in sorted(p.name for p in work.path.iterdir()):
            print(f"glimpse:   {work.path / name}")
    return ec.USAGE


def _audit(_args: argparse.Namespace) -> int:
    return _pending("audit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glimpse",
        description="One command turns a lecture recording into a readable, "
        "independently audited Markdown note.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser(
        "doctor",
        help="report missing/conflicting dependencies",
        description="Check every external dependency and exit with the reason "
        "if one is absent or broken.",
    )
    p_doctor.set_defaults(fn=_doctor)

    p_process = sub.add_parser(
        "process",
        help="run the pipeline on a recording",
        description="Transcribe, extract frames, synthesise, audit, link. "
        f"Stages 0-{glp.IMPLEMENTED} are implemented; the rest are tracked "
        "in #3, and this exits 1 until they are.",
    )
    p_process.add_argument("path", nargs="?", help="video or audio file")
    p_process.add_argument(
        "--workdir",
        metavar="DIR",
        help="parent directory for the managed work dir (default: $TMPDIR, or $GLIMPSE_WORKDIR)",
    )
    p_process.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep the work dir even when the run succeeds",
    )
    p_process.set_defaults(fn=_process)

    p_audit = sub.add_parser(
        "audit",
        help="audit a note that already exists",
        description="Run the audit stage against an existing note. Not implemented in this build.",
    )
    p_audit.add_argument("note", nargs="?", help="Markdown note to audit")
    p_audit.set_defaults(fn=_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 for a usage error, and 2 is MISSING_DEPENDENCY in the
        # ADR table. Without this remap a typo'd subcommand reads as a missing
        # dependency, and a script branching on the code takes the wrong action.
        code = exc.code
        if code in (0, None):
            return ec.OK
        return ec.USAGE
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
