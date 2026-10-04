# Governed Analytics Agent

An AI analytics assistant that answers business questions from warehouse data, grounded in governed
metric definitions and enforced permissions, and measured at every layer of complexity.

**The problem:** AI assistants that answer questions from company data often give wrong answers,
not because they can't write SQL, but because they don't know what "revenue" or "active customer"
means at that company, or what data they're allowed to use. This project builds the layers that fix
that, one level at a time, and measures what each layer actually improves.

## The scenario

Lumen Goods is a fictional U.S. direct-to-consumer home and kitchen brand (~$40M annual revenue)
spending ~$1.5M/month on paid search, shopping, video, display, paid social, and email. Its data
arrives from separate systems with realistic quirks: different IDs, naming conventions, time zones,
money formats, late and duplicate data, and restatements.

## Roadmap

| Level | What gets added | How it's measured |
|---|---|---|
| 0 | Data foundation: generator, Snowflake ingestion (batch + streaming), dbt models and tests, benchmark | Data quality tests, reconciliation |
| 1 | Naive text-to-SQL baseline + infrastructure skeleton (tracing, CI evals, read-only role) | Execution accuracy vs. gold answers |
| 2 | Context layer: semantic layer, metric definitions, glossary | Accuracy gain over baseline; ambiguity handling |
| 3 | MCP server + tool-using agent + policy gateway | Accuracy, tool-use quality, red-team suite |
| 4+ | Built only if evaluations justify them: multi-agent diagnostics, context ranking, second domain | Planted-anomaly diagnosis, definition selection |

**Current status:** Level 0 — the data generator is complete (Passes 1–3, 108 checks) and dbt's staging and intermediate layers are built and verified against the ground truth (122 dbt nodes, identity resolution and attribution). Next: dbt marts (star schema), the benchmark, then Snowflake ingestion and the live stream. Open design items are in [docs/backlog.md](docs/backlog.md).

**Governance preview:** a working early version of the Level 3 controls (policy gateway, MCP server, local Qwen agent, control tests and a small red-team set) is in [governance/](governance/README.md). It tests whether the controls work; it does not replace the benchmark ladder ([decision 0008](docs/decisions/0008-governance-preview-before-benchmark.md)).

## Repository layout

```text
generator/          synthetic data generator, config, and audit checks
  common.py           helpers shared by the passes (smoothed seasonality)
  reference.py        Pass 1: products, campaigns, customers, email sends, budget plan
  history.py          Pass 2: ad performance, orders, refunds, email metrics, web events, plus the incidents that
                      change what happened (CPC spike, bulk order, small-cell orders)
  incidents.py        Pass 3: incidents that damage the raw files (tracking outage, duplicate/late events,
                      Meta restatements and field rename)
  check_*.py          automated audits for each pass (run in CI)
data/               generated output (gitignored; reproducible from code + config)
  raw/              what each source system sends — the only data loaded into the warehouse
  state/            generator-internal records for later passes (never loaded)
  ground_truth/     log of planted issues, used only for scoring (never loaded)
ingest/             loads data/raw into a DuckDB `raw` schema (stand-in for Snowflake RAW)
dbt/                dbt project: staging, intermediate and mart models, tests, docs (DuckDB now, Snowflake later)
governance/         Level 3 preview: policy gateway, MCP server, Qwen agent, control tests, red-team prompts
docs/decisions/     short records of key design decisions (0001–0009)
docs/diagrams/      architecture diagrams as Mermaid code
docs/backlog.md     agreed but not-yet-built items
.github/workflows/  CI: regenerate at 5% scale and run audit checks on every push
```

## Quick start

```bash
pip install -r requirements.txt

# Fast development run (5% scale, about a minute end to end)
python generator/reference.py       --scale 0.05 --out data
python generator/check_reference.py --scale 0.05 --data data
python generator/history.py         --scale 0.05 --out data
python generator/check_history.py   --scale 0.05 --data data     # clean history
python generator/incidents.py       --scale 0.05 --out data
python generator/check_history.py   --scale 0.05 --data data     # same audit, after the incidents
python generator/check_incidents.py --scale 0.05 --data data

# Full scale (about 6 minutes): drop --scale from every command

# Load the raw files into DuckDB and build the dbt project
python ingest/load_duckdb.py --data data --db dbt/lumen.duckdb
cd dbt && dbt build --profiles-dir .
cd .. && python generator/check_dbt_outputs.py --data data --db dbt/lumen.duckdb   # dbt vs. ground truth
```

DuckDB bakes the database file's name into its views, so rebuild the warehouse instead of renaming or copying the file under a new name.

Run the steps in this order with the same `--scale`. `history.py` stops if Pass 1 used a different scale, and
`incidents.py` refuses to run twice (re-run `history.py` to start from clean files).

## What the data looks like at full scale

| Measure | Value |
|---|---|
| Media spend | ~$1.5M/month across 5 channels, 235 campaigns |
| Customers | 850K: 250K legacy (signed up 2020–2022, with past orders) + 600K signing up 2023–2026; see decision 0003 |
| Orders | ~1.80M since Jan 2023; ~1,500/day since tracking began |
| Net sales | $34.5M (2023), $39.6M (2024), $44.5M (2025) |
| Blended first-party ROAS | ~1.7 (Shopping ~2.8, Search ~2.0, Social ~1.3, Display ~1.1, Video ~0.2) |
| Web events | ~7.2M (every buying path, plus 10% of other sessions; see decision 0002) |
| Customer mix (tracked orders) | 33% new, 52% returning, 15% lapsed |

## Planted incidents

Each has a known cause, recorded in `ground_truth/injected_issues.jsonl` with measured numbers (never loaded
into the warehouse). See decision 0005 for why they are planted in two different ways.

| Incident | Where | Signature | Questions |
|---|---|---|---|
| Tracking outage, Aug 10–16 | web events | Paid-social landings lose UTM tags and click IDs; spend, clicks and Meta-reported conversions are normal; first-party attribution of those orders is lost | 21, 32, 34 |
| Competitor CPC spike, July | Google keywords, auctions | Head-term CPC ~3.5x; clicks fall on fixed budgets; CTR and conversion per click steady; impression share falls | 22 |
| Early pauses, Aug 4 | both ad platforms | Four large campaigns stop early; they explain only part of the July→August decline | 23 |
| Bulk order on a tiny campaign | orders, Google | One ~$1.7K order on ~$230 of spend gives an extreme ROAS | 33 |
| Small-cell skillet orders, Sep 2 | orders, CRM | 3 of the 6 customers in one ZIP buy the same product the same day | 49 |
| Meta field rename, Sep 3 | Meta insights | `spend` becomes `amount_spent`, including restatements of earlier days | 39 |
| Meta restatements | Meta insights | First extract understates conversions; finals arrive 3 days later; latest 3 days stay provisional | 24, 29 |
| Duplicate and late events | web events | ~0.5% retry duplicates (same `event_id`) and ~2% late arrivals | pipeline tests |

Same seed + same config + the pinned library versions in `requirements.txt` = identical output. `python generator/reconcile.py` checks a build against the signed-off reference totals ([decision 0009](docs/decisions/0009-pin-versions-and-reconcile-data.md)).

## Level pages and tags

| Level | Status | Page | Tag |
|---|---|---|---|
| 0 Data foundation | In progress | [level-0](docs/levels/level-0.md) | generator-v1 |
| 1 Naive baseline | Planned | none yet | none yet |
| 2 Context layer | Planned | none yet | none yet |
| 3 MCP + governance | Preview built ([governance/](governance/README.md)); benchmark not yet run | none yet | none yet |
| 4+ Gated extensions | Planned | none yet | none yet |
