# 0009. Pin data library versions and reconcile every build against a reference

**Status:** Proposed (owner to accept)
**Date:** 2026-10-04

## Context

Decision 0001 says the same seed and config always produce identical data. That held only for one set of
library versions. During governance run 3, the same query returned $25,758.38 on the owner's Mac and $25,758.37
on another machine. Rebuilding with each environment's exact versions isolated the cause: **numpy 2.5.3 vs
2.4.4** changes some random draws. Across the whole warehouse, the older version produced 271 more web events
and moved Google cost by $0.10 and Meta spend by $0.70. Order, customer and Meta row counts were identical, so
the drift was easy to miss. The operating system was not a factor: a Linux build with the owner's versions matched
the Mac exactly.

The benchmark depends on known answers, so unexplained drift would make a correct agent look wrong.

## Decision

- Pin every library that affects the generated data to an exact version in `requirements.txt`, using the owner's
  environment as the reference.
- Commit reference totals for each scale (`generator/reference_totals/scale_<scale>.json`): row counts for every raw
  table, exact (DECIMAL) sums of every money column, and key counts, recorded by the owner with
  `python generator/reconcile.py --write`.
- `python generator/reconcile.py` compares any build with the reference, lists every difference and the library
  versions that differ, and fails CI on a mismatch.
- Upgrading a pinned data library is a deliberate change: rebuild, review the differences, and record a new
  reference in the same commit.

## Consequences

- "Same seed + same config = identical output" now reads "same seed, config and pinned versions".
- Gold answers for the benchmark must come from the reconciled dataset.
- Money is still stored as DOUBLE in staging; the reference sums cast to DECIMAL, so the check itself is exact.
  Moving staging money columns to DECIMAL is a separate, later change.
