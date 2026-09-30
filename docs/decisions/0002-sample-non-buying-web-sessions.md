# 0002. Keep every buying path; sample 10% of non-buying web sessions

**Status:** Proposed (default in config; revisit before loading to Snowflake)
**Date:** 2026-09-29
**Amended:** 2026-09-29, after an external review and a check against the generated data

## Context

At full scale, Lumen Goods has roughly 60,000 web sessions a day. Generating every event for
18 months would produce well over 100 million rows: slow to generate on a laptop, slower to develop
against, and a heavier draw on Snowflake trial credits. But orders must always link to their web
sessions, or first-party attribution breaks.

## Decision

Keep **every** session on a buying path (the ad-click visit and the purchase visit), and keep a
**10% random sample** of sessions that don't buy. Sampled rows carry `sample_weight = 10`; buying rows
carry `sample_weight = 1`. Session counts are estimated as `SUM(sample_weight)`.

Result at full scale: about 7 million events instead of 60+ million, with 100% of orders linked.

## Alternatives considered

- **Everything, unsampled.** Strongest "big data" story; costs 5-10x more time and credits.
- **Sample all sessions uniformly.** Rejected: 90% of orders would lose their sessions, breaking attribution.

## Consequences

- Attribution, conversion paths, and order-level analysis are exact.
- Session and funnel counts are estimates and must use `sample_weight`. Measured on the generated
  data: the raw row-level session conversion rate is **18.2%**, the weighted rate is **2.3%**
  (a 7.9x distortion). Anyone computing it with `COUNT(*)` gets a wildly wrong answer.
- **Which metrics are affected.** Anything with a web-session count in the numerator or
  denominator: sessions, session conversion rate, add-to-cart rate, funnel drop-off, bounce rate.
  **Not affected:** spend, ad-platform clicks and impressions, orders, revenue, CPA, ROAS. These
  come from unsampled sources. (Click-to-purchase rate is ambiguous by design: computed from
  platform clicks it needs no weights; computed from web sessions it does.)
- Mirrors real analytics tools, which sample high-volume traffic data.
- Reversible: set `history.web.non_converting_sample_rate: 1.0` for full volume.

## How the rule reaches the agent

The rule is defined **once, in the semantic layer** (Level 2): `sessions = SUM(sample_weight)` and
`session_conversion_rate = SUM(sample_weight) of buying sessions / SUM(sample_weight) of all sessions`,
plus a column description on `sample_weight` in the dbt docs. It is deliberately *not* delivered
through the agent's system prompt, because a prompt instruction is exactly the kind of guardrail
this project argues against. The Level 1 baseline is expected to get these questions wrong; the
size of that failure, and the fix at Level 2, is a measured result.

## Known limitation: no multi-session journeys for non-buyers

In the generated data every sampled non-buying session has its own `anonymous_id` (verified: all
multi-session IDs are buying paths, which are kept in full). So sampling by session and sampling by
user are equivalent here, and no non-buyer's journey is broken. This is a simplification of
real traffic, where returning visitors have several non-buying sessions.

If return-visitor journeys or multi-touch attribution are added later, switch to **user-level
sampling**: keep every session of any user who ever converts, and keep all sessions of a
deterministic hash-selected 10% of the remaining users (for example `hash(anonymous_id) % 10 = 0`),
so kept users have complete, unbroken histories.
