# 0006. Certified attribution: last paid click within a 7-day window

**Status:** Accepted
**Date:** 2026-09-29

## Context

Orders must be credited to a campaign to compute ROAS and CPA. Two decisions are involved: which touch
gets the credit, and how far back to look.

In this data every ad order has exactly one tagged click, so touch-based rules (first click, linear,
time-decay, position-based) all give the same answer. The window is what changes results. Measured on the
537K ad orders:

| Window | Ad orders captured |
|---|---|
| 1 day | 69.1% |
| 3 days | 89.9% |
| 7 days | 98.0% |
| 14 days and up | 100% (the generator caps click-to-order lag at 14 days) |

## Decision

- **Rule:** last tagged marketing touch: a paid click (Google `gclid` or Meta `fbclid`) or a tagged email link
  (`utm_medium = email`). Direct or untagged visits do not override an earlier touch (a strict "last click" would
  credit the final direct visit and mark most later-visit orders as direct). Email counts as a touch so email-driven
  revenue is not left unattributed, even though email has no media spend to compute ROAS against (Q19).
- **Window:** 7 days, set by the dbt variable `attribution_window_days` (default 7).
- **Data used:** first-party web events only. Click IDs and UTM tags link a purchase to a campaign through the
  shared `anonymous_id`. No view-through credit, because there is no impression-level data.
- **Unattributed orders are kept** in an explicit "unattributed" bucket, never dropped (Q34).
- The attribution model stores each order's candidate touch (up to a 30-day lookback, `attribution_max_lookback_days`)
  and its lag in days, so any window can be recomputed downstream. Q35 ("recompute both quarters under one definition") becomes a parameter change.
- The window is deliberately independent of Meta's platform setting. Meta's own window changed on
  2026-01-01; the certified first-party definition does not.

## Why 7 days

It captures 98% of ad orders, and it matches Meta's setting after 2026-01-01 (7-day click), so differences between
platform-reported and first-party numbers reflect over-reporting, not different windows.

## Consequences

- Choosing 1 day instead of 7 would change attributed revenue by about 31%; 14 instead of 7 by about 2%.
- Last-paid-click over-credits bottom-of-funnel campaigns and under-credits awareness channels. It is a
  convention, not the truth. Multi-touch or data-driven attribution would need return-visitor journeys in the
  generator, and is a gated Level 4+ extension.
- During the tracking outage (Aug 10-16), affected Meta orders become unattributed. That is the intended signature.
