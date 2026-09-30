# 0004. Benchmark as-of date is 2026-09-30

**Status:** Accepted
**Date:** 2026-09-29

## Context

The benchmark's relative dates ("last month", "this quarter") resolve against an as-of date. It was
set to 2026-09-15 while the generated data runs to 2026-09-30. Left alone, "this month" and pacing
questions would see all of September, and making them behave would require cutting the last two weeks
out of every table. That cut would also show final restated values for Sept 12-14, which nobody
could have seen on the 15th, and would distort the late-arriving data the later passes plant.

## Decision

Set the as-of date to **2026-09-30**, the last day of the data. The frozen `BENCHMARK` database is a
zero-copy clone of the marts taken at that point, before the live stream writes anything later.
Marts also carry a `date <= as_of` filter driven by one dbt variable, as a safeguard.

| Phrase | Resolves to |
|---|---|
| last month | August 2026 |
| this month | September 2026 (complete but provisional) |
| this quarter | Q3 2026 (complete) |
| last quarter | Q2 2026 |
| yesterday | 2026-09-29 |

## Consequences

- The last few days are immature by design: conversions trail clicks, refunds on September orders
  arrive later, and Meta restates its last three days. September figures are provisional, so headline
  benchmark questions avoid September. It appears where immaturity is the point (Q24, Q29, Q31, Q40).
- Pacing (Q9) needs an explicit date, since September is already over: "As of September 15, which
  campaigns were on pace to overspend September's budget?"
- Q3 2026 questions are complete-quarter questions.
- Nothing has to be time-traveled: the frozen benchmark is the full backfill.
