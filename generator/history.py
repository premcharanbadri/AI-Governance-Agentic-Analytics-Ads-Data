"""
Lumen Goods synthetic data generator — Pass 2: daily history.

Reads Pass 1 output and simulates activity, in this order:
  A. Campaign-days: daily spend, CPC, clicks, impressions for every live campaign
  B. Ad-driven orders: clicks convert at calibrated rates, after a realistic delay
  C. Email-driven orders, from each newsletter send
  D. Organic orders (direct, organic search) and pre-tracking history back to 2023
  E. Customer assignment: every order gets a real Pass 1 customer (new / returning / lapsed)
  F. Baskets and order financials (items, discounts, shipping, tax)
  G. Refunds
  H. Ad-platform reports (Google CSV, Meta JSON), incl. platform-reported conversions
  I. Keyword performance and auction insights (Google search)
  J. Email engagement metrics
  K. Web events (every buying path, plus a sample of non-buying sessions)

Principle: demand is caused by spend. Pausing a campaign removes its clicks, so it removes its
orders too. Totals are *calibrated* from config targets rather than hard-coded.

Run:  python generator/history.py --out data            (after reference.py)
"""
import argparse
import json
import shutil
import zlib
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from common import smooth_seasonality

TZ = {"google": "America/Los_Angeles", "meta": "America/New_York"}

# Explicit file schemas: a column that happens to be empty in one file must still be typed the same
# as in every other file, or loaders (pyarrow, Snowflake) reject the mismatch.
TS = pa.timestamp("us")
SCHEMAS = {
    "web_events": pa.schema([("event_id", pa.string()), ("event_type", pa.string()), ("event_ts", TS),
                             ("received_ts", TS), ("anonymous_id", pa.string()), ("session_id", pa.string()),
                             ("customer_id", pa.string()), ("utm_source", pa.string()), ("utm_medium", pa.string()),
                             ("utm_campaign", pa.string()), ("utm_content", pa.string()), ("gclid", pa.string()),
                             ("fbclid", pa.string()), ("landing_page", pa.string()), ("device_type", pa.string()),
                             ("ip_address", pa.string()), ("user_agent", pa.string()), ("sample_weight", pa.float64())]),
    "orders": pa.schema([("order_id", pa.int64()), ("shop_customer_id", pa.string()), ("session_id", pa.string()),
                         ("order_ts", TS), ("subtotal", pa.float64()), ("shipping", pa.float64()),
                         ("discount", pa.float64()), ("tax", pa.float64()), ("total", pa.float64()),
                         ("discount_code", pa.string()), ("status", pa.string())]),
    "order_items": pa.schema([("order_id", pa.int64()), ("product_id", pa.int64()), ("quantity", pa.int64()),
                              ("unit_price", pa.float64())]),
    "refunds": pa.schema([("refund_id", pa.string()), ("order_id", pa.int64()), ("refund_ts", TS),
                          ("amount", pa.float64()), ("reason", pa.string())]),
}


def write_parquet(df, path, schema_name):
    schema = SCHEMAS[schema_name]
    df = df[schema.names].copy()
    for f in schema:
        if pa.types.is_string(f.type):   # None/NaN stay null; everything else becomes text
            df[f.name] = df[f.name].astype(object).where(df[f.name].notna(), None).map(
                lambda v: v if v is None else str(v))
    pq.write_table(pa.Table.from_pandas(df, schema=schema, preserve_index=False, safe=False), path)
ET = "America/New_York"
SEC_PER_DAY = 86400


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def local_to_utc(naive_local, tz):
    """Convert naive local timestamps to naive UTC (DST gaps shift forward, overlaps take standard time)."""
    s = pd.Series(naive_local)
    return (s.dt.tz_localize(tz, ambiguous=np.zeros(len(s), dtype=bool), nonexistent="shift_forward")
             .dt.tz_convert("UTC").dt.tz_localize(None).to_numpy())


def utc_to_local_date(naive_utc, tz):
    s = pd.Series(naive_utc)
    return s.dt.tz_localize("UTC").dt.tz_convert(tz).dt.tz_localize(None).dt.normalize().to_numpy()


def random_times_in_day(rng, dates, hour_weights):
    """Timestamps within each given (naive) date, following an hour-of-day pattern."""
    hw = np.asarray(hour_weights, float)
    hours = rng.choice(24, size=len(dates), p=hw / hw.sum())
    secs = hours * 3600 + rng.integers(0, 3600, len(dates))
    return pd.to_datetime(dates).to_numpy() + secs.astype("timedelta64[s]")


def sample_labels(rng, mix, n):
    keys = list(mix)
    p = np.array([mix[k] for k in keys], float)
    return np.array(keys)[rng.choice(len(keys), size=n, p=p / p.sum())]


def hex_ids(rng, n, prefix="", nbytes=8):
    a = rng.integers(0, 2**62, n, dtype=np.int64)
    s = pd.Series(a).map(lambda v: format(v, f"0{nbytes * 2}x")[: nbytes * 2])
    return (prefix + s).to_numpy()


def in_windows(dates, windows):
    d = pd.to_datetime(dates).to_numpy()
    out = np.zeros(len(d), dtype=bool)
    for s, e in windows:
        out |= (d >= np.datetime64(s)) & (d < np.datetime64(e + timedelta(days=1)))
    return out


# ----------------------------------------------------------------------------
# Load Pass 1 output
# ----------------------------------------------------------------------------
def load_reference(d):
    master = pd.read_csv(d / "state/campaign_master.csv",
                         parse_dates=["start_date", "planned_end_date", "effective_end_date"])
    g_groups = pd.read_csv(d / "raw/google_ads/ad_groups.csv")
    g_ads = pd.read_csv(d / "raw/google_ads/ads.csv")
    g_kw = pd.read_csv(d / "raw/google_ads/keywords.csv")
    m_sets = pd.read_json(d / "raw/meta_ads/adsets.jsonl", lines=True, dtype={"id": str, "campaign_id": str})
    m_ads = pd.read_json(d / "raw/meta_ads/ads.jsonl", lines=True, dtype={"id": str, "adset_id": str})
    products = pd.read_csv(d / "raw/catalog/products.csv")
    truth = pd.read_csv(d / "state/customer_truth.csv", parse_dates=["created_at_utc", "prior_last_order_utc"])
    emails = pd.read_csv(d / "raw/email_platform/campaigns.csv", parse_dates=["send_ts"])
    emails["send_ts"] = emails["send_ts"].dt.tz_localize(None)

    # One ads table across platforms: ad_id, ad_group_id, campaign_id (all as strings).
    ga = g_ads.merge(g_groups[["ad_group_id", "campaign_id"]], on="ad_group_id")
    ads = pd.concat([
        pd.DataFrame({"ad_id": ga["ad_id"].astype(str), "ad_group_id": ga["ad_group_id"].astype(str),
                      "campaign_id": ga["campaign_id"].astype(str)}),
        pd.DataFrame({"ad_id": m_ads["id"], "ad_group_id": m_ads["adset_id"],
                      "campaign_id": m_ads["adset_id"].map(m_sets.set_index("id")["campaign_id"])}),
    ], ignore_index=True)
    master["campaign_id"] = master["campaign_id"].astype(str)
    g_kw = g_kw.merge(g_groups[["ad_group_id", "campaign_id"]], on="ad_group_id")
    g_kw["campaign_id"] = g_kw["campaign_id"].astype(str)
    return master, ads, g_kw, products, truth, emails


# ----------------------------------------------------------------------------
# Step A: campaign-days (spend, CPC, clicks, impressions)
# ----------------------------------------------------------------------------
def keyword_profile(cfg, cid, grp):
    """A campaign's fixed keyword mix: click share `w`, relative CPC `c`, and which keywords are the
    planted head terms. Head terms get more volume, like real 'cast iron skillet'-style queries."""
    r = np.random.default_rng(zlib.crc32(("kw" + cid).encode()))
    w = r.dirichlet(np.full(len(grp), 1.5))
    c = r.lognormal(0, 0.25, len(grp))
    is_spike = grp["keyword_text"].isin(cfg["planted"]["spike_keywords"]).to_numpy()
    w = w * np.where(is_spike, cfg["planted"]["cpc_spike"]["head_term_boost"], 1.0)
    return w / w.sum(), c, is_spike


def make_campaign_days(rng, cfg, master, kw):
    h, d = cfg["history"], cfg["dates"]
    ads_start, extract = pd.Timestamp(d["ads_start"]), pd.Timestamp(d["extract_date"])
    rows = []
    for r in master.itertuples():
        s, e = max(r.start_date, ads_start), min(r.effective_end_date, extract)
        if e < s:
            continue
        dates = pd.date_range(s, e, freq="D")
        rows.append(pd.DataFrame({"campaign_id": r.campaign_id, "local_date": dates}))
    cd = pd.concat(rows, ignore_index=True).merge(
        master[["campaign_id", "platform", "channel", "intent", "category", "kind", "daily_budget"]], on="campaign_id")

    # Spend = budget x pacing (slightly lower on weekends).
    p = h["pacing"]
    pace = np.clip(rng.normal(p["mean"], p["sd"], len(cd)), p["min"], p["max"])
    pace *= np.where(cd["local_date"].dt.dayofweek >= 5, p["weekend_factor"], 1.0)
    cd["spend"] = (cd["daily_budget"] * pace).round(2)

    # CPC: channel base x Q4 inflation x diminishing returns x noise. Clicks = spend / CPC.
    econ = h["channel_economics"]
    base_cpc = cd["channel"].map({k: v["cpc"] for k, v in econ.items()})
    median_budget = cd.groupby("channel")["daily_budget"].transform("median")
    size = (cd["daily_budget"] / median_budget) ** h["diminishing_returns"]
    season = cd["local_date"].dt.month.map(h["seasonal_cpc"]).fillna(1.0)
    cpc = base_cpc * season * size * rng.lognormal(0, h["cpc_noise_sd"], len(cd))

    # Planted (Q22): competitors bid up the head search terms. A campaign's average CPC rises by the
    # cost-weighted share of those terms in its keyword mix. Budgets are fixed, so it buys fewer clicks,
    # and everything downstream (orders, CPA) follows from that.
    sp = cfg["planted"]["cpc_spike"]
    share = {}
    for cid, grp in kw.groupby("campaign_id"):
        w, c, is_spike = keyword_profile(cfg, cid, grp)
        if is_spike.any():
            share[cid] = float((w * c * is_spike).sum() / (w * c).sum())
    ratio = cd["campaign_id"].map(share).fillna(0.0).to_numpy()
    in_window = ((cd["channel"] == sp["channel"]) & (cd["local_date"] >= pd.Timestamp(sp["start"]))
                 & (cd["local_date"] <= pd.Timestamp(sp["end"]))).to_numpy()
    active = in_window & (ratio > 0)
    factor = np.where(active, 1 + ratio * (sp["multiplier"] - 1), 1.0)
    cd["spike_active"], cd["cpc_factor"] = active, factor
    cpc = cpc * factor
    cd["clicks"] = rng.poisson(cd["spend"] / cpc)
    ctr = cd["channel"].map({k: v["ctr"] for k, v in econ.items()}) * rng.lognormal(0, 0.10, len(cd))
    cd["impressions"] = np.maximum(np.round(cd["clicks"] / ctr), cd["clicks"]).astype(np.int64)
    cd["landing_rate"] = cd["channel"].map({k: v["landing_rate"] for k, v in econ.items()})
    return cd


# ----------------------------------------------------------------------------
# Step F helper: baskets (also used to estimate average order value for calibration)
# ----------------------------------------------------------------------------
def build_baskets(rng, cfg, products, category_pref):
    """category_pref: array of category names or '' (no preference), one per order."""
    b = cfg["history"]["basket"]
    n = len(category_pref)
    n_items = sample_labels(rng, b["items_per_order"], n).astype(int)
    order_idx = np.repeat(np.arange(n), n_items)
    first = np.r_[True, order_idx[1:] != order_idx[:-1]] if len(order_idx) else np.array([], bool)

    # Popularity ~ 1/rank^skew, with ranks shuffled once so popularity isn't tied to product ID.
    rank = np.random.default_rng(cfg["seed"] + 7).permutation(len(products)) + 1
    pop = 1.0 / rank ** b["popularity_skew"]
    prod_ids = products["product_id"].to_numpy()
    chosen = np.empty(len(order_idx), dtype=np.int64)
    pref = np.asarray(category_pref)[order_idx]
    biased = first & (pref != "") & (rng.random(len(order_idx)) < b["category_bias"])
    chosen[~biased] = rng.choice(prod_ids, size=(~biased).sum(), p=pop / pop.sum())
    for cat in np.unique(pref[biased]):
        rows = biased & (pref == cat)
        in_cat = (products["category"] == cat).to_numpy()
        w = pop[in_cat]
        chosen[rows] = rng.choice(prod_ids[in_cat], size=rows.sum(), p=w / w.sum())
    qty = sample_labels(rng, b["quantity"], len(order_idx)).astype(int)
    price = products.set_index("product_id").loc[chosen, "list_price"].to_numpy()
    items = pd.DataFrame({"order_idx": order_idx, "product_id": chosen, "quantity": qty, "unit_price": price})
    subtotal = np.bincount(order_idx, weights=qty * price, minlength=n)
    return items, subtotal


# ----------------------------------------------------------------------------
# Step B: ad-driven order intents
# ----------------------------------------------------------------------------
def make_ad_intents(rng, cfg, cd, ads, products):
    h = cfg["history"]
    rel = h["relative_cvr"]
    windows = [(s["start"], s["end"]) for s in cfg["campaigns"]["seasonal"]]
    rcvr = np.array([rel[c].get(i, 1.0) for c, i in zip(cd["channel"], cd["intent"])])
    lift = np.where(in_windows(cd["local_date"], windows), h["seasonal_event_cvr_lift"], 1.0)
    lift *= cd["local_date"].dt.month.map(h["monthly_cvr"]).fillna(1.0).to_numpy()
    weight = cd["clicks"].to_numpy() * cd["landing_rate"].to_numpy() * rcvr * lift
    weight = np.where(cd["kind"].to_numpy() == "test", 0.0, weight)   # internal QA campaigns: spend and clicks, no buyers

    # Calibration: choose one scale k so expected attributed net sales / spend = target ROAS.
    # Average order value is estimated by simulating 20,000 baskets, less a typical discount.
    _, sub = build_baskets(np.random.default_rng(cfg["seed"] + 1), cfg, products, np.full(20000, ""))
    aov_net = sub.mean() * 0.974
    k = h["target_roas"] * cd["spend"].sum() / (aov_net * weight.sum())
    cd["expected_orders"] = weight * k
    cd["n_orders"] = rng.poisson(cd["expected_orders"])

    it = cd.loc[cd.index.repeat(cd["n_orders"]), ["campaign_id", "platform", "channel", "intent", "category",
                                                   "local_date"]].reset_index(drop=True)
    hw = h["web"]["hour_weights"]
    local_click = random_times_in_day(rng, it["local_date"], hw)
    it["click_ts"] = np.empty(len(it), dtype="datetime64[ns]")
    for plat, tz in TZ.items():
        m = (it["platform"] == plat).to_numpy()
        it.loc[m, "click_ts"] = local_to_utc(local_click[m], tz)

    # Delay from click to purchase: same visit (10-55 min) or a later visit (lognormal days).
    same = rng.random(len(it)) < h["same_session_share"]
    lag_cfg = h["later_lag_days"]
    later = np.minimum(rng.lognormal(np.log(lag_cfg["median"]), lag_cfg["sigma"], len(it)), lag_cfg["max"])
    lag_s = np.where(same, rng.integers(600, 3300, len(it)), np.maximum(later * SEC_PER_DAY, 3600))
    it["same_session"] = same
    it["order_ts"] = it["click_ts"].to_numpy() + lag_s.astype("timedelta64[s]")

    # Which ad inside the campaign was clicked (fixed per-ad weights, reused for ad-level reporting).
    ad_w = {}
    it["ad_id"] = ""
    for cid, grp in ads.groupby("campaign_id"):
        w = np.random.default_rng(zlib.crc32(cid.encode())).dirichlet(np.full(len(grp), 2.0))
        ad_w[cid] = (grp["ad_id"].to_numpy(), grp["ad_group_id"].to_numpy(), w)
        m = (it["campaign_id"] == cid).to_numpy()
        if m.any():
            it.loc[m, "ad_id"] = rng.choice(ad_w[cid][0], size=m.sum(), p=w)
    it["requested_type"] = ""
    for intent, grp in it.groupby("intent"):
        it.loc[grp.index, "requested_type"] = sample_labels(rng, h["customer_mix"][intent], len(grp))
    it["path"] = "ad"
    it["category_pref"] = np.where(it["category"].isin(["All", "Brand"]), "", it["category"])
    return it, ad_w, k, aov_net


# ----------------------------------------------------------------------------
# Steps C + D: email and organic intents
# ----------------------------------------------------------------------------
def make_email_intents(rng, cfg, emails):
    e = cfg["history"]["email"]
    emails["delivered"] = np.round(emails["recipients"] * e["delivery_rate"]).astype(int)
    n = rng.poisson(emails["delivered"] * e["order_rate"])
    it = emails.loc[emails.index.repeat(n), ["email_campaign_id", "send_ts", "target_segment"]].reset_index(drop=True)
    hours = np.minimum(rng.lognormal(np.log(5), 1.2, len(it)), 72)
    it["order_ts"] = it["send_ts"].to_numpy() + (hours * 3600).astype("timedelta64[s]")
    it["requested_type"] = ""
    for seg, grp in it.groupby("target_segment"):
        it.loc[grp.index, "requested_type"] = sample_labels(rng, cfg["history"]["customer_mix"][f"email_{seg}"], len(grp))
    it["path"] = "email"
    it["category_pref"] = ""
    return it.drop(columns=["send_ts", "target_segment"])


def make_organic_intents(rng, cfg, ad_intents, email_intents):
    h, d = cfg["history"], cfg["dates"]
    ads_start = pd.Timestamp(d["ads_start"])
    days = pd.date_range(d["order_history_start"], d["extract_date"], freq="D")
    years = (days - ads_start).days / 365.25
    season = smooth_seasonality(days, cfg["customers"]["monthly_seasonality"])
    dow = np.array(h["day_of_week"])[days.dayofweek]
    trend = (1 + h["yearly_growth"]) ** years * season * dow
    organic_level = h["organic_orders_per_day"] * cfg["scale"]

    # Before web tracking existed, all orders are "untracked". Their level continues the first
    # four weeks of tracked totals backward, so there's no jump at the ads_start boundary.
    first4 = (lambda t: (t >= ads_start) & (t < ads_start + pd.Timedelta(days=28)))
    tracked_level = (first4(pd.to_datetime(ad_intents["order_ts"])).sum()
                     + first4(pd.to_datetime(email_intents["order_ts"])).sum()) / 28 + organic_level
    pre = days < ads_start
    level = np.where(pre, tracked_level, organic_level)
    n = rng.poisson(level * trend)
    dates = np.repeat(days.to_numpy(), n)
    local = random_times_in_day(rng, dates, h["web"]["hour_weights"])
    it = pd.DataFrame({"order_ts": local_to_utc(local, ET)})
    it["path"] = np.where(np.repeat(pre, n), "untracked", "organic")
    it["requested_type"] = ""
    for path, grp in it.groupby("path"):
        it.loc[grp.index, "requested_type"] = sample_labels(rng, h["customer_mix"][path], len(grp))
    it["category_pref"] = ""
    return it


# ----------------------------------------------------------------------------
# Step E: assign every order to a real customer
# ----------------------------------------------------------------------------
def assign_customers(rng, cfg, intents, truth):
    h = cfg["history"]
    t0 = np.datetime64(pd.Timestamp(cfg["dates"]["order_history_start"]))
    created = (truth["created_at_utc"].to_numpy() - t0) / np.timedelta64(1, "s")   # seconds since start
    n_cust = len(truth)
    prior_n = truth["prior_orders"].to_numpy()
    count = prior_n.astype(np.int32)         # orders placed before history begins
    last = np.full(n_cust, -np.inf)          # last order time (seconds since t0; negative = before history)
    has_prior = prior_n > 0
    last[has_prior] = (truth["prior_last_order_utc"].to_numpy()[has_prior] - t0) / np.timedelta64(1, "s")
    lapse_s = h["lapsed_after_days"] * SEC_PER_DAY
    tau_new = h["new_customer_recency_days"] * SEC_PER_DAY

    intents = intents.sort_values("order_ts", kind="stable").reset_index(drop=True)
    ts = (intents["order_ts"].to_numpy() - t0) / np.timedelta64(1, "s")
    day = np.floor(ts / SEC_PER_DAY).astype(np.int64)
    req = intents["requested_type"].to_numpy()
    cust = np.full(len(intents), -1, dtype=np.int64)
    shift = np.zeros(len(intents))           # seconds an order was pushed later because the buyer signed up after it
    # Each customer has a fixed, bounded tendency to reorder (some are fans, most are occasional).
    # An earlier version weighted by past order count, which snowballed into customers with 90 orders.
    propensity = rng.gamma(h["repeat_propensity_shape"], 1 / h["repeat_propensity_shape"], n_cust)
    final_type = np.array([""] * len(intents), dtype=object)
    stats = {"fallback": 0, "dropped": 0}
    bounds = np.r_[0, np.flatnonzero(np.diff(day)) + 1, len(day)]

    def pick(cand, w, m):
        m = min(m, len(cand))
        if m == 0:
            return np.array([], dtype=np.int64)
        return rng.choice(cand, size=m, replace=False, p=w / w.sum())

    for a, b in zip(bounds[:-1], bounds[1:]):
        day_start = day[a] * SEC_PER_DAY
        now = day_start + SEC_PER_DAY               # end of this day
        idx = np.arange(a, b)
        queue = {t: list(idx[req[a:b] == t]) for t in ("new", "lapsed", "returning")}
        # new -> leftover becomes returning; lapsed -> leftover becomes returning; returning -> leftover new.
        for t in ("new", "lapsed", "returning", "new_fallback"):
            want = queue.get(t if t != "new_fallback" else "new_retry", [])
            if not want:
                continue
            if t in ("new", "new_fallback"):
                # A buyer must have signed up at least 21 minutes before the day ends: their order is placed 1-20
                # minutes after sign-up, and must not spill into the next day (or past the end of the data).
                cand = np.flatnonzero((count == 0) & (created < now - 1260))
                w = np.exp(-np.clip(now - created[cand], 0, None) / tau_new) + 1e-12
            elif t == "lapsed":
                cand = np.flatnonzero((count > 0) & (last < now - lapse_s))
                w = propensity[cand] * np.exp(-(now - lapse_s - last[cand]) / (365 * SEC_PER_DAY)) + 1e-12
            else:
                # Exclude anyone who already bought today, so a same-day "returning" order can't
                # precede their first order.
                cand = np.flatnonzero((count > 0) & (last >= now - lapse_s) & (last < day_start))
                # No recency term: weighting recent buyers higher makes anyone who just ordered likely to
                # order again, which snowballs into streaks. The 180-day window already limits the pool.
                w = propensity[cand] + 1e-12
            got = pick(cand, w, len(want))
            rows = np.array(want[: len(got)], dtype=np.int64)
            cust[rows] = got
            final_type[rows] = "new" if t in ("new", "new_fallback") else t
            # New customers who signed up later the same day buy a few minutes after signing up.
            if t in ("new", "new_fallback") and len(rows):
                late = created[got] > ts[rows]
                if late.any():
                    old = ts[rows[late]].copy()
                    ts[rows[late]] = created[got[late]] + rng.integers(60, 1200, late.sum())
                    shift[rows[late]] = ts[rows[late]] - old
            count[got] += 1
            last[got] = np.maximum(last[got], ts[rows]) if len(rows) else last[got]
            rest = want[len(got):]
            if rest:
                stats["fallback"] += len(rest)
                if t in ("new", "lapsed"):
                    queue["returning"] = queue["returning"] + rest
                elif t == "returning":
                    queue["new_retry"] = rest
                else:
                    stats["dropped"] += len(rest)
                    stats["fallback"] -= len(rest)

    intents["order_ts"] = t0 + (ts * 1e9).astype("timedelta64[ns]")
    # A purchase pushed more than 15 minutes later can no longer be "the same visit as the click":
    # the shopper clicked, left, and came back after creating an account. Without this, sessions
    # would last up to a day (found in the Pass 2 review).
    moved = (shift > 900) & (intents["same_session"] == True)
    intents.loc[moved, "same_session"] = False
    stats["moved_to_later_visit"] = int(moved.sum())
    intents["customer_idx"] = cust
    intents["customer_type"] = final_type
    kept = intents[cust >= 0].sort_values("order_ts", kind="stable").reset_index(drop=True)
    return kept, stats



# ----------------------------------------------------------------------------
# Planted orders (Q33, Q49): specific orders so benchmark questions have something real to find
# ----------------------------------------------------------------------------
def plant_orders(rng, cfg, out, orders, master, ad_w, truth, products):
    po = cfg["planted"]["planted_orders"]
    lapse = np.timedelta64(cfg["history"]["lapsed_after_days"], "D")
    created = truth["created_at_utc"].to_numpy()
    prior_last = truth["prior_last_order_utc"].to_numpy()

    def state_before(day):
        """Each customer's latest order before `day` (including pre-history orders), and who has
        any order from `day` onward. Planted orders go only to customers with nothing later, so no
        existing new/returning/lapsed label changes."""
        last = prior_last.copy()
        before = orders[orders["order_ts"] < day].groupby("customer_idx")["order_ts"].max()
        idx = before.index.to_numpy()
        cur = last[idx]
        last[idx] = np.where(np.isnat(cur) | (before.to_numpy() > cur), before.to_numpy(), cur)
        later = np.zeros(len(truth), dtype=bool)
        later[orders.loc[orders["order_ts"] >= day, "customer_idx"].to_numpy()] = True
        return last, later

    def label(last, T):
        if np.isnat(last):
            return "new"
        return "returning" if (T - last) <= lapse else "lapsed"

    def pick_product(text):
        m = products[products["product_name"].str.contains(text, regex=False)].sort_values("list_price")
        return int(m.iloc[len(m) // 2]["product_id"])

    rows = []
    # (a) A very large order on the ~$8/day clearance campaign: extreme ROAS from almost no spend.
    tb = po["tiny_bulk"]
    tiny = master[master["kind"] == "tiny"].iloc[0]
    T = local_to_utc([pd.Timestamp(tb["date"]) + pd.Timedelta(hours=13, minutes=40)], TZ["google"])[0]
    cut = np.datetime64(pd.Timestamp(tb["date"]) - pd.Timedelta(days=1))
    last, later = state_before(cut)
    gap = T - last
    ok = (created < T) & ~np.isnat(last) & (gap > np.timedelta64(1, "D")) & (gap <= np.timedelta64(170, "D")) & ~later
    cidx = int(rng.choice(np.flatnonzero(ok)))
    ad_ids, _, w = ad_w[tiny.campaign_id]
    rows.append({"campaign_id": tiny.campaign_id, "platform": "google", "channel": "SHOPPING", "intent": "prospecting",
                 "category": "All", "local_date": pd.Timestamp(tb["date"]), "click_ts": T - np.timedelta64(22, "m"),
                 "same_session": True, "ad_id": str(rng.choice(ad_ids, p=w)), "requested_type": "returning",
                 "path": "ad", "category_pref": "", "order_ts": T, "customer_idx": cidx,
                 "customer_type": "returning", "planted": "tiny_bulk",
                 "planted_product": pick_product(tb["product_contains"]), "planted_qty": tb["quantity"]})

    # (b) Orders containing a cast-iron skillet from customers in the tiny ZIP group (Q49).
    sc = po["small_cell"]
    crm = pd.read_csv(out / "raw/crm/customers.csv", dtype={"zip": str}, usecols=["crm_id", "zip"])
    where = {cid: i for i, cid in enumerate(truth["crm_id"])}
    group = [where[c] for c in crm.loc[crm["zip"] == cfg["customers"]["small_cell_zip"], "crm_id"]]
    day0 = pd.Timestamp(sc["date"])
    last, later = state_before(np.datetime64(day0 - pd.Timedelta(days=1)))
    times = [day0 + pd.Timedelta(hours=h, minutes=m) for h, m in [(11, 20), (15, 45), (20, 5)]]
    utc = local_to_utc(times, ET)
    eligible = [i for i in rng.permutation(group) if created[i] < utc[0] and not later[i]]
    chosen = eligible[: sc["customers"]]
    if len(chosen) < 2:
        raise RuntimeError("Fewer than 2 small-cell customers are eligible for a planted order; change the seed or date")
    pid = pick_product(sc["product_contains"])
    for i, T in zip(chosen, utc):
        rows.append({"path": "organic", "category_pref": "", "order_ts": T, "customer_idx": int(i),
                     "customer_type": label(last[i], T), "planted": "small_cell", "planted_product": pid,
                     "planted_qty": np.nan, "requested_type": ""})

    base = orders.assign(planted="", planted_product=np.nan, planted_qty=np.nan)
    return (pd.concat([base, pd.DataFrame(rows)], ignore_index=True)
            .sort_values("order_ts", kind="stable").reset_index(drop=True))

# ----------------------------------------------------------------------------
# Steps F + G: order financials and refunds
# ----------------------------------------------------------------------------
def make_orders(rng, cfg, orders, products, truth):
    h = cfg["history"]
    orders["order_id"] = np.arange(5_100_000_001, 5_100_000_001 + len(orders), dtype=np.int64)
    orders["shop_customer_id"] = truth["shop_customer_id"].to_numpy()[orders["customer_idx"].to_numpy()]
    items, subtotal = build_baskets(rng, cfg, products, orders["category_pref"].to_numpy())

    # Planted orders get specific baskets: one huge bulk line, or an ordinary basket that includes the skillet.
    if "planted" in orders.columns:
        pl = orders["planted"].fillna("").to_numpy()
        pp, pq_ = orders["planted_product"].to_numpy(), orders["planted_qty"].to_numpy()
        price_of = products.set_index("product_id")["list_price"]
        for i in np.flatnonzero(pl == "tiny_bulk"):
            items = items[items["order_idx"] != i]
            items = pd.concat([items, pd.DataFrame({"order_idx": [i], "product_id": [int(pp[i])],
                                                    "quantity": [int(pq_[i])], "unit_price": [float(price_of[int(pp[i])])]})])
        for i in np.flatnonzero(pl == "small_cell"):
            mine = items["order_idx"] == i
            if (items.loc[mine, "product_id"] == int(pp[i])).any():
                continue
            first = items.index[mine][0]
            items.loc[first, "product_id"] = int(pp[i])
            items.loc[first, "unit_price"] = float(price_of[int(pp[i])])
        items = items.sort_values("order_idx", kind="stable").reset_index(drop=True)
        items["product_id"], items["quantity"] = items["product_id"].astype("int64"), items["quantity"].astype("int64")
        subtotal = np.bincount(items["order_idx"].to_numpy(), weights=(items["quantity"] * items["unit_price"]).to_numpy(),
                               minlength=len(orders))
    items["order_id"] = orders["order_id"].to_numpy()[items["order_idx"].to_numpy()]

    windows = [(s["start"], s["end"]) for s in cfg["campaigns"]["seasonal"]]
    local_date = utc_to_local_date(orders["order_ts"].to_numpy(), ET)
    event = in_windows(local_date, windows)
    dc = h["discounts"]
    r = rng.random(len(orders))
    code = np.where(event & (r < dc["sale"]["share_during_events"]), dc["sale"]["code"],
                    np.where(~event & (orders["customer_type"] == "new") & (r < dc["welcome"]["share_of_new"]),
                             dc["welcome"]["code"], ""))
    pct = np.where(code == dc["sale"]["code"], dc["sale"]["pct"], np.where(code == dc["welcome"]["code"],
                                                                            dc["welcome"]["pct"], 0.0))
    orders["subtotal"] = subtotal.round(2)
    orders["discount"] = (subtotal * pct).round(2)
    net = orders["subtotal"] - orders["discount"]
    orders["shipping"] = np.where(net >= h["shipping"]["free_over"], 0.0, h["shipping"]["fee"])
    orders["tax"] = (net * h["tax_rate"]).round(2)
    orders["total"] = (net + orders["shipping"] + orders["tax"]).round(2)
    orders["net_sales"] = net.round(2)
    orders["discount_code"] = code

    # Refunds: some share of orders, full or partial, within the return window.
    rf = h["refunds"]
    is_ref = rng.random(len(orders)) < rf["rate"]
    ref = orders.loc[is_ref, ["order_id", "order_ts", "total"]].copy()
    ref["refund_ts"] = ref["order_ts"].to_numpy() + rng.integers(2 * SEC_PER_DAY, rf["max_days"] * SEC_PER_DAY,
                                                                 len(ref)).astype("timedelta64[s]")
    full = rng.random(len(ref)) < rf["full_share"]
    ref["amount"] = np.where(full, ref["total"], (ref["total"] * rng.uniform(0.2, 0.6, len(ref))).round(2))
    ref["reason"] = sample_labels(rng, {"damaged": 0.25, "not_as_described": 0.2, "changed_mind": 0.4,
                                        "wrong_item": 0.15}, len(ref))
    ref["refund_id"] = hex_ids(rng, len(ref), "rf_", 6)
    extract_end = np.datetime64(pd.Timestamp(cfg["dates"]["extract_date"]) + pd.Timedelta(days=1))
    cols = ["refund_id", "order_id", "refund_ts", "amount", "reason"]
    pending = ref[ref["refund_ts"].to_numpy() >= extract_end]      # arrive after the extract: emitted by the live stream
    ref = ref[ref["refund_ts"].to_numpy() < extract_end]
    return orders, items.drop(columns="order_idx"), ref[cols], pending[cols]


# ----------------------------------------------------------------------------
# Step H: ad-platform reports
# ----------------------------------------------------------------------------
def split_to_ads(rng, cd, ad_w):
    """Split each campaign-day's clicks/impressions/spend across its ads (sums stay exact)."""
    out = []
    for r in cd.itertuples():
        ad_ids, grp_ids, w = ad_w[r.campaign_id]
        clicks = rng.multinomial(r.clicks, w)
        impr = rng.multinomial(r.impressions, w)
        cost_w = rng.dirichlet(w * 200 + 1e-3)
        cost_micros = np.floor(cost_w * round(r.spend * 1e6)).astype(np.int64)
        cost_micros[np.argmax(cost_w)] += int(round(r.spend * 1e6)) - cost_micros.sum()
        out.append(pd.DataFrame({"campaign_id": r.campaign_id, "ad_group_id": grp_ids, "ad_id": ad_ids,
                                 "local_date": r.local_date, "impressions": impr, "clicks": clicks,
                                 "cost_micros": cost_micros, "platform": r.platform}))
    return pd.concat(out, ignore_index=True)


def platform_conversions(rng, cfg, ad_days, orders):
    h = cfg["history"]
    ad_orders = orders[orders["path"] == "ad"].copy()
    ad_orders["local_date"] = pd.NaT
    for plat, tz in TZ.items():
        m = (ad_orders["platform"] == plat).to_numpy()
        ad_orders.loc[m, "local_date"] = utc_to_local_date(ad_orders.loc[m, "click_ts"].to_numpy(), tz)
    ad_orders["local_date"] = pd.to_datetime(ad_orders["local_date"])
    fp = (ad_orders.groupby(["ad_id", "local_date"])
          .agg(fp_orders=("order_id", "size"), fp_sales=("net_sales", "sum")).reset_index())
    ad_days = ad_days.merge(fp, on=["ad_id", "local_date"], how="left").fillna({"fp_orders": 0, "fp_sales": 0.0})
    oc = h["platform_overcount"]
    change = np.datetime64(pd.Timestamp(cfg["planted"]["attribution_change_date"]))
    factor = np.where(ad_days["platform"] == "google", oc["google"],
                      np.where(ad_days["local_date"].to_numpy() < change, oc["meta_before_change"],
                               oc["meta_after_change"]))
    aov = np.where(ad_days["fp_orders"] > 0, ad_days["fp_sales"] / ad_days["fp_orders"].clip(lower=1), 95.0)
    expected = ad_days["fp_orders"] * factor + ad_days["clicks"] * 0.0005          # small view-through noise
    google = ad_days["platform"] == "google"
    conv = np.where(google, np.round(expected * rng.lognormal(0, 0.08, len(ad_days)), 2),   # fractional (data-driven)
                    rng.poisson(expected))
    ad_days["platform_conversions"] = conv
    ad_days["platform_conversion_value"] = np.round(conv * aov * rng.lognormal(0, 0.05, len(ad_days)), 2)
    return ad_days


def write_google(out, ad_days):
    g = ad_days[ad_days["platform"] == "google"]
    folder = out / "raw/google_ads/ad_perf_daily"
    folder.mkdir(parents=True, exist_ok=True)
    for date, grp in g.groupby("local_date"):
        grp.assign(date=date.strftime("%Y-%m-%d"))[
            ["date", "campaign_id", "ad_group_id", "ad_id", "impressions", "clicks", "cost_micros",
             "platform_conversions", "platform_conversion_value"]
        ].to_csv(folder / f"date={date:%Y-%m-%d}.csv", index=False)


def write_meta(out, ad_days):
    m = ad_days[ad_days["platform"] == "meta"]
    folder = out / "raw/meta_ads/insights"
    folder.mkdir(parents=True, exist_ok=True)
    for date, grp in m.groupby("local_date"):
        ds = date.strftime("%Y-%m-%d")
        with open(folder / f"date={ds}.jsonl", "w") as fh:
            for r in grp.itertuples():
                # Meta returns numbers as strings and buries conversions in nested arrays.
                fh.write(json.dumps({
                    "date_start": ds, "date_stop": ds, "campaign_id": r.campaign_id, "adset_id": r.ad_group_id,
                    "ad_id": r.ad_id, "impressions": str(r.impressions), "link_clicks": str(r.clicks),
                    "spend": f"{r.cost_micros / 1e6:.2f}",
                    "actions": [{"action_type": "link_click", "value": str(r.clicks)},
                                {"action_type": "purchase", "value": str(int(r.platform_conversions))}],
                    "action_values": [{"action_type": "purchase", "value": f"{r.platform_conversion_value:.2f}"}],
                }) + "\n")


# ----------------------------------------------------------------------------
# Step I: keyword performance and auction insights
# ----------------------------------------------------------------------------
def make_keywords(rng, cfg, cd, kw):
    sp = cfg["planted"]["cpc_spike"]
    search = cd[cd["channel"] == "SEARCH"]
    fixed = {}
    for cid, grp in kw.groupby("campaign_id"):
        w, c, is_spike = keyword_profile(cfg, cid, grp)
        fixed[cid] = (grp, w, c, is_spike)
    out = []
    for r in search.itertuples():
        if r.campaign_id not in fixed:
            continue
        grp, w, cpc_mult, is_spike = fixed[r.campaign_id]
        clicks = rng.multinomial(r.clicks, w)
        impr = rng.multinomial(r.impressions, w)
        # During the spike the head terms cost `multiplier` times more per click.
        cw = clicks * cpc_mult * np.where(is_spike & r.spike_active, sp["multiplier"], 1.0)
        cw = cw / cw.sum() if cw.sum() > 0 else w
        total = int(round(r.spend * 1e6))
        cost = np.floor(cw * total).astype(np.int64)
        cost[np.argmax(cw)] += total - cost.sum()
        out.append(pd.DataFrame({"date": r.local_date.strftime("%Y-%m-%d"), "keyword_id": grp["keyword_id"].to_numpy(),
                                 "keyword_text": grp["keyword_text"].to_numpy(),
                                 "match_type": grp["match_type"].to_numpy(),
                                 "impressions": impr, "clicks": clicks, "cost_micros": cost,
                                 "campaign_id": r.campaign_id}))
    return pd.concat(out, ignore_index=True)


def make_auction(rng, cd):
    a = cd[cd["channel"].isin(["SEARCH", "SHOPPING"])].copy()
    brand = (a["intent"] == "brand").to_numpy()
    imp = np.clip(np.where(brand, rng.normal(0.93, 0.02, len(a)), rng.normal(0.72, 0.04, len(a))), 0.1, 0.99)
    ovl = np.clip(rng.normal(0.25, 0.03, len(a)), 0, 1)
    outr = np.clip(rng.normal(0.45, 0.04, len(a)), 0, 1)
    f = a["cpc_factor"].to_numpy()
    hit = f > 1
    # Fixed budget + higher CPC = fewer impressions won; more rivals overlap with us and outrank us.
    a["impression_share"] = np.clip(imp / f, 0.05, 0.99).round(4)
    a["overlap_rate"] = np.clip(ovl + np.where(hit, 0.12, 0.0), 0, 1).round(4)
    a["outranking_share"] = np.clip(outr - np.where(hit, 0.10, 0.0), 0, 1).round(4)
    a["date"] = a["local_date"].dt.strftime("%Y-%m-%d")
    return a[["date", "campaign_id", "impression_share", "overlap_rate", "outranking_share"]]


# ----------------------------------------------------------------------------
# Step J: email engagement
# ----------------------------------------------------------------------------
def make_email_daily(rng, cfg, emails):
    e = cfg["history"]["email"]
    rows = []
    for r in emails.itertuples():
        opens = rng.binomial(r.delivered, e["open_rate"])
        clicks = rng.binomial(r.delivered, e["click_rate"])
        unsubs = rng.binomial(r.delivered, e["unsubscribe_rate"])
        for k, share in enumerate([0.6, 0.25, 0.15]):          # engagement trails off over 3 days
            rows.append({"date": (r.send_ts.normalize() + pd.Timedelta(days=k)).strftime("%Y-%m-%d"),
                         "email_campaign_id": r.email_campaign_id, "delivered": r.delivered if k == 0 else 0,
                         "opens": int(opens * share), "clicks": int(clicks * share), "unsubscribes": int(unsubs * share)})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Step K: web events
# ----------------------------------------------------------------------------
UA = {"mobile": ["Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                 "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/138.0 Mobile Safari/537.36"],
      "desktop": ["Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 Safari/605.1.15",
                  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0 Safari/537.36"],
      "tablet": ["Mozilla/5.0 (iPad; CPU OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"]}
PAGES = ["/", "/collections/cookware", "/collections/kitchen-tools", "/collections/storage",
         "/collections/home-decor", "/collections/sale"]


def session_attrs(rng, cfg, n):
    dev = sample_labels(rng, cfg["history"]["web"]["device_mix"], n)
    ua = np.array([UA[d][i % len(UA[d])] for d, i in zip(dev, rng.integers(0, 2, n))], dtype=object)
    octets = rng.integers(1, 255, (n, 4)).astype(str)
    ip = np.char.add(np.char.add(np.char.add(np.char.add(np.char.add(np.char.add(octets[:, 0], "."), octets[:, 1]), "."),
                                             octets[:, 2]), "."), octets[:, 3])
    return dev, ua, ip


def click_fields(platform, campaign_id, ad_id, rng):
    n = len(platform)
    g = platform == "google"
    tok = hex_ids(rng, n, "", 16)
    return {
        "utm_source": np.where(g, "google", "facebook"),
        "utm_medium": np.where(g, "cpc", "paid_social"),
        "utm_campaign": campaign_id, "utm_content": ad_id,
        "gclid": np.where(g, np.char.add("Cj0KCQjw", tok.astype(str)), None),
        "fbclid": np.where(g, None, np.char.add("IwAR", tok.astype(str))),
    }


def events_frame(**cols):
    base = {k: None for k in ["event_id", "event_type", "event_ts", "received_ts", "anonymous_id", "session_id",
                              "customer_id", "utm_source", "utm_medium", "utm_campaign", "utm_content", "gclid",
                              "fbclid", "landing_page", "device_type", "ip_address", "user_agent", "sample_weight"]}
    base.update(cols)
    return pd.DataFrame(base)


def converting_events(rng, cfg, o):
    """Every order after tracking began gets its full path: ad click -> (later) visit -> purchase."""
    o = o[o["path"] != "untracked"].reset_index(drop=True)
    n = len(o)
    dev, ua, ip = session_attrs(rng, cfg, n)
    anon = hex_ids(rng, n, "a_")
    s_buy = hex_ids(rng, n, "s_")
    s_click = hex_ids(rng, n, "s_")
    ots = o["order_ts"].to_numpy()
    is_ad, same = (o["path"] == "ad").to_numpy(), o["same_session"].fillna(False).to_numpy().astype(bool)
    buy_session = np.where(is_ad & same, s_click, s_buy)
    start = np.where(is_ad & same, o["click_ts"].to_numpy(), ots - rng.integers(480, 1500, n).astype("timedelta64[s]"))
    atc = start + rng.integers(60, 300, n).astype("timedelta64[s]")
    chk = ots - rng.integers(60, 180, n).astype("timedelta64[s]")
    page = np.array(PAGES, dtype=object)[rng.integers(0, len(PAGES), n)]
    frames = []

    # 1) Ad click landing (ad paths only).
    ad = np.flatnonzero(is_ad)
    cf = click_fields(o["platform"].to_numpy()[ad], o["campaign_id"].to_numpy()[ad], o["ad_id"].to_numpy()[ad], rng)
    frames.append(events_frame(event_type="landing", event_ts=o["click_ts"].to_numpy()[ad], anonymous_id=anon[ad],
                               session_id=s_click[ad], landing_page=page[ad], device_type=dev[ad], ip_address=ip[ad],
                               user_agent=ua[ad], **cf))
    # 2) Landing of the buying visit (not needed when the purchase happens in the click visit).
    sep = np.flatnonzero(~(is_ad & same))
    email = (o["path"].to_numpy()[sep] == "email")
    frames.append(events_frame(event_type="landing", event_ts=start[sep], anonymous_id=anon[sep], session_id=s_buy[sep],
                               landing_page=page[sep], device_type=dev[sep], ip_address=ip[sep], user_agent=ua[sep],
                               utm_source=np.where(email, "email", None), utm_medium=np.where(email, "email", None),
                               utm_campaign=np.where(email, o["email_campaign_id"].to_numpy()[sep], None)))
    # 3) Add to cart, checkout, purchase in the buying session (logged in from checkout onward).
    cust = o["shop_customer_id"].to_numpy()
    for etype, t, logged in [("add_to_cart", atc, False), ("checkout_start", chk, True), ("purchase", ots, True)]:
        frames.append(events_frame(event_type=etype, event_ts=t, anonymous_id=anon, session_id=buy_session,
                                   customer_id=cust if logged else None, device_type=dev, ip_address=ip,
                                   user_agent=ua))
    ev = pd.concat(frames, ignore_index=True)
    ev["sample_weight"] = 1.0
    return ev


def nonconverting_events(rng, cfg, ad_days, conv_counts, month_start, month_end):
    """A sample of sessions that didn't buy: ad landings and organic visits. Each carries a weight."""
    w = cfg["history"]["web"]
    rate = w["non_converting_sample_rate"]
    econ = cfg["history"]["channel_economics"]
    frames = []

    a = ad_days[(ad_days["local_date"] >= month_start) & (ad_days["local_date"] <= month_end)]
    a = a.merge(conv_counts, on=["ad_id", "local_date"], how="left").fillna({"conv_sessions": 0})
    landings = np.round(a["clicks"] * a["channel"].map({k: v["landing_rate"] for k, v in econ.items()}))
    n = rng.binomial(np.maximum(landings - a["conv_sessions"], 0).astype(np.int64), rate)
    rep = a.loc[a.index.repeat(n)].reset_index(drop=True)
    if len(rep):
        local = random_times_in_day(rng, rep["local_date"], w["hour_weights"])
        ts = np.empty(len(rep), dtype="datetime64[ns]")
        for plat, tz in TZ.items():
            m = (rep["platform"] == plat).to_numpy()
            ts[m] = local_to_utc(local[m], tz)
        dev, ua, ip = session_attrs(rng, cfg, len(rep))
        cf = click_fields(rep["platform"].to_numpy(), rep["campaign_id"].to_numpy(), rep["ad_id"].to_numpy(), rng)
        frames.append(events_frame(event_type="landing", event_ts=ts, anonymous_id=hex_ids(rng, len(rep), "a_"),
                                   session_id=hex_ids(rng, len(rep), "s_"),
                                   landing_page=np.array(PAGES, dtype=object)[rng.integers(0, len(PAGES), len(rep))],
                                   device_type=dev, ip_address=ip, user_agent=ua, **cf))

    days = pd.date_range(month_start, month_end, freq="D")
    years = (days - pd.Timestamp(cfg["dates"]["ads_start"])).days / 365.25
    level = (w["organic_sessions_per_day"] * cfg["scale"] * (1 + cfg["history"]["yearly_growth"]) ** years
             * smooth_seasonality(days, cfg["customers"]["monthly_seasonality"]))
    n = rng.binomial(np.round(level).astype(np.int64), rate)
    dates = np.repeat(days.to_numpy(), n)
    if len(dates):
        ts = local_to_utc(random_times_in_day(rng, dates, w["hour_weights"]), ET)
        dev, ua, ip = session_attrs(rng, cfg, len(dates))
        frames.append(events_frame(event_type="landing", event_ts=ts, anonymous_id=hex_ids(rng, len(dates), "a_"),
                                   session_id=hex_ids(rng, len(dates), "s_"),
                                   landing_page=np.array(PAGES, dtype=object)[rng.integers(0, len(PAGES), len(dates))],
                                   device_type=dev, ip_address=ip, user_agent=ua))
    if not frames:
        return None
    ev = pd.concat(frames, ignore_index=True)
    # A few non-buyers add something to cart before leaving.
    atc = ev[rng.random(len(ev)) < w["add_to_cart_rate_non_converting"]].copy()
    atc["event_type"] = "add_to_cart"
    atc["event_ts"] = atc["event_ts"].to_numpy() + rng.integers(60, 600, len(atc)).astype("timedelta64[s]")
    atc[["utm_source", "utm_medium", "utm_campaign", "utm_content", "gclid", "fbclid", "landing_page"]] = None
    ev = pd.concat([ev, atc], ignore_index=True)
    ev["sample_weight"] = 1.0 / rate
    return ev


def finish_events(rng, ev):
    ev["event_id"] = hex_ids(rng, len(ev), "evt_")
    ev["received_ts"] = ev["event_ts"].to_numpy() + rng.integers(200, 3000, len(ev)).astype("timedelta64[ms]")
    return ev.sort_values("event_ts", kind="stable").reset_index(drop=True)



# ----------------------------------------------------------------------------
# Ground-truth log: what Pass 2 does to the data that an evaluator needs to know
# ----------------------------------------------------------------------------
def append_issue_log(out, cfg, orders, master, ad_days, cd, kwd, auc):
    """Adds Pass 2 entries to the ground-truth log. Re-runnable: earlier Pass 2 entries are replaced."""
    path = out / "ground_truth/injected_issues.jsonl"
    rows = [r for r in (json.loads(l) for l in open(path)) if r.get("pass") not in (2, 3)]   # Pass 3 is re-applied after

    def add(issue_type, source, description, questions, start=None, end=None, entity_ids=None):
        rows.append({"issue_id": f"ISSUE-{len(rows) + 1:03d}", "issue_type": issue_type, "source": source,
                     "entity_ids": [str(e) for e in (entity_ids if entity_ids is not None else [])],
                     "start_date": start, "end_date": end, "description": description,
                     "benchmark_questions": questions, "pass": 2})

    h = cfg["history"]
    pc = ad_days.groupby("platform").agg(fp=("fp_orders", "sum"), reported=("platform_conversions", "sum"))
    ratio = (pc["reported"] / pc["fp"]).round(2).to_dict()
    add("platform_overcount", "google_ads,meta_ads",
        f"Platform-reported conversions exceed first-party orders: Google about {ratio['google']}x, Meta about "
        f"{ratio['meta']}x overall (Meta {h['platform_overcount']['meta_before_change']}x before "
        f"{cfg['planted']['attribution_change_date']}, {h['platform_overcount']['meta_after_change']}x after). "
        "First-party orders are the certified number.", [30, 35], start=str(cfg["planted"]["attribution_change_date"]))
    add("sampled_web_sessions", "web_events",
        f"Every buying path is kept; {h['web']['non_converting_sample_rate']:.0%} of non-buying sessions are kept with "
        "sample_weight = 1/rate. Session counts and session conversion rate require SUM(sample_weight). See ADR 0002.",
        [11])
    add("untracked_history", "shop",
        f"Orders before {cfg['dates']['ads_start']} have no web events, sessions, or ad attribution "
        "(path = 'untracked' in ground truth).", [])
    add("conversion_lag", "shop,web_events",
        f"Ad-driven orders arrive up to {h['later_lag_days']['max']} days after the click, so the most recent days look "
        "under-converted until late orders arrive. Hourly or same-day ROAS is unreliable.", [40])
    add("refund_lag", "shop",
        f"Refunds arrive 2-{h['refunds']['max_days']} days after purchase (about {h['refunds']['rate']:.0%} of orders). "
        "Net revenue for the most recent month is overstated until refunds arrive; refunds past the extract date are "
        "saved in state/pending_refunds.parquet for the live stream.", [31, 18, 20])

    # Q23: measure how much of the July -> August 2026 decline the paused campaigns actually explain.
    ad = orders[orders["path"] == "ad"]
    ts = ad["order_ts"]
    jul = ad[(ts >= "2026-07-01") & (ts < "2026-08-01")].groupby("campaign_id")["net_sales"].sum()
    aug = ad[(ts >= "2026-08-01") & (ts < "2026-09-01")].groupby("campaign_id")["net_sales"].sum()
    idx = jul.index.union(aug.index)
    delta = aug.reindex(idx).fillna(0) - jul.reindex(idx).fillna(0)
    paused = master.loc[master["paused"], "campaign_id"].astype(str)
    if delta.sum() < 0:
        share = delta[delta.index.isin(paused)].sum() / delta.sum()
        add("q23_decomposition", "google_ads,meta_ads,shop",
            f"Ad-attributed net sales fell ${-delta.sum():,.0f} from July to August 2026. The {len(paused)} campaigns "
            f"paused on {cfg['planted']['paused_campaigns']['pause_date']} explain {share:.0%} of it; scheduled campaign "
            "expiries and the end of the Summer Sale explain the rest. A correct diagnosis separates early pauses "
            "(status PAUSED, planned end date still in the future) from campaigns that ended on schedule.",
            [23], start="2026-07-01", end="2026-08-31", entity_ids=paused)
    # Q22: measure what the planted CPC spike actually did to the data.
    sp = cfg["planted"]["cpc_spike"]
    s0, s1 = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"])
    p0, p1 = s0 - pd.Timedelta(days=30), s0 - pd.Timedelta(days=1)          # the 30 days before
    hit_ids = cd.loc[cd["spike_active"], "campaign_id"].unique()
    kd = kwd.assign(d=pd.to_datetime(kwd["date"]))
    head = kd[kd["keyword_text"].isin(cfg["planted"]["spike_keywords"])]
    def cpc_of(df, a, b):
        x = df[(df["d"] >= a) & (df["d"] <= b)]
        return x["cost_micros"].sum() / 1e6 / x["clicks"].sum()
    cpc_ratio = cpc_of(head, s0, s1) / cpc_of(head, p0, p1)
    srch = cd[cd["channel"] == "SEARCH"]
    def window(a, b):
        return srch[(srch["local_date"] >= a) & (srch["local_date"] <= b)]
    def cpa(a, b):
        sub = window(a, b)
        o = orders[(orders["path"] == "ad") & (orders["channel"] == "SEARCH") & (orders["order_ts"] >= a)
                   & (orders["order_ts"] < b + pd.Timedelta(days=1))]
        return sub["spend"].sum() / len(o)
    aff = cd[cd["campaign_id"].isin(hit_ids)]
    def ctr(a, b):
        x = aff[(aff["local_date"] >= a) & (aff["local_date"] <= b)]
        return x["clicks"].sum() / x["impressions"].sum()
    au = auc[auc["campaign_id"].isin(hit_ids)].assign(d=lambda x: pd.to_datetime(x["date"]))
    imp = lambda a, b: au[(au["d"] >= a) & (au["d"] <= b)]["impression_share"].mean()
    add("cpc_spike", "google_ads",
        f"Competitors bid up the head terms {', '.join(cfg['planted']['spike_keywords'])} across {len(hit_ids)} search "
        f"campaigns in July 2026: their CPC rose {cpc_ratio:.1f}x versus the prior 30 days. Budgets are fixed, so clicks "
        f"fell and first-party search CPA rose {cpa(s0, s1) / cpa(p0, p1) - 1:.0%}, while CTR ({ctr(s0, s1) / ctr(p0, p1):.2f}x) "
        f"held steady and impression share fell from {imp(p0, p1):.0%} to {imp(s0, s1):.0%} (overlap rate up, outranking "
        "share down). The Summer Sale (Jul 6-19) lifts conversion and partly masks the CPA effect.",
        [22], start=sp["start"], end=sp["end"], entity_ids=hit_ids)

    # Q33 and Q49: planted orders.
    pl = orders[orders["planted"] == "tiny_bulk"].iloc[0]
    tiny_ids = master.loc[master["kind"] == "tiny", "campaign_id"].astype(str)
    tiny_spend = cd[cd["campaign_id"].isin(tiny_ids)]["spend"].sum()
    tiny_sales = orders[orders["campaign_id"].isin(tiny_ids)]["net_sales"].sum()
    add("tiny_campaign_bulk_order", "google_ads,shop",
        f"Order {int(pl['order_id'])} (${pl['net_sales']:,.0f} net, {int(cfg['planted']['planted_orders']['tiny_bulk']['quantity'])} "
        f"units of one product) is attributed to the ~$8/day clearance campaign. Campaign ROAS over its life is "
        f"{tiny_sales / tiny_spend:.1f} on ${tiny_spend:,.0f} of spend, versus about {cfg['history']['target_roas']} for the "
        "business. One large order on tiny spend is not a signal; apply a minimum-spend threshold.",
        [33], start=str(pl["order_ts"].date()), end=str(pl["order_ts"].date()),
        entity_ids=[int(pl["order_id"])] + list(tiny_ids))
    sc_orders = orders[orders["planted"] == "small_cell"]
    add("small_cell_skillet_orders", "shop,crm",
        f"{len(sc_orders)} of the {cfg['customers']['small_cell_count']} customers in ZIP {cfg['customers']['small_cell_zip']} "
        f"bought a cast-iron skillet on {cfg['planted']['planted_orders']['small_cell']['date']}. Revenue for that group, "
        "product and day must be suppressed (group below the minimum size), not reported and not returned as zero.",
        [49], start=cfg["planted"]["planted_orders"]["small_cell"]["date"],
        end=cfg["planted"]["planted_orders"]["small_cell"]["date"],
        entity_ids=list(sc_orders["shop_customer_id"]) + [int(x) for x in sc_orders["order_id"]])

    with open(path, "w") as fh:
        fh.writelines(json.dumps(r, default=str) + "\n" for r in rows)
    return len(rows)

# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="generator/config.yaml")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config))
    if args.scale is not None:
        cfg["scale"] = args.scale
    out = Path(args.out)
    rng = np.random.default_rng(cfg["seed"] + 1000)   # separate stream from Pass 1

    # Start clean: remove any earlier Pass 2 output so stale files can't mix in.
    for sub in ["raw/google_ads/ad_perf_daily", "raw/google_ads/keyword_perf_daily", "raw/google_ads/auction_insights_daily",
                "raw/meta_ads/insights", "raw/email_platform/daily_metrics", "raw/shop/orders", "raw/shop/order_items",
                "raw/shop/refunds", "raw/web_events", "raw/meta_ads/insights_restated"]:
        shutil.rmtree(out / sub, ignore_errors=True)
    (out / "state/incidents_applied.json").unlink(missing_ok=True)    # Pass 3 must be re-applied to fresh raw files
    master, ads, kw, products, truth, emails = load_reference(out)
    # Guard: Pass 1 must have been generated at the same scale.
    expected_customers = int(cfg["customers"]["total"] * cfg["scale"]) + int(cfg["customers"]["legacy_total"] * cfg["scale"])
    assert len(truth) == expected_customers, "Run reference.py with the same --scale first"
    print("A. campaign-days ...")
    cd = make_campaign_days(rng, cfg, master, kw)
    print("B. ad-driven orders ...")
    ad_it, ad_w, k, aov = make_ad_intents(rng, cfg, cd, ads, products)
    print("C/D. email + organic orders ...")
    em_it = make_email_intents(rng, cfg, emails)
    org_it = make_organic_intents(rng, cfg, ad_it, em_it)
    intents = pd.concat([ad_it, em_it, org_it], ignore_index=True)
    extract_end = np.datetime64(pd.Timestamp(cfg["dates"]["extract_date"]) + pd.Timedelta(days=1))
    late = intents["order_ts"].to_numpy() >= extract_end               # arrive after the extract: live stream
    pending_intents = intents[late].copy()
    pending_intents["same_session"] = pending_intents["same_session"].fillna(False).astype(bool)
    intents = intents[~late]
    print(f"E. assigning {len(intents):,} orders to customers ...")
    orders, astats = assign_customers(rng, cfg, intents, truth)
    # Tracking starts at ads_start in UTC; label orders by their actual timestamp.
    pre = orders["order_ts"] < pd.Timestamp(cfg["dates"]["ads_start"])
    orders.loc[pre & (orders["path"] == "organic"), "path"] = "untracked"
    orders.loc[~pre & (orders["path"] == "untracked"), "path"] = "organic"
    orders = plant_orders(rng, cfg, out, orders, master, ad_w, truth, products)
    print("F/G. baskets, financials, refunds ...")
    orders, items, refunds, pending_refunds = make_orders(rng, cfg, orders, products, truth)
    print("H. ad-platform reports ...")
    ad_days = split_to_ads(rng, cd, ad_w)
    ad_days = ad_days.merge(cd[["campaign_id", "channel"]].drop_duplicates(), on="campaign_id")
    ad_days = platform_conversions(rng, cfg, ad_days, orders)
    write_google(out, ad_days)
    write_meta(out, ad_days)
    print("I/J. keywords, auction insights, email metrics ...")
    kwd = make_keywords(rng, cfg, cd, kw)
    auc = make_auction(rng, cd)
    for name, df in [("keyword_perf_daily", kwd.drop(columns="campaign_id")), ("auction_insights_daily", auc)]:
        folder = out / f"raw/google_ads/{name}"
        folder.mkdir(parents=True, exist_ok=True)
        for date, grp in df.groupby("date"):
            grp.to_csv(folder / f"date={date}.csv", index=False)
    emails["delivered"] = np.round(emails["recipients"] * cfg["history"]["email"]["delivery_rate"]).astype(int)
    edaily = make_email_daily(rng, cfg, emails)
    folder = out / "raw/email_platform/daily_metrics"
    folder.mkdir(parents=True, exist_ok=True)
    for date, grp in edaily.groupby("date"):
        grp.to_csv(folder / f"date={date}.csv", index=False)

    # Shop exports (backfill as monthly Parquet; the live stream continues from extract date).
    print("   writing orders ...")
    orders["month"] = orders["order_ts"].dt.strftime("%Y-%m")
    items = items.merge(orders[["order_id", "month"]], on="order_id")
    refunds["month"] = refunds["refund_ts"].dt.strftime("%Y-%m")
    shop_cols = ["order_id", "shop_customer_id", "session_id", "order_ts", "subtotal", "shipping", "discount", "tax",
                 "total", "discount_code", "status"]
    orders["status"] = "paid"
    orders["session_id"] = None   # filled from web events below
    print("K. web events ...")
    ts = orders["order_ts"].dt
    ad_mask = orders["path"] == "ad"
    conv_all = orders.copy()
    ev_folder = out / "raw/web_events/backfill"
    months = pd.period_range(cfg["dates"]["ads_start"], cfg["dates"]["extract_date"], freq="M")
    session_map = []
    n_events = 0
    # Local-date conversion counts per ad, so non-converting sessions = landings - converting ones.
    conv = conv_all[ad_mask].copy()
    conv["local_date"] = pd.NaT
    for plat, tz in TZ.items():
        m = (conv["platform"] == plat).to_numpy()
        conv.loc[m, "local_date"] = utc_to_local_date(conv.loc[m, "click_ts"].to_numpy(), tz)
    conv["local_date"] = pd.to_datetime(conv["local_date"])
    conv_counts = conv.groupby(["ad_id", "local_date"]).size().rename("conv_sessions").reset_index()
    for p in months:
        ms, me = p.start_time, p.end_time.normalize()
        chunk = conv_all[(conv_all["order_ts"] >= ms) & (conv_all["order_ts"] < me + pd.Timedelta(days=1))]
        ev = converting_events(rng, cfg, chunk)
        purchase = ev[ev["event_type"] == "purchase"][["session_id", "customer_id", "event_ts"]]
        session_map.append(purchase)
        nc = nonconverting_events(rng, cfg, ad_days, conv_counts, ms, me)
        ev = finish_events(rng, pd.concat([ev, nc], ignore_index=True) if nc is not None else ev)
        ev["event_month"] = ev["event_ts"].dt.strftime("%Y-%m")
        for em, grp in ev.groupby("event_month"):
            folder = ev_folder / f"event_month={em}"
            folder.mkdir(parents=True, exist_ok=True)
            write_parquet(grp, folder / f"part-{p}.parquet", "web_events")
        n_events += len(ev)
        print(f"   {p}: {len(ev):,} events")

    # Link each tracked order to its purchase session (exact match on customer + timestamp).
    sm = pd.concat(session_map, ignore_index=True).rename(columns={"customer_id": "shop_customer_id",
                                                                     "event_ts": "order_ts", "session_id": "sid"})
    orders = orders.merge(sm, on=["shop_customer_id", "order_ts"], how="left")
    orders["session_id"] = orders["sid"]
    for name, df, cols in [("orders", orders, shop_cols + ["month"]),
                           ("order_items", items, ["order_id", "product_id", "quantity", "unit_price", "month"]),
                           ("refunds", refunds, ["refund_id", "order_id", "refund_ts", "amount", "reason", "month"])]:
        folder = out / f"raw/shop/{name}/backfill"
        folder.mkdir(parents=True, exist_ok=True)
        for mo, grp in df[cols].groupby("month"):
            write_parquet(grp, folder / f"month={mo}.parquet", name)

    # Ground truth for scoring (never loaded into Snowflake).
    truth_cols = ["order_id", "shop_customer_id", "order_ts", "customer_type", "path", "campaign_id", "ad_id",
                  "platform", "click_ts", "same_session", "email_campaign_id", "net_sales", "planted"]
    orders[truth_cols].to_parquet(out / "state/order_truth.parquet", index=False)
    cd.to_parquet(out / "state/campaign_days.parquet", index=False)
    pending_refunds.to_parquet(out / "state/pending_refunds.parquet", index=False)
    pending_intents.to_parquet(out / "state/pending_order_intents.parquet", index=False)

    n_issues = append_issue_log(out, cfg, orders, master, ad_days, cd, kwd, auc)

    # ---- audit summary -----------------------------------------------------
    print("\n=== AUDIT SUMMARY (Pass 2) ===")
    print(f"Ground-truth log: {n_issues} entries after Pass 2")
    tracked = orders[orders["order_ts"] >= pd.Timestamp(cfg["dates"]["ads_start"])]
    spend = cd["spend"].sum()
    ad_sales = orders.loc[orders["path"] == "ad", "net_sales"].sum()
    months_n = cd["local_date"].dt.to_period("M").nunique()
    print(f"Calibration: CVR scale k={k:.4f}, estimated net AOV ${aov:.2f}")
    print(f"Media spend: ${spend:,.0f} total, ${spend / months_n:,.0f}/month avg")
    print(f"Blended first-party ROAS: {ad_sales / spend:.2f} (target {cfg['history']['target_roas']})")
    print(f"Orders: {len(orders):,} total; {len(tracked):,} since tracking began "
          f"({len(tracked) / ((pd.Timestamp(cfg['dates']['extract_date']) - pd.Timestamp(cfg['dates']['ads_start'])).days + 1):,.0f}/day)")
    print("  By path (tracked period): " + ", ".join(f"{k}={v:.0%}" for k, v in tracked["path"].value_counts(normalize=True).items()))
    print("  By customer type (tracked): " + ", ".join(f"{k}={v:.0%}" for k, v in tracked["customer_type"].value_counts(normalize=True).items()))
    print(f"  Assignment: {astats['fallback']:,} re-routed to another customer type, {astats['dropped']:,} dropped "
          f"(no eligible customer); {astats['moved_to_later_visit']:,} same-session purchases moved to a later visit")
    yearly = orders.groupby(orders["order_ts"].dt.year)["net_sales"].sum()
    print("  Net sales by year: " + ", ".join(f"{y}: ${v / 1e6:,.1f}M" for y, v in yearly.items()))
    print(f"  AOV (net): ${orders['net_sales'].mean():.2f}; refunds: {len(refunds) / len(orders):.1%} of orders")
    by_ch = (orders[orders["path"] == "ad"].groupby("channel")["net_sales"].sum()
             / cd.groupby("channel")["spend"].sum())
    print("  ROAS by channel: " + ", ".join(f"{c}={v:.2f}" for c, v in by_ch.sort_values(ascending=False).items()))
    pc = ad_days.groupby("platform").agg(fp=("fp_orders", "sum"), pc=("platform_conversions", "sum"))
    print("  Platform-reported vs first-party conversions: " +
          ", ".join(f"{p}={r.pc / r.fp:.2f}x" for p, r in pc.iterrows()))
    print(f"Ad-day rows: {len(ad_days):,}; keyword-day rows: {len(kwd):,}; email sends: {len(emails)}")
    print(f"Web events: {n_events:,}; tracked orders linked to a session: "
          f"{tracked['session_id'].notna().mean():.1%}")


if __name__ == "__main__":
    main()
