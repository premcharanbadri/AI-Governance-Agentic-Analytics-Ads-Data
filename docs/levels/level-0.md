# Level 0: data foundation

**Status:** in progress
**Measured by:** data quality tests and reconciliation (dbt), 108 generator checks in CI
**Decisions:** 0001-0007 in docs/decisions/
**Tag:** level-0 (not yet created; generator-v1 marks the finished generator)

## Progress

| Piece | Status |
|---|---|
| Synthetic data generator (Passes 1-3) and its 108 checks | Done |
| Loader into DuckDB (stand-in for Snowflake RAW) | Done |
| dbt staging layer: 22 models, 103 tests, runs in CI | Done |
| dbt intermediate models: identity resolution, attribution | Next |
| dbt marts (star schema) and reconciliation tests | Planned |
| Benchmark (about 60 questions, gold SQL, held-out set) | Planned |
| Docker packaging and orchestration | Planned |
| Snowflake ingestion (batch, then streaming) and frozen benchmark clone | Planned |
