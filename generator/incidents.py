"""
Lumen Goods synthetic data generator — Pass 3: incidents in how data is delivered.

Pass 2 already planted the incidents that change what *happened* (the July CPC spike, the bulk order,
the small-cell orders), because those must flow through spend, clicks and orders consistently.
This pass plants the ones that change what the raw files *say*, without changing what happened:

  1. Tracking outage   paid-social landing events lose their UTM tags and click IDs (Q21, Q32, Q34)
  2. Delivery problems duplicate and late-arriving web events (pipeline tests)
  3. Meta restatements Meta's first extract for a day is provisional; final values arrive 3 days later
  4. Schema change     Meta renames `spend` to `amount_spent` mid-stream (Q39)

The orders, spend and clicks in the ground truth are untouched, so the right answers stay knowable.

Run after history.py:  python generator/incidents.py --out data
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

from common import rewrite_log
from history import write_parquet

ET = "America/New_York"


def eastern_window_utc(start, end):
    """[start, end] are Eastern calendar dates; returns the matching half-open UTC interval."""
    a = pd.Timestamp(start).tz_localize(ET).tz_convert("UTC").tz_localize(None)
    b = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(ET).tz_convert("UTC").tz_localize(None)
    return a, b


def web_files(raw):
    return sorted((raw / "web_events/backfill").glob("event_month=*/*.parquet"))


# ----------------------------------------------------------------------------
# 1. Tracking outage
# ----------------------------------------------------------------------------
def strip_tracking(cfg, raw):
    to = cfg["planted"]["tracking_outage"]
    a, b = eastern_window_utc(to["start"], to["end"])
    stripped = 0
    for f in web_files(raw):
        df = pq.read_table(f).to_pandas()
        m = ((df["event_type"] == "landing") & (df["utm_source"] == "facebook")
             & (df["event_ts"] >= a) & (df["event_ts"] < b))
        if m.any():
            df.loc[m, ["utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid"]] = None
            write_parquet(df, f, "web_events")
            stripped += int(m.sum())
    return stripped, a, b


# ----------------------------------------------------------------------------
# 2. Duplicate and late-arriving events
# ----------------------------------------------------------------------------
def delivery_problems(cfg, raw, rng):
    ed = cfg["planted"]["event_delivery"]
    tot = dup_n = late_n = dup_buy = 0
    for f in web_files(raw):
        df = pq.read_table(f).to_pandas()
        n = len(df)
        dup = rng.random(n) < ed["duplicate_rate"]
        late = (~dup) & (rng.random(n) < ed["late_rate"])
        hours = np.minimum(rng.lognormal(np.log(ed["late_hours_median"]), ed["late_hours_sigma"], int(late.sum())),
                           ed["late_max_days"] * 24)
        if late.any():
            delayed = df.loc[late, "event_ts"].to_numpy() + (hours * 3600).astype("timedelta64[s]")
            df.loc[late, "received_ts"] = delayed.astype(df["received_ts"].dtype)
        d = df[dup].copy()                                   # a retry: same event_id, same payload, arrives later
        d["received_ts"] = d["received_ts"] + pd.to_timedelta(rng.integers(5, 600, len(d)), unit="s")
        df = pd.concat([df, d], ignore_index=True).sort_values("received_ts", kind="stable").reset_index(drop=True)
        write_parquet(df, f, "web_events")
        tot += n; dup_n += len(d); late_n += int(late.sum()); dup_buy += int((d["event_type"] == "purchase").sum())
    return tot, dup_n, late_n, dup_buy


# ----------------------------------------------------------------------------
# 3 + 4. Meta: provisional first extract, restated finals, and a field rename
# ----------------------------------------------------------------------------
def meta_versions(cfg, raw, rng):
    mr, sc = cfg["planted"]["meta_restatement"], cfg["planted"]["schema_change"]
    snapshot = pd.Timestamp(cfg["dates"]["extract_date"]) + pd.Timedelta(days=1, hours=10)   # the morning after the last day
    change_at = pd.Timestamp(sc["date"]) + pd.Timedelta(hours=12)
    out_dir = raw / "meta_ads/insights_restated"
    out_dir.mkdir(parents=True, exist_ok=True)
    st = {"initial_rows": 0, "restated_rows": 0, "renamed_rows": 0, "conv_factors": [], "provisional_days": []}

    def stamp(row, at):
        r = dict(row, extracted_at=at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if at >= change_at:                                   # the API now uses the new field name
            r = {(sc["new_field"] if k == sc["old_field"] else k): v for k, v in r.items()}
            st["renamed_rows"] += 1
        return r

    for f in sorted((raw / "meta_ads/insights").glob("date=*.jsonl")):
        day = pd.Timestamp(f.stem.split("=")[1])
        final = [json.loads(line) for line in open(f)]
        initial_at, final_at = day + pd.Timedelta(days=1, hours=10), day + pd.Timedelta(days=mr["days"], hours=12)
        initial = []
        for row in final:
            fc = rng.uniform(*mr["initial_conversion_factor"])
            fs = rng.uniform(*mr["initial_spend_factor"])
            r = json.loads(json.dumps(row))
            r["spend"] = f"{float(row['spend']) * fs:.2f}"
            for a in r["actions"]:
                if a["action_type"] == "purchase":
                    a["value"] = str(int(round(int(a["value"]) * fc)))
            for a in r["action_values"]:
                a["value"] = f"{float(a['value']) * fc:.2f}"
            initial.append(stamp(r, initial_at))
            st["conv_factors"].append(fc)
        with open(f, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in initial)
        st["initial_rows"] += len(initial)
        if final_at <= snapshot:
            with open(out_dir / f.name, "w") as fh:
                fh.writelines(json.dumps(stamp(row, final_at)) + "\n" for row in final)
            st["restated_rows"] += len(final)
        else:
            st["provisional_days"].append(day.strftime("%Y-%m-%d"))
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="generator/config.yaml")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    out = Path(a.out)
    raw, marker = out / "raw", out / "state/incidents_applied.json"
    if marker.exists():
        raise SystemExit("Incidents are already applied to this data. Re-run history.py first to start from clean files.")
    rng = np.random.default_rng(cfg["seed"] + 3000)
    truth = pd.read_parquet(out / "state/order_truth.parquet")
    pl = cfg["planted"]

    print("1. tracking outage ...")
    stripped, w0, w1 = strip_tracking(cfg, raw)
    hit = truth[(truth["platform"] == "meta") & (truth["click_ts"] >= w0) & (truth["click_ts"] < w1)]
    print("2. duplicate and late web events ...")
    tot, dup_n, late_n, dup_buy = delivery_problems(cfg, raw, rng)
    print("3/4. Meta restatements and schema change ...")
    st = meta_versions(cfg, raw, rng)

    to, sc = pl["tracking_outage"], pl["schema_change"]
    avg_fc = float(np.mean(st["conv_factors"]))
    entries = [
        {"issue_type": "tracking_outage", "source": "web_events",
         "description": f"From {to['start']} to {to['end']} (Eastern), {stripped:,} paid-social landing events lost their UTM tags and "
                        f"click IDs. Spend, clicks and Meta-reported conversions are normal, but {len(hit):,} orders "
                        f"(${hit['net_sales'].sum():,.0f} net) from clicks in that window can no longer be attributed from first-party "
                        "data, so first-party paid-social ROAS collapses while platform-reported ROAS does not. Total orders are "
                        "unchanged; the revenue moves to 'unattributed'.",
         "benchmark_questions": [21, 32, 34], "start_date": str(to["start"]), "end_date": str(to["end"])},
        {"issue_type": "meta_schema_change", "source": "meta_ads",
         "description": f"From {sc['date']} the Meta API returns `{sc['new_field']}` instead of `{sc['old_field']}`. Every extract taken on "
                        f"or after {sc['date']} 12:00 UTC uses the new name ({st['renamed_rows']:,} rows), including restatements of "
                        f"Aug 31 to Sep 2. A pipeline that maps only `{sc['old_field']}` gets NULL spend from {sc['date']} onward "
                        "and its not_null test should fail.",
         "benchmark_questions": [39], "start_date": str(sc["date"])},
        {"issue_type": "meta_restatement", "source": "meta_ads",
         "description": f"Meta's first extract for a day (next morning) understates conversions (on average {1 - avg_fc:.0%}) and spend "
                        f"by up to {1 - pl['meta_restatement']['initial_spend_factor'][0]:.1%}; final values arrive {pl['meta_restatement']['days']} "
                        f"days later in insights_restated/ ({st['restated_rows']:,} rows). The latest extracted_at wins. "
                        f"{', '.join(st['provisional_days'])} have only provisional values at the as-of date.",
         "benchmark_questions": [24, 29], "start_date": st["provisional_days"][0] if st["provisional_days"] else None},
        {"issue_type": "web_event_delivery", "source": "web_events",
         "description": f"{dup_n:,} retry duplicates ({dup_n / tot:.2%} of {tot:,} events; same event_id, later received_ts; "
                        f"{dup_buy:,} are purchase events) and {late_n:,} late-arriving events ({late_n / tot:.2%}; received up to "
                        f"{pl['event_delivery']['late_max_days']} days after they happened). Deduplicate on event_id before counting; "
                        "use event_ts, not received_ts, for time.",
         "benchmark_questions": []},
    ]
    n = rewrite_log(out / "ground_truth/injected_issues.jsonl", 3, entries)
    marker.write_text(json.dumps({"stripped_landings": stripped, "orders_unattributed": int(len(hit)),
                                  "duplicates": dup_n, "late": late_n, "meta_initial": st["initial_rows"],
                                  "meta_restated": st["restated_rows"], "meta_renamed": st["renamed_rows"]}, indent=2))
    print(f"\n=== PASS 3 SUMMARY ===\nGround-truth log: {n} entries")
    print(f"Tracking outage: {stripped:,} landing events stripped; {len(hit):,} orders (${hit['net_sales'].sum():,.0f}) lose first-party attribution")
    print(f"Web events: {tot:,} originals; {dup_n:,} duplicates ({dup_n / tot:.2%}); {late_n:,} late ({late_n / tot:.2%})")
    print(f"Meta: {st['initial_rows']:,} provisional rows, {st['restated_rows']:,} restated rows, {st['renamed_rows']:,} rows with the new field name; "
          f"provisional days: {', '.join(st['provisional_days'])}")


if __name__ == "__main__":
    main()
