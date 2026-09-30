# 0007. Reporting time zone is America/New_York

**Status:** Accepted
**Date:** 2026-09-29

## Context

Sources use different clocks: Google daily rows are Pacific dates, Meta daily rows are Eastern dates, orders and
web events are UTC, the CRM is naive Central time, and emails send at 10:00 Eastern. A business day has to be
defined once.

Platform daily totals arrive already bucketed by their account's time zone and cannot be re-bucketed.
Timestamped orders and events can be moved to any zone.

## Decision

- Store every timestamp in **UTC**. Derive the reporting date in **America/New_York**, through the dbt variable
  `reporting_timezone`, in the date logic and dimensions only.
- Google's daily rows keep their Pacific account date and are labeled as such; they are not re-bucketed.
- The semantic layer states the reporting zone, so the agent can answer or ask (Q36, "orders on November 30").

## Why Eastern

It matches Meta's account, the email send schedule, and an Eastern-headquartered U.S. brand; only Google's daily
rows need a caveat. Pacific would be equally defensible; what matters is one documented zone.

## Consequences

- Measured: 9.1% of orders fall on a different calendar day in Pacific versus Eastern time, but only 0.28% fall in a
  different month. Daily and weekly comparisons against Google's numbers carry a boundary mismatch; monthly and
  quarterly ones barely change.
- Daylight saving is handled by computing the local date from UTC, never by fixed offsets.
