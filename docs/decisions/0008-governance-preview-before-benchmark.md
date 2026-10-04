# 0008. Build a governance preview before the Level 0 benchmark

**Status:** Proposed (owner to accept)
**Date:** 2026-10-04

## Context

The roadmap builds governance at Level 3, after the marts, the benchmark, the naive baseline and the context
layer. The controls themselves (read-only access, PII blocking, small-cell suppression, human approval for writes,
an audit log) do not depend on the benchmark: whether a control stops a planted violation can be tested on the
staging and intermediate models that already exist.

## Decision

- Build a working preview of the Level 3 controls in `governance/` now, on DuckDB, with a local Qwen model
  (Ollama) as the agent, so nothing leaves the laptop.
- Test each control two ways: directly, without an LLM (`control_tests.py`), and through the agent with a small
  set of legitimate questions and attacks (`run_redteam.py`). Every attack test is paired with legitimate questions,
  so over-blocking is measured alongside blocking.
- Use four **draft** metric definitions only. The certified semantic layer is still Level 2 work.

## Consequences

- The benchmark accuracy ladder (Levels 1 to 3) is unchanged and still to come. This preview reports control
  effectiveness, not accuracy gains.
- PII blocking and masking are enforced in gateway code on DuckDB. They move into database masking policies and
  role grants when the project moves to Snowflake.
- The 15 agent prompts are examples, not rates. Block and over-block rates come from the full red-team suite at
  Level 3.
