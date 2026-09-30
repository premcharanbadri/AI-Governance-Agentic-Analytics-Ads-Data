# 0005. Plant causal incidents inside the simulation and delivery incidents on the raw files

**Status:** Accepted
**Date:** 2026-09-29

## Context

Pass 3 has to plant problems with known answers. They are of two different kinds, and one mechanism
cannot do both honestly.

- **Incidents that change what happened.** A competitor bids up head search terms, so a fixed budget buys
  fewer clicks, which means fewer orders and a higher CPA. Editing the ad reports afterwards would leave
  clicks, orders and spend contradicting each other.
- **Incidents that change what the files say, not what happened.** A tracking outage strips UTM tags, a field
  is renamed, events are duplicated or arrive late, Meta restates yesterday's numbers. Orders, spend and clicks
  are unchanged; only the raw records are damaged.

## Decision

- **Causal incidents live inside `history.py`:** the July CPC spike (through CPC, clicks, orders, keyword and
  auction tables), the bulk order on the clearance campaign, and the skillet orders from the small ZIP group.
  Everything downstream stays consistent because it is computed from the changed inputs.
- **Delivery incidents are applied by `incidents.py` to the raw files after Pass 2:** the tracking outage,
  duplicate and late web events, Meta's provisional-then-final versions, and the `spend` -> `amount_spent`
  rename. The ground truth (`state/`) is deliberately left untouched, so the right answer is always recoverable.
- `incidents.py` refuses to run twice (a marker file is written); re-running `history.py` clears the marker.
- The live event emitter is **not** part of Pass 3. It is built as "Pass 4" alongside Snowflake streaming
  ingestion, so it can be tested against the real sink instead of a mock.

## Alternatives considered

- **Patch everything after the fact.** Rejected for causal incidents: the CPC spike would have no effect on
  clicks or orders, and any analysis would expose the inconsistency.
- **Simulate every delivery problem inside Pass 2.** Rejected: it couples file-format quirks to the business
  simulation, and makes it impossible to run the clean-history audit before the damage is done.

## Consequences

- The same audit (`check_history.py`) can run on clean history and again after the incidents, which proves the
  incidents damage only what they should.
- Every incident check was also run against data without the incidents and fails there, so the checks detect
  absence as well as presence.
- Attribution is rebuilt from raw web events in `check_incidents.py`: about 100% of ad orders recover their
  campaign outside the outage and none of the affected Meta orders recover it inside, which is the signature
  the dbt attribution model has to reproduce.
