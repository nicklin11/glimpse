"""How many stages exist, and how many are built.

Its own module because three others need it. `pipeline` owns the list of stage calls,
`cli` owns the exit codes, and each stage module writes its own progress line -- and a
stage module that imports `pipeline` to ask "how many stages are there?" is a circular
import, since `pipeline` imports every stage module.

Keeping this in one place is what stops the progress output from claiming stages that do
not exist. `[6/12] caption` on a six-stage pipeline is a claim about the software, not a
formatting preference.
"""

from __future__ import annotations

#: Stage 0 is `doctor`, run by the CLI. The twelve-stage list is ADR-0001 § *The pipeline*
#: -- it is not any numbered decision; `D3` is about reading frames from the source.
IMPLEMENTED = 12

#: Stages ADR-0001 § *The pipeline* assigns to the pipeline. The count in every progress
#: line is IMPLEMENTED, not the 12 that section describes: a line reading "[6/12] caption"
#: next to "[4/6] frames" tells the reader two different things about how much of this
#: software exists.
FIRST_STAGE = 1

REMAINING_NOTE = "every stage in ADR-0001 § The pipeline is built"
