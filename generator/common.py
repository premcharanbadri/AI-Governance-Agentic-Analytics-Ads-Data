"""Helpers shared by the generator passes."""
import numpy as np
import pandas as pd


def smooth_seasonality(dates, monthly):
    """Daily seasonality factor from a {month: factor} table.

    A plain month lookup makes the factor jump at every month boundary (for example +48% on
    Nov 1). Instead, each month's factor applies at mid-month and days in between are linearly
    interpolated, so the pattern ramps up and down like real demand does.
    """
    d = pd.DatetimeIndex(dates)
    pos = (d.day.to_numpy() - 0.5) / d.days_in_month.to_numpy()      # 0..1 through the month
    cur = np.array([monthly[m] for m in d.month], dtype=float)
    prev = np.array([monthly[(m - 2) % 12 + 1] for m in d.month], dtype=float)
    nxt = np.array([monthly[m % 12 + 1] for m in d.month], dtype=float)
    return np.where(pos < 0.5, prev + (cur - prev) * (pos + 0.5), cur + (nxt - cur) * (pos - 0.5))


def rewrite_log(path, pass_no, entries):
    """Replace one pass's entries in the ground-truth log (safe to re-run) and keep issue IDs sequential.

    Each entry is a dict with at least issue_type, source, description, benchmark_questions.
    """
    import json
    rows = [r for r in (json.loads(line) for line in open(path)) if r.get("pass") != pass_no]
    for e in entries:
        rows.append({"issue_id": f"ISSUE-{len(rows) + 1:03d}", "entity_ids": [], "start_date": None,
                     "end_date": None, **e, "pass": pass_no})
    with open(path, "w") as fh:
        fh.writelines(json.dumps(r, default=str) + "\n" for r in rows)
    return len(rows)
