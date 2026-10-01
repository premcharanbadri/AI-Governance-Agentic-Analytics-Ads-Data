"""
Compares dbt's output to the generator's ground truth.

The generator knows which click actually caused each order and which CRM record belongs to each shop customer.
This script checks that the dbt models, which only see the raw files, reach the same answers. It lives beside the
generator because it needs data/state/, which never enters the warehouse.

The expected attribution adapts to the window dbt used, so the check stays valid for any `attribution_window_days`.

Run:  python generator/check_dbt_outputs.py --data data --db dbt/lumen.duckdb
"""
import argparse
import sys

import duckdb
import numpy as np
import pandas as pd
import yaml

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="generator/config.yaml")
    ap.add_argument("--scale", type=float)          # accepted so CI can pass the same flags to every script
    ap.add_argument("--data", default="data")
    ap.add_argument("--db", default="dbt/lumen.duckdb")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    con = duckdb.connect(a.db, read_only=True)
    truth = pd.read_parquet(f"{a.data}/state/order_truth.parquet")
    att = con.execute("select * from intermediate.int_order_attribution").df()

    window = float(att["attribution_window_days"].iloc[0])
    print(f"dbt attribution window: {window:g} days\n")

    # ---- expected attribution from the ground truth --------------------------------------------------
    to = cfg["planted"]["tracking_outage"]
    w0 = pd.Timestamp(to["start"]).tz_localize("America/New_York").tz_convert("UTC").tz_localize(None)
    w1 = (pd.Timestamp(to["end"]) + pd.Timedelta(days=1)).tz_localize("America/New_York").tz_convert("UTC").tz_localize(None)
    t = truth[["order_id", "path", "campaign_id", "email_campaign_id", "platform", "click_ts", "order_ts", "net_sales"]].copy()
    lag = (t["order_ts"] - t["click_ts"]).dt.total_seconds() / 86400
    outage = (t["path"] == "ad") & (t["platform"] == "meta") & (t["click_ts"] >= w0) & (t["click_ts"] < w1)
    t["exp_status"] = np.select(
        [t["path"] == "untracked", t["path"] == "organic", outage, t["path"] == "email", (t["path"] == "ad") & (lag > window)],
        ["untracked", "no_marketing_touch", "no_marketing_touch", "attributed", "outside_window"], default="attributed")
    t["exp_campaign"] = np.where(t["exp_status"] != "attributed", None,
                                 np.where(t["path"] == "email", t["email_campaign_id"], t["campaign_id"]))
    m = t.merge(att[["order_id", "attribution_status", "attributed_campaign_id", "candidate_campaign_id", "touch_type"]], on="order_id", how="outer", indicator=True)

    # ---- attribution ---------------------------------------------------------------------------------
    check("attribution: one row per order, matching the ground truth", (m["_merge"] == "both").all(), f"{len(truth):,} orders")
    wrong = m[m["exp_status"] != m["attribution_status"]]
    check("attribution: status agrees with the ground truth for every order", wrong.empty,
          f"{len(wrong):,} mismatches" if len(wrong) else f"{len(m):,} orders agree")
    if len(wrong):
        print(wrong.groupby(["exp_status", "attribution_status"]).size().to_string(), "\n")
    att_rows = m[m["attribution_status"] == "attributed"]
    check("attribution: every attributed order is credited to the true campaign",
          (att_rows["attributed_campaign_id"] == att_rows["exp_campaign"]).all(), f"{len(att_rows):,} attributed orders")
    hit = m[m["order_id"].isin(t.loc[outage, "order_id"])]
    check("attribution: no Meta order clicked during the tracking outage is attributed",
          (hit["attribution_status"] != "attributed").all(), f"{len(hit):,} orders, ${t.loc[outage, 'net_sales'].sum():,.0f} net moved to unattributed")
    organic = m[m["order_id"].isin(t.loc[t["path"].isin(["organic", "untracked"]), "order_id"])]
    check("attribution: organic and untracked orders are never credited to a campaign",
          (organic["attribution_status"] != "attributed").all(), f"{len(organic):,} orders")

    # ---- how much the window matters (decision 0006) ---------------------------------------------------------
    paid = att[att["touch_type"] == "paid_click"]
    cap = {d: (paid["touch_lag_days"] <= d).mean() for d in (1, 3, 7, 14)}
    print("Paid-click orders captured by window: " + ", ".join(f"{d}d {v:.1%}" for d, v in cap.items()))
    rev = t.set_index("order_id")["net_sales"]
    att_rev = rev.reindex(att.loc[att["is_attributed"], "order_id"]).sum()
    print(f"Net sales attributed to a campaign (tracked period): {att_rev / rev[t.set_index('order_id')['path'] != 'untracked'].sum():.1%} of tracked net sales\n")

    # ---- identity resolution ---------------------------------------------------------------------------------------
    res = con.execute("select shop_customer_id, crm_id from intermediate.int_customers_resolved").df()
    ct = pd.read_csv(f"{a.data}/state/customer_truth.csv", usecols=["shop_customer_id", "crm_id"], dtype=str)
    j = res.merge(ct, on="shop_customer_id", suffixes=("_dbt", "_true"), how="outer", indicator=True)
    ok = (j["crm_id_dbt"].fillna("") == j["crm_id_true"].fillna(""))
    check("identity: every shop customer is matched to its true CRM record (or none)", bool(ok.all()) and (j["_merge"] == "both").all(),
          f"{len(j):,} customers, {int(j['crm_id_dbt'].notna().sum()):,} matched, {int((~ok).sum())} wrong")
    naive = con.execute("select count(*) from raw.crm_customers c left join raw.shop_customers s on s.email = c.email where s.id is null").fetchone()[0]
    total = con.execute("select count(*) from raw.crm_customers").fetchone()[0]
    check("identity: a naive join on raw email would have lost matches (why normalization exists)", naive > 0, f"{naive:,} of {total:,} CRM records ({naive / total:.1%})")

    width = max(len(n) for n, _, _ in results)
    for name, ok_, detail in results:
        print(f"{'PASS' if ok_ else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(not o for _, o, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
