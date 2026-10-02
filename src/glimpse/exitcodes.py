"""Exit codes are a contract, not documentation (ADR-0001).

Every stage imports its code from here rather than hardcoding an integer, so
that the table in the ADR and the behaviour of the binary cannot drift apart.
If a stage invents its own exit path, the table becomes fiction.
"""

from __future__ import annotations

# 0 — success; all artefacts written and verified on disk.
OK = 0

# 1 — usage error. Also used for a subcommand that is not implemented yet:
# invoking something this build cannot do is a usage error.
USAGE = 1

# 2 — missing dependency. The check names it and prints a remediation line.
# `process` refuses to start rather than silently degrading.
MISSING_DEPENDENCY = 2

# 3 — dependency present but failed. Its stderr is reproduced VERBATIM (D2):
# a tool that swallows its dependency's errors is worse than no tool, because
# the failure looks like success.
DEPENDENCY_FAILED = 3

# 4 — quality gate failed. The note is still written; the report says why (D4).
QUALITY_GATE_FAILED = 4

# 5 — the audit found errors above threshold. The note is written and FLAGGED.
AUDIT_FINDINGS = 5

DESCRIPTIONS: dict[int, str] = {
    OK: "success",
    USAGE: "usage error",
    MISSING_DEPENDENCY: "missing dependency (named, with remediation)",
    DEPENDENCY_FAILED: "dependency failed (stderr reproduced verbatim)",
    QUALITY_GATE_FAILED: "quality gate failed; note written, report says why",
    AUDIT_FINDINGS: "audit found errors above threshold; note written and flagged",
}