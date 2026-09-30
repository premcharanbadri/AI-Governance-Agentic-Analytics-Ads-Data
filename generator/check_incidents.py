"""
Automated audit of Pass 3 (planted incidents).

For each incident this verifies three things:
  - it is present, with the signature described in the ground-truth log,
  - it is *isolated* (nothing outside its window or scope was damaged),
  - the rest of the data still supports the right answer (for example, attribution can be rebuilt
    from raw web events everywhere except where the tracking outage broke it).

Run:  python generator/check_incidents.py --scale 0.1 --data data
"""
import argparse
import glob
import json
import sys

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import yaml

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def eastern_window_utc(start, end):
    a = pd.Timestamp(start).tz_localize("America/New_York").tz_convert("UTC").tz_localize(None)
    b = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize("America/New_York").tz_convert("UTC").tz_localize(None)
    return a, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="generator/config.yaml")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if a.scale is not None:
        cfg["scale"] = a.scale
    d, pl = a.data, cfg["planted"]
    truth = pd.read_parquet(f"{d}/state/order_truth.parquet")
    cd = pd.read_parquet(f"{d}/state/campaign_days.parquet")
    ev = ds.dataset(f"{d}/raw/web_events/backfill", format="parquet", partitioning="hive")

    # ---- 1. Tracking outage -------------------------------------------------------------
    to = pl["tracking_outage"]
    w0, w1 = eastern_window_utc(to["start"], to["end"])
    tagged = ev.to_table(columns=["event_id", "event_ts", "utm_source", "fbclid"],
                         filter=(ds.field("event_type") == "landing") & (ds.field("fbclid").is_valid() | (ds.field("utm_source") == "facebook"))
                         ).to_pandas().drop_duplicates("event_id")
    inside = tagged[(tagged["event_ts"] >= w0) & (tagged["event_ts"] < w1)]
    check("outage: no tagged paid-social landings inside the window", len(inside) == 0, f"{len(inside)} found")
    days_out = tagged[(tagged["event_ts"] < w0) | (tagged["event_ts"] >= w1)]["event_ts"].dt.date
    aug = pd.date_range("2026-08-01", "2026-08-31").date
    window_days = set(pd.date_range(to["start"], to["end"]).date)
    missing = [x for x in aug if x not in window_days and x not in set(days_out)]
    check("outage: tagged landings exist on every other day of August", not missing, f"{len(missing)} empty days")

    # Rebuild each ad order's campaign from raw events, the way the attribution model will.
    buys = ev.to_table(columns=["event_id", "anonymous_id", "customer_id", "event_ts"], filter=ds.field("event_type") == "purchase"
                       ).to_pandas().drop_duplicates("event_id")
    lands = ev.to_table(columns=["event_id", "anonymous_id", "event_ts", "utm_campaign"],
                        filter=(ds.field("event_type") == "landing") & (ds.field("gclid").is_valid() | ds.field("fbclid").is_valid())
                        ).to_pandas().drop_duplicates("event_id")
    adt = truth[truth["platform"].isin(["google", "meta"])][["shop_customer_id", "order_ts", "campaign_id", "platform", "click_ts"]]
    adt = adt.merge(buys.rename(columns={"customer_id": "shop_customer_id", "event_ts": "order_ts"})[["shop_customer_id", "order_ts", "anonymous_id"]],
                    on=["shop_customer_id", "order_ts"], how="left")
    rec = adt.merge(lands.rename(columns={"event_ts": "land_ts"})[["anonymous_id", "land_ts", "utm_campaign"]], on="anonymous_id", how="left")
    rec = rec[rec["land_ts"].isna() | (rec["land_ts"] <= rec["order_ts"])]
    rec["ok"] = rec["utm_campaign"] == rec["campaign_id"]
    per = rec.groupby(["shop_customer_id", "order_ts", "platform", "click_ts"])["ok"].max().reset_index()
    per["in_window"] = (per["click_ts"] >= w0) & (per["click_ts"] < w1)
    g_rate = per[per["platform"] == "google"]["ok"].mean()
    m_out = per[(per["platform"] == "meta") & ~per["in_window"]]["ok"].mean()
    m_in = per[(per["platform"] == "meta") & per["in_window"]]["ok"].mean()
    check("attribution: campaign is recoverable from raw events for Google orders (>= 99%)", g_rate >= 0.99, f"{g_rate:.2%}")
    check("attribution: recoverable for Meta orders outside the outage (>= 99%)", m_out >= 0.99, f"{m_out:.2%}")
    check("attribution: not recoverable for Meta orders clicked during the outage (0%)", m_in == 0,
          f"{m_in:.2%} of {int(per['in_window'].sum() - per[per['platform'] == 'google']['in_window'].sum())} orders")

    # ---- 2. Duplicate and late events -------------------------------------------------------
    allev = ev.to_table(columns=["event_id", "event_type", "event_ts", "received_ts"]).to_pandas()
    ed = pl["event_delivery"]
    dup_rate = len(allev) / allev["event_id"].nunique() - 1
    check("delivery: duplicate rate near config", abs(dup_rate - ed["duplicate_rate"]) < 0.002, f"{dup_rate:.2%}")
    dups = allev[allev.duplicated("event_id", keep=False)]
    same = dups.groupby("event_id").agg(t=("event_ts", "nunique"), k=("event_type", "nunique"), r=("received_ts", "nunique"))
    check("delivery: duplicates repeat the same event but arrive at a different time",
          (same["t"] == 1).all() and (same["k"] == 1).all() and (same["r"] > 1).all(), f"{len(same):,} duplicated events")
    first = allev.sort_values("received_ts").drop_duplicates("event_id")
    delay = (first["received_ts"] - first["event_ts"]).dt.total_seconds() / 3600
    late_share = (delay > 1 / 6).mean()
    check("delivery: late-arriving share near config", abs(late_share - ed["late_rate"]) < 0.007, f"{late_share:.2%}")
    check("delivery: nothing arrives later than the maximum delay", delay.max() <= ed["late_max_days"] * 24 + 0.2, f"max {delay.max():.1f} h")
    del allev, dups, first

    # ---- 3. Meta restatements and the schema change --------------------------------------
    sc, mr = pl["schema_change"], pl["meta_restatement"]
    change_at = pd.Timestamp(sc["date"]) + pd.Timedelta(hours=12)
    rows = []
    for kind, pattern in [("initial", "insights"), ("restated", "insights_restated")]:
        for f in glob.glob(f"{d}/raw/meta_ads/{pattern}/*.jsonl"):
            for line in open(f):
                r = json.loads(line)
                purchase = next(int(x["value"]) for x in r["actions"] if x["action_type"] == "purchase")
                rows.append((kind, r["date_start"], r["ad_id"], pd.Timestamp(r.get("extracted_at", "1970-01-01T00:00:00Z").replace("Z", "")),
                             sc["old_field"] in r, sc["new_field"] in r, int(r["link_clicks"]), int(r["impressions"]), purchase))
    mt = pd.DataFrame(rows, columns=["kind", "date", "ad_id", "at", "has_old", "has_new", "clicks", "impr", "purchases"])
    after = mt["at"] >= change_at
    check("schema change: every extract before the change uses the old field name", (mt.loc[~after, "has_old"] & ~mt.loc[~after, "has_new"]).all())
    check("schema change: every extract after the change uses the new name only",
          after.any() and (mt.loc[after, "has_new"] & ~mt.loc[after, "has_old"]).all(), f"{int(after.sum()):,} rows")
    first_bad = mt.loc[after & (mt["kind"] == "initial"), "date"].min()
    check("schema change: first provisional day affected is the change date", first_bad == str(sc["date"]), f"{first_bad}")
    last_day = pd.Timestamp(cfg["dates"]["extract_date"])
    want_restated = {str((last_day - pd.Timedelta(days=k)).date()) for k in range(mr["days"], 400)}
    have = set(mt.loc[mt["kind"] == "restated", "date"])
    prov = sorted(set(mt["date"]) - have)
    check("restatement: every day except the latest 3 has a restated version", len(prov) == mr["days"] and all(x not in want_restated for x in prov),
          f"provisional: {', '.join(prov)}")
    both = mt[mt["kind"] == "initial"].merge(mt[mt["kind"] == "restated"], on=["date", "ad_id"], suffixes=("_i", "_r"))
    ratio = both["purchases_i"].sum() / both["purchases_r"].sum()
    lo, hi = mr["initial_conversion_factor"]
    check("restatement: provisional conversions sit below the final ones", lo - 0.03 <= ratio <= hi + 0.03, f"{ratio:.2f} of final")
    check("restatement: clicks and impressions do not change between versions",
          (both["clicks_i"] == both["clicks_r"]).all() and (both["impr_i"] == both["impr_r"]).all())

    # ---- 4. Planted orders ---------------------------------------------------------------------
    tiny = truth[truth["planted"] == "tiny_bulk"]
    master = pd.read_csv(f"{d}/state/campaign_master.csv", dtype={"campaign_id": str})
    tid = master.loc[master["kind"] == "tiny", "campaign_id"].iloc[0]
    roas = truth[truth["campaign_id"] == tid]["net_sales"].sum() / cd[cd["campaign_id"] == tid]["spend"].sum()
    check("bulk order: exists, is large, and is attributed to the tiny campaign",
          len(tiny) == 1 and tiny["net_sales"].iloc[0] >= 1000 and tiny["campaign_id"].iloc[0] == tid,
          f"${tiny['net_sales'].iloc[0]:,.0f}" if len(tiny) else "missing")
    check("bulk order: tiny campaign ROAS is extreme (> 5) on under $500 of spend", roas > 5 and cd[cd['campaign_id'] == tid]['spend'].sum() < 500,
          f"ROAS {roas:.1f}")
    crm = pd.read_csv(f"{d}/raw/crm/customers.csv", usecols=["crm_id", "zip"], dtype={"zip": str})
    ct = pd.read_csv(f"{d}/state/customer_truth.csv", usecols=["crm_id", "shop_customer_id"])
    group = set(crm.loc[crm["zip"] == cfg["customers"]["small_cell_zip"], "crm_id"].map(dict(zip(ct["crm_id"], ct["shop_customer_id"]))))
    sco = truth[truth["planted"] == "small_cell"]
    local_day = sco["order_ts"].dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    items = pd.read_parquet(f"{d}/raw/shop/order_items/backfill")
    prods = pd.read_csv(f"{d}/raw/catalog/products.csv")
    skillets = set(prods.loc[prods["product_name"].str.contains(pl["planted_orders"]["small_cell"]["product_contains"], regex=False), "product_id"])
    has_skillet = items[items["order_id"].isin(sco["order_id"])].groupby("order_id")["product_id"].apply(lambda s: s.isin(skillets).any())
    check("small cell: at least 2 group customers bought a skillet on the planted day",
          len(sco) >= 2 and set(sco["shop_customer_id"]) <= group and (local_day == str(pl["planted_orders"]["small_cell"]["date"])).all()
          and has_skillet.all(), f"{len(sco)} of {len(group)} customers")
    check("small cell: the ZIP group is below the suppression threshold (< 10 customers)", len(group) < 10, f"{len(group)} customers")

    # ---- 5. CPC spike -------------------------------------------------------------------------------
    sp = pl["cpc_spike"]
    s0, s1 = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"])
    p0, p1 = s0 - pd.Timedelta(days=30), s0 - pd.Timedelta(days=1)
    kw = pd.concat(pd.read_csv(f) for f in glob.glob(f"{d}/raw/google_ads/keyword_perf_daily/*.csv"))
    kw["d"] = pd.to_datetime(kw["date"])
    head = kw[kw["keyword_text"].isin(pl["spike_keywords"])]
    cpc = lambda x, a1, b1: (lambda y: y["cost_micros"].sum() / 1e6 / y["clicks"].sum())(x[(x["d"] >= a1) & (x["d"] <= b1)])
    ratio = cpc(head, s0, s1) / cpc(head, p0, p1)
    other = kw[~kw["keyword_text"].isin(pl["spike_keywords"])]
    other_ratio = cpc(other, s0, s1) / cpc(other, p0, p1)
    check("cpc spike: head-term CPC at least doubles", ratio >= 2.0, f"{ratio:.1f}x")
    check("cpc spike: other search terms are not affected (within 15%)", abs(other_ratio - 1) < 0.15, f"{other_ratio:.2f}x")
    hit = cd[cd["spike_active"]]
    ids = set(hit["campaign_id"])
    aff = cd[cd["campaign_id"].isin(ids)]
    win = lambda x, a1, b1: x[(x["local_date"] >= a1) & (x["local_date"] <= b1)]
    ctr = lambda a1, b1: win(aff, a1, b1)["clicks"].sum() / win(aff, a1, b1)["impressions"].sum()
    check("cpc spike: CTR holds steady (within 10%)", abs(ctr(s0, s1) / ctr(p0, p1) - 1) < 0.10, f"{ctr(s0, s1) / ctr(p0, p1):.2f}x")
    ao = truth[truth["campaign_id"].isin(ids) & (truth["path"] == "ad")]
    opc = lambda a1, b1: len(ao[(ao["order_ts"] >= a1) & (ao["order_ts"] < b1 + pd.Timedelta(days=1))]) / win(aff, a1, b1)["clicks"].sum()
    check("cpc spike: orders per click do not fall (conversion quality unchanged)", opc(s0, s1) / opc(p0, p1) >= 0.9, f"{opc(s0, s1) / opc(p0, p1):.2f}x")
    auc = pd.concat(pd.read_csv(f) for f in glob.glob(f"{d}/raw/google_ads/auction_insights_daily/*.csv"))
    auc["d"] = pd.to_datetime(auc["date"]); auc["campaign_id"] = auc["campaign_id"].astype(str)
    au = auc[auc["campaign_id"].isin(ids)]
    imp = lambda a1, b1: au[(au["d"] >= a1) & (au["d"] <= b1)]["impression_share"].mean()
    check("cpc spike: impression share of affected campaigns falls by at least 25%", imp(s0, s1) / imp(p0, p1) <= 0.75,
          f"{imp(p0, p1):.0%} -> {imp(s0, s1):.0%}")
    srch = cd[cd["channel"] == "SEARCH"]
    so = truth[(truth["path"] == "ad") & truth["campaign_id"].isin(set(srch["campaign_id"]))]
    months = pd.period_range("2025-04", "2026-09", freq="M")
    cpa = {str(mo): srch[srch["local_date"].dt.to_period("M") == mo]["spend"].sum() / max((so["order_ts"].dt.to_period("M") == mo).sum(), 1) for mo in months}
    base = np.mean([cpa["2026-04"], cpa["2026-05"], cpa["2026-06"]])
    check("cpc spike: July search CPA is at least 10% above the April-June average", cpa["2026-07"] / base >= 1.10, f"{cpa['2026-07'] / base - 1:+.0%}")

    # ---- 6. Ground-truth log ---------------------------------------------------------------------------
    log = [json.loads(line) for line in open(f"{d}/ground_truth/injected_issues.jsonl")]
    kinds = {r["issue_type"] for r in log if r.get("pass") in (2, 3)}
    need = {"cpc_spike", "tiny_campaign_bulk_order", "small_cell_skillet_orders", "tracking_outage", "meta_schema_change",
            "meta_restatement", "web_event_delivery"}
    check("ground truth: every Pass 3 incident is logged, IDs unique", need <= kinds and len({r["issue_id"] for r in log}) == len(log),
          f"{len(log)} entries; missing: {sorted(need - kinds)}")

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
