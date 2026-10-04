"""Reconcile a built warehouse against the signed-off reference totals (decision 0009).

The generator is deterministic only for the pinned library versions: a different numpy version changes some
random draws (seen in practice: 271 web events and one cent of Meta spend). This check makes any drift visible
instead of letting it surface later as a "wrong" benchmark answer.

  python generator/reconcile.py --scale 0.05                 # compare with the reference, exit 1 on any difference
  python generator/reconcile.py --scale 0.05 --write         # (owner only) record a new reference

Reference files live in generator/reference_totals/ and are committed. Money is summed as DECIMAL, so the totals
themselves are exact.
"""
from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ["numpy", "pandas", "Faker", "pyarrow", "duckdb", "dbt-core", "dbt-duckdb", "PyYAML"]

MONEY = {
    "staging.stg_shop__orders": ["subtotal", "discount", "shipping", "tax", "total"],
    "staging.stg_shop__refunds": ["refund_amount"],
    "staging.stg_google__ad_performance_daily": ["cost_usd", "platform_conversion_value"],
    "staging.stg_meta__insights": ["spend_usd", "platform_conversion_value"],
}


def totals(db: Path) -> dict:
    con = duckdb.connect(str(db), read_only=True)
    out = {"row_counts": {}, "money": {}, "keys": {}}
    for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw' "
                            "ORDER BY 1").fetchall():
        out["row_counts"][f"raw.{t}"] = con.execute(f"SELECT count(*) FROM raw.{t}").fetchone()[0]
    for table, cols in MONEY.items():
        for c in cols:
            v = con.execute(f"SELECT sum(CAST({c} AS DECIMAL(18,2))) FROM {table}").fetchone()[0]
            out["money"][f"{table}.{c}"] = str(v)
    item = con.execute("SELECT sum(CAST(quantity * unit_price AS DECIMAL(18,2))) "
                       "FROM staging.stg_shop__order_items").fetchone()[0]
    out["money"]["staging.stg_shop__order_items.quantity_x_unit_price"] = str(item)
    for name, sql in {
        "distinct shop customers with orders": "SELECT count(DISTINCT shop_customer_id) FROM staging.stg_shop__orders",
        "distinct orders": "SELECT count(DISTINCT order_id) FROM staging.stg_shop__orders",
        "resolved customers": "SELECT count(*) FROM intermediate.int_customers_resolved",
    }.items():
        out["keys"][name] = con.execute(sql).fetchone()[0]
    gt = ROOT / "data" / "ground_truth" / "injected_issues.jsonl"
    out["keys"]["ground-truth entries"] = len(gt.read_text().splitlines()) if gt.exists() else None
    con.close()
    return out


def versions() -> dict:
    v = {}
    for p in PACKAGES:
        try:
            v[p] = version(p)
        except PackageNotFoundError:
            v[p] = None
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="0.05")
    ap.add_argument("--db", default=str(ROOT / "dbt" / "lumen.duckdb"))
    ap.add_argument("--write", action="store_true", help="record the current build as the reference")
    a = ap.parse_args()
    ref_path = ROOT / "generator" / "reference_totals" / f"scale_{a.scale}.json"
    now = totals(Path(a.db))

    if a.write:
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(json.dumps({"scale": a.scale, "library_versions": versions(), **now}, indent=2) + "\n")
        print(f"reference written: {ref_path.relative_to(ROOT)}")
        return

    if not ref_path.exists():
        sys.exit(f"No reference at {ref_path.relative_to(ROOT)}. The data owner records one with --write.")
    ref = json.loads(ref_path.read_text())
    diffs = []
    for section in ("row_counts", "money", "keys"):
        for k in sorted(set(ref[section]) | set(now[section])):
            if ref[section].get(k) != now[section].get(k):
                diffs.append((section, k, ref[section].get(k), now[section].get(k)))
    mine, theirs = versions(), ref.get("library_versions", {})
    vdiff = {p: (theirs.get(p), mine.get(p)) for p in PACKAGES if theirs.get(p) != mine.get(p)}

    checked = sum(len(now[s]) for s in ("row_counts", "money", "keys"))
    if not diffs:
        print(f"Reconciled: all {checked} totals match the reference ({ref_path.name}).")
        if vdiff:
            print(f"Note: library versions differ from the reference but produced identical data: {vdiff}")
        return
    print(f"NOT RECONCILED: {len(diffs)} of {checked} totals differ from the reference ({ref_path.name}).")
    for section, k, r, n in diffs:
        print(f"  {section:10} {k:55} reference {r}  this build {n}")
    if vdiff:
        print("\nLibrary versions differ from the reference (reference, this build):")
        for p, (r, n) in vdiff.items():
            print(f"  {p:12} {r}  {n}")
        print("Install the pinned versions: pip install -r requirements.txt")
    sys.exit(1)


if __name__ == "__main__":
    main()
