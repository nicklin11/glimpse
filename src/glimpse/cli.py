"""Entry point and subcommand dispatch (ADR-0001 D1).

The surface is the documentation: a binary name cannot teach another agent what
a tool does, so `glimpse --help` and each subcommand's help can.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import exitcodes as ec
from . import bundle as glb
from . import link as gllnk
from . import pipeline as glp
from . import runner
from .deps import PROCESS_REQUIRES, Check, run_all, worst_code
from .workspace import WorkDir

# Subcommands that are not built yet. Each names its tracking issue so the failure is a
# pointer, not a dead end. The standalone subcommand is unbuilt; the stage itself runs inside
# `process`, so "stage 9, tracked in #11" would now be a false statement about the pipeline.
PENDING: dict[str, str] = {
    "audit": "the standalone `glimpse audit NOTE` entry point, tracked in #11; stage 9 "
    "does run as part of `process`",
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
    """Stages 1-12: probe -> audio -> stt -> frames -> quality -> caption -> synth -> lint
    -> audit -> repair -> link -> report.

    Exits 0 only when stage 12 has stat'd the filesystem and found every artefact it
    claimed. Exit 0 in ADR-0001 means "all artefacts written and verified", and the
    second half of that is the half every pipeline before this one skipped: D3 records a
    run that produced zero frames and exited successfully, because ffmpeg wrote an empty
    file and exited 0.

    The precedence is 5 (errors in the note) > 4 (soft frames) > 1 (could not verify) >
    0, and every message is printed regardless of which code comes out.
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
        source = Path(args.path).expanduser()
    except (OSError, RuntimeError) as exc:
        print(f"glimpse: cannot expand the input path: {exc}", file=sys.stderr)
        return ec.USAGE
    try:
        output = glb.Bundle.open(
            source,
            output_dir=getattr(args, "output_dir", None),
            overwrite=getattr(args, "overwrite", False),
        )
    except OSError as exc:
        print(f"glimpse: cannot use the output directory: {exc}", file=sys.stderr)
        print(
            "glimpse:   remediation: --output-dir must name a writable location",
            file=sys.stderr,
        )
        return ec.USAGE
    try:
        result = glp.run(
            source,
            work,
            bundle=output,
            glossary_path=getattr(args, "glossary", None),
            terms_dir=getattr(args, "terms_dir", None),
            vault_path=getattr(args, "vault_path", None),
        )
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
    print(f"  output     {result.bundle.root}  ({result.bundle.summary()})")
    vault = getattr(args, "vault_path", None)
    if vault:
        try:
            copied = result.bundle.export_to_vault(Path(vault).expanduser())
        except OSError as exc:
            print(f"glimpse: vault export failed: {exc}", file=sys.stderr)
            print(
                "glimpse:   remediation: the vault is an export target; the bundle is intact",
                file=sys.stderr,
            )
            return ec.DEPENDENCY_FAILED
        print(f"  vault      {len(copied)} files exported to {Path(vault).expanduser()}")
    if not result.quality.ok:
        # D4: the note is still written; a bad frame means the crops are soft, not that
        # the pipeline stopped. The quality report is the artefact that explains why.
        print(
            f"glimpse: quality gate failed for {len(result.quality.failed)}"
            f"/{len(result.quality.frames)} frames; see {result.bundle.root}",
            file=sys.stderr,
        )
        for line in result.quality.explain().splitlines():
            print(f"glimpse: {line.strip()}", file=sys.stderr)
        print(
            "glimpse:   remediation: lower quality.decay to tighten the gate, or "
            "re-extract with a higher source bitrate -- interpolation cannot recover "
            "text that was never encoded",
            file=sys.stderr,
        )
    audit_errors = [f for f in result.audit.findings if f.severity == "ERROR"]
    if audit_errors:
        # D5: the note is written and FLAGGED, never withheld. Exit 5 is that flag.
        # Naming the count and the rule is the whole point -- an exit code with no list
        # attached is a code the reader cannot act on.
        print(
            f"glimpse: audit found {len(audit_errors)} error-tier findings; the note is "
            f"written and flagged, not withheld; see {result.bundle.root}",
            file=sys.stderr,
        )
        for finding in audit_errors[:10]:
            print(
                f"glimpse:   line {finding.line} {finding.rule}: {finding.detail}", file=sys.stderr
            )
        if len(audit_errors) > 10:
            print(
                f"glimpse:   ... and {len(audit_errors) - 10} more in audit/audit.json",
                file=sys.stderr,
            )
        print(
            "glimpse:   remediation: read the quoted line in audit/audit.json and check it "
            "against the recording -- stage 10 repaired what was mechanical and declined "
            "the rest, by name, in repair/repair.json",
            file=sys.stderr,
        )
    print(f"glimpse: {work.note()}")
    # Artefact paths are printed only when the directory still exists. Pointing
    # the reader at files that release() just deleted is worse than saying
    # nothing: it looks like an output location and is not one.
    if work.retained:
        for name in sorted(p.name for p in work.path.iterdir()):
            print(f"glimpse:   {work.path / name}")

    # Exit code precedence, and why it is this order.
    #
    #   5 -- the note exists and contains errors. This is the most specific statement
    #        about the deliverable, so it outranks the others. When the quality gate also
    #        failed, 5 still wins: the reader is told the note has errors AND, above, that
    #        the frames were soft. Reporting 4 alone would hide the note's own verdict.
    #   4 -- the frames were soft. The note exists; its inputs were not.
    #   1 -- the pipeline could not finish, or could not verify what it produced. This is
    #        D3's point: a stage that exits 0 having written nothing has lied, and the
    #        only defence is that something measured the filesystem instead of asking.
    #   0 -- every stage ran and every artefact it claimed to write is on disk.
    if audit_errors:
        return ec.AUDIT_FINDINGS
    if not result.quality.ok:
        return ec.QUALITY_GATE_FAILED
    if not result.report.ok:
        for entry in result.report.missing:
            print(
                f"glimpse: MISSING {entry['artefact']} ({entry['detail']}): {entry['why']}",
                file=sys.stderr,
            )
        print(
            "glimpse:   this is D3: stages above reported success, and the filesystem "
            "disagrees. Not exiting 0.",
            file=sys.stderr,
        )
        return ec.USAGE
    print(f"glimpse: all stages complete; artefacts verified in {result.bundle.root}")
    return ec.OK


def _audit(_args: argparse.Namespace) -> int:
    return _pending("audit")


def _terms_dir(args: argparse.Namespace):
    """Resolved terms directory, or None with the reason already printed.

    `_term_*` refuses to run rather than reporting "no terms found": those two look the
    same in the output and only one of them means the vault was read.
    """
    resolved = gllnk.resolve_terms_dir(
        getattr(args, "terms_dir", None), getattr(args, "vault_path", None)
    )
    if resolved is None:
        print(
            f"glimpse: no terms directory. Pass --terms-dir DIR, set "
            f"{gllnk.TERMS_ENV}, or pass --vault-path DIR "
            f"(looked for <vault>/{gllnk.COURSES_DIRNAME}/{gllnk.TERMS_DIRNAME}).",
            file=sys.stderr,
        )
    return resolved


def _term_link(args: argparse.Namespace) -> int:
    terms_dir = _terms_dir(args)
    if terms_dir is None:
        return ec.USAGE
    terms = gllnk.load_terms(terms_dir)
    if not terms:
        print(f"glimpse: no term notes in {terms_dir}", file=sys.stderr)
        return ec.USAGE
    targets = gllnk.collect_targets(args.paths, terms_dir, args.vault_path)
    if not targets:
        print("glimpse: no target notes to link", file=sys.stderr)
        return ec.USAGE
    report = gllnk.link_paths(targets, terms, write=args.write)
    print(f"glimpse: {report.summary()}")
    if report.per_term:
        top = sorted(report.per_term.items(), key=lambda kv: -kv[1])[:10]
        print("glimpse:   " + ", ".join(f"{name}x{count}" for name, count in top))
    if not args.write and report.changed:
        print("glimpse:   dry run -- pass --write to apply")
    return ec.OK


def _term_index(args: argparse.Namespace) -> int:
    terms_dir = _terms_dir(args)
    if terms_dir is None:
        return ec.USAGE
    terms = gllnk.load_terms(terms_dir)
    lectures = gllnk.collect_targets([], terms_dir, args.vault_path)
    table = gllnk.index_report(lectures, terms, only_used=args.only_used)
    if not args.write:
        print(table)
        return ec.OK
    dest = terms_dir / "_index.md"
    dest.write_text(
        "---\ntype: generated\n---\n\n"
        "> Сгенерировано `glimpse term index --write`. Правь не руками.\n\n" + table + "\n",
        encoding="utf-8",
    )
    print(f"glimpse: wrote {dest}")
    return ec.OK


def _term_check(args: argparse.Namespace) -> int:
    terms_dir = _terms_dir(args)
    if terms_dir is None:
        return ec.USAGE
    terms = gllnk.load_terms(terms_dir)
    lectures = gllnk.collect_targets([], terms_dir, args.vault_path)
    print(gllnk.check_report(terms, lectures, terms_dir))
    return ec.OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glimpse",
        description="Turn a lecture recording into a structured, audited Markdown note. "
        f"Stages {glp.FIRST_STAGE}-{glp.IMPLEMENTED} are implemented. process exits 0 "
        f"only when stage 12 has verified the artefacts on disk: {ec.AUDIT_FINDINGS} if "
        f"the audit found errors, {ec.QUALITY_GATE_FAILED} if frames were soft, "
        f"{ec.DEPENDENCY_FAILED} if a tool failed, {ec.USAGE} if it could not verify.",
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
        "--output-dir",
        metavar="DIR",
        help=(
            "where to write the output bundle (default: "
            "$XDG_STATE_HOME/glimpse/<lecture>, or ./output/<lecture> if unset)"
        ),
    )
    p_process.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output bundle instead of writing into it",
    )
    p_process.add_argument(
        "--vault-path",
        metavar="DIR",
        help="copy the finished bundle into this vault; never a pipeline blocker",
    )
    p_process.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep the work dir even when the run succeeds",
    )
    p_process.add_argument(
        "--glossary",
        metavar="FILE",
        help=(
            "ASR term glossary for stages 9 and 10, in the project's "
            "`| variant | canonical | domain | date |` format. Without it the term-drift "
            "rule finds nothing and the audit says so."
        ),
    )
    p_process.add_argument(
        "--terms-dir",
        metavar="DIR",
        help=(
            "atomic term notes for stage 11. Default: $GLIMPSE_TERMS_DIR, then "
            "<vault-path>/mscs/_terms. Without it the note is written unlinked and the "
            "stage says so."
        ),
    )
    p_process.set_defaults(fn=_process)

    p_audit = sub.add_parser(
        "audit",
        help="audit a note that already exists",
        description="Run the audit stage against an existing note. Not implemented in this build.",
    )
    p_audit.add_argument("note", nargs="?", help="Markdown note to audit")
    p_audit.set_defaults(fn=_audit)

    # `mscs-termlink`'s three subcommands, kept as one group: the ported tool keeps its own
    # vocabulary, and a bare top-level `check` would sit confusingly next to `doctor`.
    p_term = sub.add_parser(
        "term",
        help="term vocabulary: link / index / check the vault's atomic term notes",
        description=(
            "Ported from ~/.local/bin/mscs-termlink (issue #8). The terms directory is "
            "resolved from --terms-dir, then $GLIMPSE_TERMS_DIR, then "
            "<--vault-path>/mscs/_terms."
        ),
    )
    term_sub = p_term.add_subparsers(dest="term_cmd", required=True)

    t_link = term_sub.add_parser("link", help="inject wikilinks; dry run unless --write")
    t_link.add_argument("paths", nargs="*", help="files or directories (default: lecture notes)")
    t_link.add_argument("--write", action="store_true", help="persist the changes")
    t_link.add_argument("--terms-dir", metavar="DIR")
    t_link.add_argument("--vault-path", metavar="DIR")
    t_link.set_defaults(fn=_term_link)

    t_index = term_sub.add_parser("index", help="report which lectures mention which term")
    t_index.add_argument("--write", action="store_true", help="write _index.md")
    t_index.add_argument("--only-used", action="store_true", help="omit terms used nowhere")
    t_index.add_argument("--terms-dir", metavar="DIR")
    t_index.add_argument("--vault-path", metavar="DIR")
    t_index.set_defaults(fn=_term_index)

    t_check = term_sub.add_parser("check", help="diagnostics on terms and lecture notes")
    t_check.add_argument("--terms-dir", metavar="DIR")
    t_check.add_argument("--vault-path", metavar="DIR")
    t_check.set_defaults(fn=_term_check)

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
