# 0003. Start order history with a legacy customer base

**Status:** Accepted
**Date:** 2026-09-29 (found in the Pass 1-2 review)

## Context

The first version created customers from 2023-01-01, the same day order history begins. A business
doing tens of millions in sales cannot start from zero customers, and the generator showed it:
on 2023-01-03 about 600 "returning" orders came from roughly 650 existing customers, so nearly
every customer reordered every day. Customers who signed up in early 2023 ended up with 11 orders
each on average (one had 91), against about 2.4 for later cohorts. Any cohort or retention chart
would have exposed this immediately.

## Decision

Add **legacy customers**: 250,000 people who signed up between 2020-07-01 and the start of order
history, each with a number of past orders and a date of last purchase (`prior_orders`,
`prior_last_order_utc` in `state/customer_truth.csv`). Their past orders are not exported, but the
order simulation starts from this state, so early 2023 has a realistic mix of active, lapsed and
first-time buyers.

Two related fixes made in the same review:

- Repeat buying uses a fixed, bounded per-customer tendency (gamma-distributed) with no recency
  weighting. The earlier "more orders makes more orders" weighting snowballed into streaks.
- Seasonality is interpolated between mid-month values instead of jumping on the 1st of each month.

## Consequences

- Customer counts rise by the legacy amount; "how many customers do we have?" (Q17) now spans
  people who signed up as early as mid-2020.
- Email list size, and therefore email volume and platform fees, rise with it.
- Cohort behavior is comparable across sign-up years (checked in `check_history.py`).
- Legacy customers' past orders are not in the raw data: any question about lifetime value before
  2023 is unanswerable by design.
