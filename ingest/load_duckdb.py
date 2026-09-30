"""
Loads the raw landing files into a DuckDB `raw` schema.

This stands in for the Snowflake RAW database: one table per landed dataset, no cleaning.
  - CSV columns stay as text (casting is dbt's job, in the staging layer)
  - JSON and Parquet keep their native types, including Meta's nested arrays
  - files with different columns are unioned by name, so a renamed field shows up as a
    new column that is NULL in older rows (exactly how schema drift looks in a warehouse)
  - every row records the file it came from (`filename`) and when it was loaded (`_loaded_at`)

Only data/raw is read. data/state and data/ground_truth never enter the warehouse.

Run:  python ingest/load_duckdb.py --data data --db dbt/lumen.duckdb
"""
import argparse
from pathlib import Path

import duckdb


def csv(*patterns):
    return f"read_csv({list(patterns)}, all_varchar=true, header=true, union_by_name=true, filename=true)"


def jsonl(*patterns):
    return f"read_json({list(patterns)}, format='newline_delimited', union_by_name=true, filename=true)"


def parquet(*patterns):
    return f"read_parquet({list(patterns)}, union_by_name=true, filename=true)"


def tables(raw: Path):
    r = lambda p: str(raw / p)
    return {
        "catalog_products": csv(r("catalog/products.csv")),
        "crm_customers": csv(r("crm/customers.csv")),
        "email_campaigns": csv(r("email_platform/campaigns.csv")),
        "email_daily_metrics": csv(r("email_platform/daily_metrics/*.csv")),
        "email_platform_fees": csv(r("email_platform/platform_fees.csv")),
        "finance_budget_plan": csv(r("finance/budget_plan.csv")),
        "google_campaigns": csv(r("google_ads/campaigns.csv")),
        "google_ad_groups": csv(r("google_ads/ad_groups.csv")),
        "google_ads": csv(r("google_ads/ads.csv")),
        "google_keywords": csv(r("google_ads/keywords.csv")),
        "google_ad_perf_daily": csv(r("google_ads/ad_perf_daily/*.csv")),
        "google_keyword_perf_daily": csv(r("google_ads/keyword_perf_daily/*.csv")),
        "google_auction_insights_daily": csv(r("google_ads/auction_insights_daily/*.csv")),
        "meta_campaigns": jsonl(r("meta_ads/campaigns.jsonl")),
        "meta_adsets": jsonl(r("meta_ads/adsets.jsonl")),
        "meta_ads": jsonl(r("meta_ads/ads.jsonl")),
        # first extracts and restated finals are loaded together; dbt keeps the latest per ad-day
        "meta_insights": jsonl(r("meta_ads/insights/*.jsonl"), r("meta_ads/insights_restated/*.jsonl")),
        "shop_customers": jsonl(r("shop/customers.jsonl")),
        "shop_orders": parquet(r("shop/orders/backfill/*.parquet")),
        "shop_order_items": parquet(r("shop/order_items/backfill/*.parquet")),
        "shop_refunds": parquet(r("shop/refunds/backfill/*.parquet")),
        "web_events": parquet(r("web_events/backfill/*/*.parquet")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--db", default="dbt/lumen.duckdb")
    a = ap.parse_args()
    raw = Path(a.data) / "raw"
    if not raw.exists():
        raise SystemExit(f"{raw} not found. Run the generator first.")
    con = duckdb.connect(a.db)
    con.execute("create schema if not exists raw")
    total = 0
    for name, source in tables(raw).items():
        con.execute(f"create or replace table raw.{name} as select *, current_timestamp as _loaded_at from {source}")
        n = con.execute(f"select count(*) from raw.{name}").fetchone()[0]
        total += n
        print(f"  raw.{name:<30} {n:>12,} rows")
    con.close()
    print(f"loaded {total:,} rows into {a.db}")


if __name__ == "__main__":
    main()
