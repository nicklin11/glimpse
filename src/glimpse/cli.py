"""Entry point and subcommand dispatch (ADR-0001 D1).

The surface is the documentation: a binary name cannot teach another agent what
a tool does, so `glimpse --help` and each subcommand's help can.
"""

from __future__ import annotations

import argparse
import sys

from . import exitcodes as ec
from .deps import Check, run_all, worst_code

# Stages not built yet. Each names its tracking issue so the failure is a
# pointer, not a dead end.
PENDING: dict[str, str] = {
    "process": "stages 0-12, tracked in #3",
    "audit": "stage 9, tracked in #11",
}


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
        if c.code == ec.MISSING_DEPENDENCY:
            print(f"glimpse:   remediation: {c.remediation}", file=sys.stderr)
        # D2: the dependency's own stderr, reproduced verbatim.
        if c.stderr:
            print(f"glimpse:   {c.name} stderr:", file=sys.stderr)
            for line in c.stderr.splitlines():
                print(f"glimpse:     {line}", file=sys.stderr)
    return worst_code(checks)


def _pending(name: str) -> int:
    print(
        f"glimpse: `{name}` is not implemented yet ({PENDING[name]}).\n"
        f"glimpse: run `glimpse doctor` for what is available.",
        file=sys.stderr,
    )
    return ec.USAGE


def _process(args: argparse.Namespace) -> int:
    return _pending("process")


def _audit(args: argparse.Namespace) -> int:
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
        help="run the full pipeline on a recording",
        description="Transcribe, extract frames, synthesise, audit, link. "
        "Not implemented in this build.",
    )
    p_process.add_argument("path", nargs="?", help="video or audio file")
    p_process.set_defaults(fn=_process)

    p_audit = sub.add_parser(
        "audit",
        help="audit a note that already exists",
        description="Run the audit stage against an existing note. "
        "Not implemented in this build.",
    )
    p_audit.add_argument("note", nargs="?", help="Markdown note to audit")
    p_audit.set_defaults(fn=_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())