# 0001. Use a synthetic data generator instead of public ad datasets

**Status:** Accepted
**Date:** 2026-09-28

## Context

The project needs advertising data that connects spend to revenue across several channels, so the
benchmark can ask realistic business questions (ROAS by channel, acquisition cost, budget pacing).
It also needs known ground truth for later evaluation, including planted anomalies whose causes
are known in advance.

Real ad-platform APIs only return data for accounts that are actively spending money. Public
datasets cover only pieces of the picture:

- Criteo attribution and click-log data: real impressions and clicks, but anonymized, with no
  campaign structure or channels.
- Avazu click-through data: real clicks, useful for click-rate patterns only.
- Google Analytics sample e-commerce data: real site and purchase behavior, but no ad spend.

None links spend to revenue across channels, and none has known root causes for anomalies.

## Decision

Build a deterministic Python generator for a fictional company (Lumen Goods) that imitates real
source systems closely: a Google-style ads export (money in micros, Pacific time), a Meta-style
export (nested JSON, amounts as strings, Eastern time), a shop, a CRM with its own IDs and
timestamp quirks, an email platform, and a Finance budget spreadsheet. Planted problems are
recorded in a ground-truth log the AI system never sees.

## Alternatives considered

- **Public datasets only.** Rejected: can't answer spend-to-revenue questions, no ground truth.
- **Public datasets stitched together.** Rejected: the pieces describe different businesses, so
  joins between them would be meaningless.
- **Real ad account with a small budget.** Rejected: costly, tiny volume, and no control over events.

## Consequences

- Full control over volume, realism, and planted issues; every benchmark answer is verifiable.
- Same seed and config always produce identical data, so benchmark answers never drift.
- Realism depends on the generator's assumptions. These live in `generator/config.yaml` and can be
  calibrated against published industry benchmarks or public datasets.
- The generator itself becomes a tested component (see `generator/check_reference.py`).
