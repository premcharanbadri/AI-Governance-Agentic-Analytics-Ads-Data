"""
Automated audit of Pass 2 (daily history).

Three kinds of checks:
  1. Targets      - calibrated totals land where the config says (spend, ROAS, volumes).
  2. Consistency  - the same fact reported by different systems agrees (ad rows sum to campaign
                    totals, keyword rows sum to campaign totals, order math adds up).
  3. Business logic - things that must be true in the real world (no order before sign-up,
                    a customer's first order is "new", refunds come after orders, paused
                    campaigns stop spending, clicks come before purchases).

Run:  python generator/check_history.py --scale 0.1 --data data
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import yaml

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="generator/config.yaml")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if a.scale is not None:
        cfg["scale"] = a.scale
    d, h = a.data, cfg["history"]
    ads_start = pd.Timestamp(cfg["dates"]["ads_start"])
    extract_end = pd.Timestamp(cfg["dates"]["extract_date"]) + pd.Timedelta(days=1)

    cd = pd.read_parquet(f"{d}/state/campaign_days.parquet")
    truth = pd.read_parquet(f"{d}/state/order_truth.parquet")
    master = pd.read_csv(f"{d}/state/campaign_master.csv", parse_dates=["effective_end_date"])
    master["campaign_id"] = master["campaign_id"].astype(str)
    orders = pd.read_parquet(f"{d}/raw/shop/orders/backfill")
    items = pd.read_parquet(f"{d}/raw/shop/order_items/backfill")
    refunds = pd.read_parquet(f"{d}/raw/shop/refunds/backfill")
    cust = pd.read_csv(f"{d}/state/customer_truth.csv", parse_dates=["created_at_utc", "prior_last_order_utc"])

    # ---- 1. Targets ----------------------------------------------------------
    months = cd["local_date"].dt.to_period("M").nunique()
    monthly = cd["spend"].sum() / months
    target = cfg["business"]["monthly_ad_budget"] * cfg["scale"]
    check("target: avg monthly spend within 3% of budget", abs(monthly / target - 1) < 0.03, f"${monthly:,.0f}/month")
    roas = truth.loc[truth["path"] == "ad", "net_sales"].sum() / cd["spend"].sum()
    check("target: blended ROAS within 8% of target", abs(roas / h["target_roas"] - 1) < 0.08, f"{roas:.2f}")
    tracked = truth[truth["order_ts"] >= ads_start]
    new_share = (tracked["customer_type"] == "new").mean()
    check("target: new-customer share 25-45% of orders", 0.25 <= new_share <= 0.45, f"{new_share:.0%}")
    ref_rate = len(refunds) / len(orders)
    check("target: refund rate near config", abs(ref_rate - h["refunds"]["rate"]) < 0.01, f"{ref_rate:.1%}")

    # ---- 2. Consistency --------------------------------------------------------
    g = pd.concat([pd.read_csv(f, dtype={"campaign_id": str}) for f in glob.glob(f"{d}/raw/google_ads/ad_perf_daily/*.csv")])
    # Meta: take the latest extract of each ad-day (the provisional file, then the restated one), and accept
    # either spelling of the spend field. Works before and after Pass 3 is applied.
    incidents = os.path.exists(f"{d}/state/incidents_applied.json")
    mrows = []
    for f in glob.glob(f"{d}/raw/meta_ads/insights/*.jsonl") + glob.glob(f"{d}/raw/meta_ads/insights_restated/*.jsonl"):
        for line in open(f):
            r = json.loads(line)
            spend = r["spend"] if "spend" in r else r["amount_spent"]
            mrows.append((r["date_start"], r["campaign_id"], r["ad_id"], r.get("extracted_at", ""), int(r["impressions"]),
                          int(r["link_clicks"]), round(float(spend) * 1e6)))
    m = pd.DataFrame(mrows, columns=["date", "campaign_id", "ad_id", "extracted_at", "impressions", "clicks", "cost_micros"])
    m = m.sort_values("extracted_at").drop_duplicates(["date", "ad_id"], keep="last").drop(columns=["ad_id", "extracted_at"])
    meta_ids = set(m["campaign_id"])
    ad_level = pd.concat([g[["date", "campaign_id", "impressions", "clicks", "cost_micros"]], m]).assign(n_ads=1)
    ad_level = ad_level.groupby(["date", "campaign_id"]).sum().reset_index()
    camp = cd.assign(date=cd["local_date"].dt.strftime("%Y-%m-%d"), cost_micros=(cd["spend"] * 1e6).round())
    joined = camp.merge(ad_level, on=["date", "campaign_id"], suffixes=("_c", "_a"), how="outer", indicator=True)
    check("consistency: every campaign-day has ad rows and vice versa", (joined["_merge"] == "both").all())
    check("consistency: ad clicks sum to campaign clicks", (joined["clicks_c"] == joined["clicks_a"]).all())
    check("consistency: ad impressions sum to campaign impressions",
          (joined["impressions_c"] == joined["impressions_a"]).all())
    # Meta rounds spend to the cent on every ad row, so sums can drift by up to half a cent per ad.
    diff = (joined["cost_micros_c"] - joined["cost_micros_a"]).abs()
    tol = joined["n_ads"] * 5_000 + 1
    if incidents:   # Meta's last days are still provisional (spend up to 1.5% low)
        days = cfg["planted"]["meta_restatement"]["days"]
        late = (pd.to_datetime(joined["date"]) > pd.Timestamp(cfg["dates"]["extract_date"]) - pd.Timedelta(days=days)) \
            & joined["campaign_id"].isin(meta_ids)
        tol = np.where(late, joined["cost_micros_c"] * 0.016 + tol, tol)
    check("consistency: ad cost sums to campaign spend (rounding; Meta's latest days provisional)",
          (diff <= tol).all(), f"max diff {diff.max() / 1e6:.2f} USD")

    kw = pd.concat([pd.read_csv(f) for f in glob.glob(f"{d}/raw/google_ads/keyword_perf_daily/*.csv")])
    kwmap = pd.read_csv(f"{d}/raw/google_ads/keywords.csv").merge(
        pd.read_csv(f"{d}/raw/google_ads/ad_groups.csv")[["ad_group_id", "campaign_id"]], on="ad_group_id")
    kw = kw.merge(kwmap[["keyword_id", "campaign_id"]], on="keyword_id")
    kw["campaign_id"] = kw["campaign_id"].astype(str)
    kwsum = kw.groupby(["date", "campaign_id"])[["clicks", "cost_micros"]].sum().reset_index()
    search = camp[camp["channel"] == "SEARCH"].merge(kwsum, on=["date", "campaign_id"], suffixes=("_c", "_k"))
    check("consistency: keyword rows cover every search campaign-day",
          len(search) == (camp["channel"] == "SEARCH").sum(), f"{len(search)} of {(camp['channel'] == 'SEARCH').sum()}")
    check("consistency: keyword clicks sum to campaign clicks", (search["clicks_c"] == search["clicks_k"]).all())

    sub = items.assign(line=items["quantity"] * items["unit_price"]).groupby("order_id")["line"].sum()
    o = orders.set_index("order_id")
    check("consistency: subtotal = sum of order items", np.allclose(o["subtotal"], sub.reindex(o.index), atol=0.011))
    recomputed = o["subtotal"] - o["discount"] + o["shipping"] + o["tax"]
    check("consistency: total = subtotal - discount + shipping + tax", np.allclose(o["total"], recomputed, atol=0.011))
    check("consistency: shop orders match ground truth 1:1",
          len(orders) == len(truth) and set(orders["order_id"]) == set(truth["order_id"]))

    # ---- 3. Business logic -------------------------------------------------------
    t = truth.merge(cust[["shop_customer_id", "created_at_utc", "prior_orders", "prior_last_order_utc"]], on="shop_customer_id")
    check("logic: no order before the customer's sign-up", (t["order_ts"] >= t["created_at_utc"]).all())
    t = t.sort_values(["shop_customer_id", "order_ts"])
    first = ~t.duplicated("shop_customer_id")
    fresh = t["prior_orders"] == 0
    check("logic: a first-time customer's first order is typed 'new'", (t.loc[first & fresh, "customer_type"] == "new").all())
    check("logic: customers with past orders are never typed 'new'", (t.loc[~fresh, "customer_type"] != "new").all())
    check("logic: every 'new' order is a customer's first", first[t["customer_type"] == "new"].all())
    prev = t.groupby("shop_customer_id")["order_ts"].shift().fillna(t["prior_last_order_utc"])
    gap = (t["order_ts"] - prev).dt.total_seconds() / 86400
    lapse = h["lapsed_after_days"]
    lap_ok = (gap[t["customer_type"] == "lapsed"] > lapse - 1).mean()
    ret_ok = (gap[t["customer_type"] == "returning"] <= lapse + 1).mean()
    check("logic: 'lapsed' orders come after a long gap", lap_ok > 0.99, f"{lap_ok:.1%}")
    check("logic: 'returning' orders come after a short gap", ret_ok > 0.99, f"{ret_ok:.1%}")

    # Warm start: early cohorts must not be over-worked because the customer pool started small.
    n_ord = t.groupby("shop_customer_id").size()
    new_c = cust[(cust["prior_orders"] == 0) & (cust["created_at_utc"] >= pd.Timestamp(cfg["dates"]["order_history_start"]))].copy()
    new_c["n"] = new_c["shop_customer_id"].map(n_ord).fillna(0)
    new_c["years"] = (extract_end - new_c["created_at_utc"]).dt.total_seconds() / (365.25 * 86400)
    rate = lambda a, b: (new_c[(new_c["created_at_utc"] >= a) & (new_c["created_at_utc"] < b)]["n"].sum()
                         / new_c[(new_c["created_at_utc"] >= a) & (new_c["created_at_utc"] < b)]["years"].sum())
    early, later = rate("2023-01-01", "2023-04-01"), rate("2024-01-01", "2024-07-01")
    check("cold start: 2023Q1 cohort orders at a similar rate to 2024H1 cohort (ratio < 1.5)", early / later < 1.5,
          f"{early:.2f} vs {later:.2f} orders per customer-year")
    # A few super-fans are realistic; a fat tail is not. (Before the review fixes, 0.036% of customers had 50+ orders.)
    extreme = (n_ord >= 40).sum() / len(cust)
    check("cold start: customers with 40+ orders are rarer than 0.01%", extreme < 0.0001,
          f"{(n_ord >= 40).sum()} customers ({extreme:.4%}); max {n_ord.max()}")

    # Seasonality must ramp, not jump: 7-day average of organic orders either side of each month boundary.
    org = truth[truth["path"] == "organic"].groupby(truth["order_ts"].dt.normalize()).size()
    worst = 0.0
    for b in ["2025-10-01", "2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01"]:
        b = pd.Timestamp(b)
        before = org.loc[b - pd.Timedelta(days=7): b - pd.Timedelta(days=1)].mean()
        after = org.loc[b: b + pd.Timedelta(days=6)].mean()
        worst = max(worst, abs(after / before - 1))
    check("logic: organic demand has no cliffs at month boundaries (max step <= 25%)", worst <= 0.25, f"largest step {worst:.0%}")

    ss = truth[(truth["path"] == "ad") & (truth["same_session"] == True)]
    gap_min = (ss["order_ts"] - ss["click_ts"]).dt.total_seconds() / 60
    check("logic: same-session purchases happen within 70 minutes of the click", gap_min.max() <= 70,
          f"max {gap_min.max():.0f} min over {len(ss):,} orders")
    test_ids = master.loc[master["kind"] == "test", "campaign_id"]
    check("logic: internal test campaigns have no attributed orders", not truth["campaign_id"].isin(test_ids).any())
    pr = pd.read_parquet(f"{d}/state/pending_refunds.parquet")
    pi = pd.read_parquet(f"{d}/state/pending_order_intents.parquet")
    check("pending: refunds and orders past the extract are saved for the live stream",
          len(pr) > 0 and len(pi) > 0 and (pr["refund_ts"] >= extract_end).all() and (pi["order_ts"] >= extract_end).all(),
          f"{len(pr):,} refunds, {len(pi):,} order intents")
    log = [json.loads(l) for l in open(f"{d}/ground_truth/injected_issues.jsonl")]
    check("ground truth: Pass 2 entries recorded, IDs still unique",
          sum(r.get("pass") == 2 for r in log) >= 5 and len({r["issue_id"] for r in log}) == len(log), f"{len(log)} entries")
    check("logic: no orders after the extract date", (orders["order_ts"] < extract_end).all())
    rj = refunds.merge(orders[["order_id", "order_ts", "total"]], on="order_id")
    check("logic: refunds after order, within the return window",
          ((rj["refund_ts"] > rj["order_ts"]) & (rj["refund_ts"] - rj["order_ts"] <= pd.Timedelta(days=h["refunds"]["max_days"]))).all())
    check("logic: refund never exceeds order total", (rj["amount"] <= rj["total"] + 0.001).all())
    ad = truth[truth["path"] == "ad"]
    check("logic: ad clicks come before purchases", (ad["click_ts"] < ad["order_ts"]).all())
    paused = master[master["paused"]]
    after = cd.merge(paused[["campaign_id", "effective_end_date"]], on="campaign_id")
    check("logic: paused campaigns stop spending", (after["local_date"] <= after["effective_end_date"]).all(),
          f"{len(paused)} paused campaigns")
    check("logic: daily spend never exceeds budget", (cd["spend"] <= cd["daily_budget"] + 0.01).all())

    gconv = g["platform_conversions"].sum()
    fp = truth[(truth["path"] == "ad") & (truth["platform"] == "google")].shape[0]
    check("logic: Google claims more conversions than first-party", gconv > fp, f"{gconv / fp:.2f}x")

    # ---- 4. Web events (read only the columns needed) -------------------------
    ev = ds.dataset(f"{d}/raw/web_events/backfill", format="parquet", partitioning="hive")
    buys = ev.to_table(columns=["event_id", "session_id", "customer_id", "event_ts", "sample_weight"],
                       filter=ds.field("event_type") == "purchase").to_pandas().drop_duplicates("event_id")   # retries repeat event_id
    check("web: one purchase event per tracked order", len(buys) == (truth["path"] != "untracked").sum(),
          f"{len(buys):,} purchases")
    check("web: buying sessions are unsampled (weight 1)", (buys["sample_weight"] == 1.0).all())
    tracked_ids = truth.loc[truth["path"] != "untracked", "order_id"]
    linked = orders.loc[orders["order_id"].isin(tracked_ids), "session_id"].notna().mean()
    check("web: every tracked order links to a session", linked == 1.0, f"{linked:.2%}")
    check("web: tracking starts exactly at ads_start (UTC)",
          (truth.loc[truth["path"] == "untracked", "order_ts"] < ads_start).all()
          and (truth.loc[truth["path"] != "untracked", "order_ts"] >= ads_start).all())
    land = ev.to_table(columns=["gclid", "fbclid", "utm_source"], filter=ds.field("event_type") == "landing").to_pandas()
    g_ok = land.loc[land["utm_source"] == "google", "gclid"].notna().all()
    f_ok = land.loc[land["utm_source"] == "facebook", "fbclid"].notna().all()
    check("web: paid landings carry the right click ID", g_ok and f_ok)
    weights = ev.to_table(columns=["sample_weight"]).to_pandas()["sample_weight"]
    rate = h["web"]["non_converting_sample_rate"]
    check("web: sampled sessions carry weight 1/rate", set(weights.unique()) <= {1.0, 1.0 / rate})

    # ---- Report -------------------------------------------------------------------
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
