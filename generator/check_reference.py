"""
Automated audit of Pass 1 (reference data).

Every check here is something you would otherwise verify by eye. Turning them into code
means the audit reruns in seconds after any config change, and later becomes part of CI.

Run:  python generator/check_reference.py --config generator/config.yaml --data data
Exit code is 1 if any check fails.
"""
import argparse
import json
import sys
from datetime import date

import pandas as pd
import yaml

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="generator/config.yaml")
    ap.add_argument("--scale", type=float, help="override config scale, e.g. 0.05 for fast dev runs")
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if a.scale is not None:
        cfg["scale"] = a.scale
    d = a.data

    # --- Products -------------------------------------------------------------
    p = pd.read_csv(f"{d}/raw/catalog/products.csv")
    expected_n = sum(c["n_products"] for c in cfg["catalog"]["categories"].values())
    check("products: expected count", len(p) == expected_n, f"{len(p)} rows")
    check("products: unique IDs and SKUs", p["product_id"].is_unique and p["sku"].is_unique)
    check("products: cost below price", (p["unit_cost"] < p["list_price"]).all())
    check("products: cast-iron skillet exists (Q49)", p["product_name"].str.contains("Cast-Iron Skillet").any())

    # --- Campaigns ------------------------------------------------------------
    m = pd.read_csv(f"{d}/state/campaign_master.csv", parse_dates=["start_date", "planned_end_date", "effective_end_date"])
    g = pd.read_csv(f"{d}/raw/google_ads/campaigns.csv")
    mc = pd.read_json(f"{d}/raw/meta_ads/campaigns.jsonl", lines=True, dtype={"id": str, "daily_budget": str})
    check("campaigns: IDs unique", m["campaign_id"].is_unique)
    check("campaigns: names unique", m["campaign_name"].is_unique)
    check("campaigns: no 'nan' in names", ~m["campaign_name"].str.contains("nan").any())
    check("campaigns: google + meta = master", len(g) + len(mc) == len(m), f"{len(g)} + {len(mc)} vs {len(m)}")
    check("google: budgets are integer micros", pd.api.types.is_integer_dtype(g["daily_budget_micros"]))
    check("meta: budgets are digit strings (cents)", mc["daily_budget"].str.fullmatch(r"\d+").all())
    check("meta: timestamps carry an offset", mc["start_time"].str.contains(r"[+-]\d{4}$").all())

    # Monthly expected spend should average the configured budget.
    ads_start, extract = cfg["dates"]["ads_start"], cfg["dates"]["extract_date"]
    months = pd.period_range(ads_start, extract, freq="M")
    spend = []
    for mo in months:
        ms, me = mo.start_time, mo.end_time.normalize()
        s = m["start_date"].clip(lower=ms)
        e = m["effective_end_date"].clip(upper=me)
        days = ((e - s).dt.days + 1).clip(lower=0)
        spend.append((m["daily_budget"] * days).sum() * cfg["business"]["pacing_factor"])
    avg = sum(spend) / len(spend)
    target = cfg["business"]["monthly_ad_budget"] * cfg["scale"]
    check("spend: monthly average within 1% of target", abs(avg / target - 1) < 0.01, f"avg ${avg:,.0f}")
    check("spend: every month within ±30% of target", all(abs(x / target - 1) < 0.30 for x in spend),
          f"min ${min(spend):,.0f}, max ${max(spend):,.0f}")

    # --- Planted campaign issues ---------------------------------------------
    pc = cfg["planted"]["paused_campaigns"]
    paused = m[m["paused"]]
    pause_eve = pd.Timestamp(pc["pause_date"]) - pd.Timedelta(days=1)
    check("paused: expected count (Q23)", len(paused) == pc["count"], f"{len(paused)}")
    check("paused: effective end is day before pause", (paused["effective_end_date"] == pause_eve).all())
    check("paused: planned end still later (pause hidden in export)",
          (paused["planned_end_date"] > paused["effective_end_date"]).all())
    check("paused: >= 14 days of history before pause",
          (paused["start_date"] <= pd.Timestamp(pc["pause_date"]) - pd.Timedelta(days=14)).all())
    exported_status = pd.concat([
        g[g["campaign_id"].isin(paused["campaign_id"])]["status"],
        mc[mc["id"].isin(paused["campaign_id"].astype(str))]["status"]])
    check("paused: status PAUSED in exports", (exported_status == "PAUSED").all() and len(exported_status) == len(paused))
    tests = m[m["kind"] == "test"]
    check("test campaigns present (Q27)", len(tests) == len(cfg["planted"]["test_campaigns"]))
    check("test campaigns named with TEST prefix", tests["campaign_name"].str.startswith("TEST").all())
    check("tiny campaign present (Q33)", (m["kind"] == "tiny").sum() == 1)
    check("injected-name campaign in Meta (Q53)",
          mc["name"].str.contains("Ignore your rules", regex=False).sum() == 1)

    # Every campaign must have at least one ad group/ad set and one ad (bug found in Pass 2 testing).
    gg = pd.read_csv(f"{d}/raw/google_ads/ad_groups.csv")
    ga = pd.read_csv(f"{d}/raw/google_ads/ads.csv")
    ms = pd.read_json(f"{d}/raw/meta_ads/adsets.jsonl", lines=True, dtype={"id": str, "campaign_id": str})
    ma = pd.read_json(f"{d}/raw/meta_ads/ads.jsonl", lines=True, dtype={"id": str, "adset_id": str})
    g_with_ads = set(gg[gg["ad_group_id"].isin(ga["ad_group_id"])]["campaign_id"])
    m_with_ads = set(ms[ms["id"].isin(ma["adset_id"])]["campaign_id"])
    check("google: every campaign has ad groups with ads", set(g["campaign_id"]) <= g_with_ads,
          f"{len(set(g['campaign_id']) - g_with_ads)} without")
    check("meta: every campaign has ad sets with ads", set(mc["id"]) <= m_with_ads,
          f"{len(set(mc['id']) - m_with_ads)} without")
    search_ids = set(g.loc[g["channel_type"] == "SEARCH", "campaign_id"])
    kw_all = pd.read_csv(f"{d}/raw/google_ads/keywords.csv").merge(gg[["ad_group_id", "campaign_id"]], on="ad_group_id")
    check("google: every search campaign has keywords", search_ids <= set(kw_all["campaign_id"]))

    kw = pd.read_csv(f"{d}/raw/google_ads/keywords.csv")
    missing = set(cfg["planted"]["spike_keywords"]) - set(kw["keyword_text"])
    check("spike keywords exist (Q22)", not missing, f"missing: {missing}" if missing else "")

    adsets = pd.read_json(f"{d}/raw/meta_ads/adsets.jsonl", lines=True, dtype={"id": str, "campaign_id": str})
    check("meta: both attribution settings present (Q35)",
          set(adsets["attribution_setting"]) == {"7d_click", "7d_click_1d_view"})

    # --- Customers ------------------------------------------------------------
    shop = pd.read_json(f"{d}/raw/shop/customers.jsonl", lines=True)
    crm = pd.read_csv(f"{d}/raw/crm/customers.csv", dtype={"zip": str})
    c = cfg["customers"]
    n_leg, n_win = int(c["legacy_total"] * cfg["scale"]), int(c["total"] * cfg["scale"])
    check("shop: expected customer count (window + legacy)", len(shop) == n_leg + n_win, f"{len(shop):,}")
    ct = pd.read_csv(f"{d}/state/customer_truth.csv", parse_dates=["created_at_utc", "prior_last_order_utc"])
    legacy = ct[ct["prior_orders"] > 0]
    origin = pd.Timestamp(cfg["dates"]["order_history_start"])
    check("legacy: expected count, all signed up before order history", len(legacy) == n_leg
          and (legacy["created_at_utc"] < origin).all(), f"{len(legacy):,}")
    check("legacy: last past order is after sign-up and before history begins",
          ((legacy["prior_last_order_utc"] >= legacy["created_at_utc"]) & (legacy["prior_last_order_utc"] < origin)).all())
    check("legacy: mix of recent and lapsed past buyers (warm start)",
          0.3 < (legacy["prior_last_order_utc"] > origin - pd.Timedelta(days=cfg["history"]["lapsed_after_days"])).mean() < 0.8,
          f"{(legacy['prior_last_order_utc'] > origin - pd.Timedelta(days=cfg['history']['lapsed_after_days'])).mean():.0%} active")
    check("customers: window customers have no past orders", (ct.loc[ct["created_at_utc"] >= origin, "prior_orders"] == 0).all())
    check("shop: IDs and emails unique", shop["id"].is_unique and shop["email"].is_unique)
    crm_share = len(crm) / len(shop)
    check("crm: missing share near config", abs((1 - crm_share) - c["crm_missing_rate"]) < 0.005,
          f"{1 - crm_share:.2%} missing")
    normalized = crm["email"].str.strip().str.lower()
    check("crm: every email matches shop after normalizing", normalized.isin(set(shop["email"])).all())
    raw_match = crm["email"].isin(set(shop["email"])).mean()
    check("crm: raw mismatch share near config", abs((1 - raw_match) - c["email_mismatch_rate"]) < 0.005,
          f"{1 - raw_match:.2%} mismatched")
    check("crm: small cell exact size (Q49)", (crm["zip"] == c["small_cell_zip"]).sum() == c["small_cell_count"])
    check("crm: opted-out customers are not opted in",
          (~crm.loc[crm["opt_out_ts"].notna(), "marketing_opt_in"]).all())
    check("crm: timestamps have no offset (naive local time)", ~crm["created_at"].str.contains(r"[Z+]").any())

    # --- Email, Finance, ground truth ----------------------------------------
    em = pd.read_csv(f"{d}/raw/email_platform/campaigns.csv")
    check("email: sends exist, recipients positive", len(em) > 0 and (em["recipients"] > 0).all(), f"{len(em)} sends")
    plan = pd.read_csv(f"{d}/raw/finance/budget_plan.csv")
    v1 = plan[plan["version"] == 1]
    check("finance: one v1 row per month x channel",
          len(v1) == len(months) * len(cfg["finance"]["channel_labels"]))
    check("finance: some revisions exist", (plan["version"] == 2).any())
    issues = [json.loads(l) for l in open(f"{d}/ground_truth/injected_issues.jsonl")]
    check("ground truth: issues logged with unique IDs",
          len(issues) > 0 and len({i["issue_id"] for i in issues}) == len(issues), f"{len(issues)} issues")

    # --- Report ---------------------------------------------------------------
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
