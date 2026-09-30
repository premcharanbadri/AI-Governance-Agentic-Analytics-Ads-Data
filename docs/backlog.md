# Backlog: decided but not yet built

Items agreed in design reviews, waiting for the level or pass where they belong.

## Pass 3 (generator)
- Put the Meta schema-change date (`spend` -> `amount_spent`, 2026-09-03) in `config.yaml` rather than in code.
- Give each ground-truth issue structured grading fields: `affected_metric`, `direction`,
  `expected_root_cause`, `affected_entities`, so diagnostic answers can be scored against the log.

## Benchmark (when the file is created)
- "How many sessions did paid social get last week?" (needs `SUM(sample_weight)`; result-matched)
- "What's the site conversion rate in August 2026?" (needs weighted sessions; result-matched)

## Level 2 (context layer)
- Small document corpus for retrieval: metric handbook, the outdated ROAS wiki page (Q38),
  attribution-window and retention policies. Written by hand or generated from the metric definitions.
- Define `sessions` and `session_conversion_rate` with sample weights in the semantic layer (ADR 0002).

## Considered and deferred
- Campaign strategy briefs and ad-copy text linked to performance: needs creative attributes to
  causally affect results in the generator. Revisit only if retrieval evaluation needs it.
- Simulated API rate limits and latency: the agent queries Snowflake through MCP, not ad APIs. Not applicable.

## Found in the Pass 1-2 review (2026-09-29)

**Decided**
- **As-of date: 2026-09-30** (decision 0004). Build the frozen `BENCHMARK` clone from the full backfill,
  plus a `date <= as_of` dbt variable as a safeguard.

**Benchmark wording**
- **Q9** (pacing): reword to "As of September 15, which campaigns were on pace to overspend September's budget?" September is over at the new as-of date.
- Keep September out of headline questions: its last days and refunds are provisional. Use it for Q24, Q29, Q31, Q40.
- Q3 2026 questions are now complete-quarter questions.
- **Q23** (July vs. August): the four paused campaigns explain roughly a third of the decline; scheduled
  campaign expiries and the end of the Summer Sale explain the rest. Reword to "Which campaigns stopped
  early in August, and what did it cost?" and score against the measured decomposition in the
  ground-truth log (`q23_decomposition`), not against "the pauses explain most of it".
- **Q30**: use the measured overcount ratios in the log (`platform_overcount`) as the rubric.

**Pass 3: done** (decision 0005)
- Planted and verified: tracking outage (Q21, Q32, Q34), CPC spike (Q22), bulk order (Q33), small-cell skillet
  orders (Q49), Meta rename (Q39), Meta restatements (Q24, Q29), duplicate and late web events.
- Wording to fix in the benchmark: **Q39** should read "Meta spend looks missing since early September. Why?"
  because the rename persists from Sept 3 (it also hits restatements of Aug 31 to Sep 2). **Q22**: the Summer
  Sale (Jul 6-19) lifts conversion and partly masks the CPA effect, so score the diagnosis (head-term CPC up,
  CTR and conversion steady, impression share down), not just "CPA rose".

**Pass 4: live emitter (built with the Snowflake streaming step, so it can be tested against the real sink)**
- Replay a day of web events and orders in near-real time into a separate live table, so the benchmark
  backfill is never polluted.
- Emit the saved `state/pending_refunds.parquet` and `state/pending_order_intents.parquet` as they "arrive",
  so September's refund rate rises toward normal (restatement, Q31).

**Known limitations (documented, not fixed)**
- The first week of April 2025 has about 10% fewer orders: clicks before ads_start do not exist, so
  their delayed orders are missing. Use click date, not order date, for early-April comparisons.
- Black Friday shows a plateau from Nov 25 to Dec 8 rather than a sharp Cyber Monday peak.
- Video ROAS is 0.22, which makes "cut video" trivially right. Consider adding channel-specific
  platform over-reporting (YouTube reports view-through conversions) to make Q30 richer, and raising
  video's conversion rate if a less obvious channel decision is wanted.
- Internal test campaigns now have spend and clicks but no attributed orders.
