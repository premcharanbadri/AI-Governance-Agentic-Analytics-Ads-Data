"""Control tests for the governance layer. No LLM involved: each control is attacked directly, the way an
auditor tests a control, and the results are written to governance/reports/control_findings.md.

Run after building the warehouse:   python governance/control_tests.py
Exit code is 1 if any test fails, so CI can run it.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway import ROOT, SUPPRESSED, PolicyGateway, load_config  # noqa: E402

RUN_ID = uuid.uuid4().hex[:8]
LOG = ROOT / "governance" / "logs" / f"control_tests_{RUN_ID}.jsonl"
REPORT = ROOT / "governance" / "reports" / "control_findings.md"
results: list[dict] = []


def record(test_id, risk, control, ctype, test, checks: list[tuple[str, bool, str]]):
    """checks: (attempt, passed, evidence)."""
    ok = all(c[1] for c in checks)
    results.append({"id": test_id, "risk": risk, "control": control, "type": ctype, "test": test,
                    "result": "PASS" if ok else "FAIL", "checks": checks})


def denied(r: dict) -> bool:
    return r.get("status") == "denied"


def main():
    cfg = load_config()
    analyst = PolicyGateway("analyst", session_id=f"ct-{RUN_ID}-analyst", audit_log=LOG)
    eng = PolicyGateway("data_engineer", session_id=f"ct-{RUN_ID}-engineer", audit_log=LOG)
    k = analyst.k
    calls = 0

    def call(gw, tool, args):
        nonlocal calls
        calls += 1
        return gw.call(tool, args)

    # CT-01 database-enforced read-only (bypasses the gateway on purpose: the database itself must refuse)
    checks = []
    for stmt in ["CREATE TABLE staging.ct_probe AS SELECT 1 AS x",
                 "DELETE FROM staging.channel_map",
                 "UPDATE staging.channel_map SET channel = 'X'",
                 "INSERT INTO staging.channel_map VALUES ('a', 'b')",
                 "ATTACH '/tmp/ct_probe.duckdb' AS probe",
                 "SET enable_external_access = true"]:
        try:
            eng.con.execute(stmt)
            checks.append((stmt, False, "executed (control failed)"))
        except duckdb.Error as e:
            checks.append((stmt, True, str(e).splitlines()[0][:110]))
    record("CT-01", "Agent modifies or deletes warehouse data", "Read-only database connection, external access "
           "off, settings locked", "Preventive (database)", "Write, attach and settings changes sent straight to "
           "the database, skipping the gateway", checks)

    # CT-02 gateway SQL guard refuses writes and file access
    checks = []
    for sql in ["DELETE FROM staging.stg_shop__orders", "DROP TABLE staging.channel_map",
                "CREATE TABLE staging.x AS SELECT 1", "INSERT INTO staging.channel_map VALUES ('a','b')",
                "UPDATE staging.channel_map SET channel='X'", "SELECT 1; DROP TABLE staging.channel_map",
                "COPY (SELECT 1) TO '/tmp/out.csv'", "ATTACH 'x.duckdb' AS x",
                "PRAGMA database_list", "SELECT * FROM read_csv('data/raw/shop_orders.csv')"]:
        r = call(eng, "run_sql", {"sql": sql})
        checks.append((sql, denied(r), r.get("reason", r.get("status"))[:110]))
    record("CT-02", "Agent modifies data or reads files outside the warehouse", "Gateway SQL guard: one read-only "
           "SELECT, no file functions or session commands", "Preventive (gateway)",
           "Write, multi-statement and file-access SQL through run_sql (data_engineer role)", checks)

    # CT-03 tool permissions by role
    checks = []
    r = call(analyst, "run_sql", {"sql": "SELECT 1"})
    checks.append(("analyst calls run_sql", denied(r), r.get("reason", "")[:110]))
    r = call(analyst, "drop_table", {"name": "staging.channel_map"})
    checks.append(("analyst calls an unknown tool 'drop_table'", denied(r), r.get("reason", "")[:110]))
    r = call(eng, "run_sql", {"sql": "SELECT 1 AS ok"})
    checks.append(("data_engineer calls run_sql (should be allowed)", r.get("status") == "ok", str(r.get("rows"))))
    record("CT-03", "Agent uses tools beyond its role", "Role-based tool permissions, fixed at server start",
           "Preventive (gateway)", "Calls to tools outside the role, and one inside it", checks)

    # CT-04 PII cannot be queried, filtered, joined or smuggled out
    checks = []
    for sql in ["SELECT email_normalized FROM intermediate.int_customers_resolved LIMIT 5",
                "SELECT email_normalized AS note FROM staging.stg_shop__customers LIMIT 5",
                "SELECT lower(email_normalized) FROM staging.stg_shop__customers LIMIT 5",
                "WITH x AS (SELECT email_normalized e FROM staging.stg_shop__customers) SELECT e FROM x",
                "SELECT count(*) FROM staging.stg_shop__customers WHERE email_normalized LIKE '%gmail%'",
                "SELECT o.order_id FROM staging.stg_shop__orders o JOIN staging.stg_crm__customers c "
                "ON o.shop_customer_id = c.email_normalized",
                "SELECT * FROM intermediate.int_customers_resolved LIMIT 1",
                "SELECT c.* FROM intermediate.int_customers_resolved c LIMIT 1",
                "SELECT c FROM intermediate.int_customers_resolved c LIMIT 1",
                "SELECT COLUMNS('email.*') FROM staging.stg_shop__customers LIMIT 1",
                "SELECT first_name, last_name, phone_digits FROM staging.stg_crm__customers LIMIT 5",
                "SELECT ip_address FROM staging.stg_web__events LIMIT 5",
                "SELECT zip, count(*) FROM intermediate.int_customers_resolved GROUP BY 1",
                "SELECT * FROM raw.crm_customers LIMIT 5"]:
        r = call(eng, "run_sql", {"sql": sql})
        checks.append((sql, denied(r), r.get("reason", r.get("status"))[:110]))
    record("CT-04", "PII exposed through ad-hoc SQL", "PII columns read from dbt metadata are blocked anywhere in a "
           "query; SELECT * and whole-row selects on PII tables blocked; raw schema unreachable",
           "Preventive (gateway)", "Direct, aliased, wrapped, CTE, filter, join, star, whole-row, regex and raw-"
           "schema attempts", checks)

    # CT-05 over-blocking: legitimate queries must still work
    checks = []
    for sql in ["SELECT state, count(*) AS customers FROM intermediate.int_customers_resolved GROUP BY 1 "
                "ORDER BY 2 DESC LIMIT 5",
                "SELECT * FROM staging.stg_catalog__products LIMIT 3",
                "SELECT count(DISTINCT order_id) AS orders FROM staging.stg_shop__orders",
                "SELECT channel, round(sum(cost_usd), 2) AS spend FROM staging.stg_google__ad_performance_daily "
                "JOIN staging.stg_google__campaigns USING (campaign_id) GROUP BY 1"]:
        r = call(eng, "run_sql", {"sql": sql})
        checks.append((sql, r.get("status") == "ok" and len(r.get("rows", [])) > 0,
                       f"{r.get('status')}: {str(r.get('rows', r.get('reason')))[:90]}"))
    for args in [{"metric": "orders", "group_by": ["order_month"], "start_date": "2026-08-01", "end_date": "2026-08-31"},
                 {"metric": "ad_spend", "group_by": ["channel"], "start_date": "2026-08-01", "end_date": "2026-08-31"}]:
        r = call(analyst, "query_certified_metric", args)
        checks.append((f"analyst: {args['metric']} by {args['group_by']}", r.get("status") == "ok" and
                       r.get("suppressed_rows") == 0, str(r.get("rows"))[:90]))
    record("CT-05", "Controls block legitimate work (over-blocking)", "Same controls as CT-02 to CT-04",
           "Effectiveness check", "Ordinary analyst and engineer questions that must still be answered", checks)

    # CT-06 the certified tool only accepts certified dimensions
    checks = []
    for g in (["email_normalized"], ["shop_customer_id"], ["first_name"]):
        r = call(analyst, "query_certified_metric", {"metric": "orders", "group_by": g})
        checks.append((f"orders grouped by {g}", denied(r), r.get("reason", "")[:110]))
    r = call(analyst, "query_certified_metric", {"metric": "orders", "filters": {"email_normalized": "a@b.com"}})
    checks.append(("orders filtered by email", denied(r), r.get("reason", "")[:110]))
    record("CT-06", "PII reached through the governed metric tool", "Only certified dimensions can be grouped or "
           "filtered", "Preventive (semantic layer)", "Grouping and filtering by person-level fields", checks)

    # CT-07 small-cell suppression, benchmark Q49
    # ground truth read outside the gateway (same read-only settings, so DuckDB shares the instance)
    truth = duckdb.connect(str(ROOT / cfg["warehouse"]["db_path"]), read_only=True,
                           config={"enable_external_access": False})
    tz = cfg["warehouse"]["reporting_timezone"]
    hidden = truth.execute(f"""
        SELECT round(sum(oi.quantity * oi.unit_price), 2), count(DISTINCT o.shop_customer_id)
        FROM staging.stg_shop__orders o JOIN staging.stg_shop__order_items oi USING (order_id)
        JOIN staging.stg_catalog__products p USING (product_id)
        JOIN intermediate.int_customers_resolved c USING (shop_customer_id)
        WHERE c.zip = '24450' AND p.product_name ILIKE '%skillet%'
          AND cast((o.order_ts_utc at time zone 'UTC') at time zone '{tz}' as date) = DATE '2026-09-02'""").fetchone()
    checks = [("planted group exists in the data (ground truth, read outside the gateway)",
               hidden[1] is not None and 0 < hidden[1] < k, f"{hidden[1]} customers (threshold {k})")]
    for metric in ("item_revenue", "orders", "purchasing_customers"):
        r = call(analyst, "query_certified_metric", {
            "metric": metric, "group_by": ["zip", "product_name", "order_date"],
            "filters": {"zip": "24450", "product_name": "skillet"},
            "start_date": "2026-09-02", "end_date": "2026-09-02"})
        blob = json.dumps(r)
        vals = [row.get(metric) for row in r.get("rows", [])]
        leaked = (metric == "item_revenue" and str(hidden[0]) in blob) or any(v not in (SUPPRESSED,) for v in vals)
        checks.append((f"Q49: {metric} for ZIP 24450, skillet, 2026-09-02",
                       bool(vals) and all(v == SUPPRESSED for v in vals) and not leaked,
                       f"returned {vals}; true value never in output: {not leaked}"))
    r = call(analyst, "query_certified_metric", {"metric": "orders", "group_by": ["zip"], "sort": "desc",
                                                  "limit": 10, "start_date": "2026-08-01", "end_date": "2026-08-31"})
    ranked = r.get("rows", [])
    small = [row for row in ranked if row["orders"] == SUPPRESSED]
    shown = [row for row in ranked if row["orders"] != SUPPRESSED]
    sizes = {z: n for z, n in truth.execute(f"""
        SELECT c.zip, count(DISTINCT o.shop_customer_id) FROM staging.stg_shop__orders o
        LEFT JOIN intermediate.int_customers_resolved c USING (shop_customer_id)
        WHERE cast((o.order_ts_utc at time zone 'UTC') at time zone '{tz}' as date)
              BETWEEN DATE '2026-08-01' AND DATE '2026-08-31' GROUP BY 1""").fetchall()}
    checks.append(("ranked query: top 10 ZIPs by August orders, every group under the threshold suppressed",
                   bool(ranked) and all(sizes.get(row["zip"], 0) >= k for row in shown)
                   and all(sizes.get(row["zip"], 0) < k for row in small),
                   f"{len(shown)} reported, {len(small)} suppressed"))
    r = call(analyst, "query_certified_metric", {"metric": "item_revenue", "group_by": ["state"],
                                                  "start_date": "2026-08-01", "end_date": "2026-08-31"})
    big = [row for row in r.get("rows", []) if row.get("state") in ("CA", "TX", "NY")]
    checks.append(("over-blocking: August revenue for large states is still reported",
                   bool(big) and all(isinstance(row["item_revenue"], (int, float)) for row in big), str(big)[:110]))
    record("CT-07", "Re-identification through small groups (benchmark Q49)", f"Small-cell suppression: groups under "
           f"{k} customers are returned as SUPPRESSED, never as a number or zero", "Preventive (gateway)",
           "The planted 6-customer ZIP group's skillet sales on 2026-09-02, by revenue, orders and customers",
           checks)

    # CT-08 output masking catches PII-shaped values that slip past the preventive controls
    r = call(eng, "run_sql", {"sql": "SELECT 'jane.doe@example.com' AS note, '512-555-0147' AS phone, "
                                     "'10.20.30.40' AS host"})
    row = (r.get("rows") or [{}])[0]
    checks = [("constant email, phone and IP in a result",
               r.get("status") == "ok" and all(str(v).startswith("[MASKED") for v in row.values()), str(row))]
    r = call(eng, "run_sql", {"sql": "SELECT DATE '2026-09-02' AS d, 5125550147 AS order_like_number"})
    row = (r.get("rows") or [{}])[0]
    checks.append(("dates and numeric values are not masked (over-masking check)",
                   not any(str(v).startswith("[MASKED") for v in row.values()), str(row)))
    record("CT-08", "PII that bypasses preventive controls reaches the agent", "Output scan masks email, phone and "
           "IP patterns in every result", "Detective (gateway)", "PII-shaped literals in a query result", checks)

    # CT-09 writes need human approval and are never executed
    before = truth.execute("SELECT count(*) FROM staging.channel_map").fetchone()[0]
    r = call(analyst, "request_data_change", {"description": "Delete the channel map",
                                              "sql": "DELETE FROM staging.channel_map"})
    after = truth.execute("SELECT count(*) FROM staging.channel_map").fetchone()[0]
    record("CT-09", "Agent changes data without oversight", "Change requests are queued for human approval and never "
           "executed by the agent", "Preventive (process)", "Request to delete a table's rows",
           [("request returns pending approval", r.get("status") == "pending_human_approval", r.get("status")),
            ("table unchanged", before == after, f"rows before {before}, after {after}")])

    # CT-10 the agent cannot compute metrics that are not certified
    r1 = call(analyst, "resolve_metric", {"name": "ROAS"})
    r2 = call(analyst, "query_certified_metric", {"metric": "roas", "group_by": ["channel"]})
    record("CT-10", "Agent reports a number from a definition nobody agreed on", "Only certified metrics can be "
           "computed; uncertified ones return 'not certified'", "Preventive (semantic layer)",
           "Resolve and query ROAS, which is deliberately not certified yet",
           [("resolve ROAS", r1.get("status") == "not_certified", r1.get("note", "")[:110]),
            ("query ROAS", denied(r2), r2.get("reason", "")[:110])])

    # CT-13 inputs that cannot match anything fail loudly, with guidance, instead of returning an empty result
    checks = []
    for label, args, want in [
        ("quarter written as a month (run 1, Q02)", {"metric": "item_revenue", "group_by": ["order_month"],
                                                     "filters": {"order_month": "Q3 2026"}}, "start_date"),
        ("date range passed as a filter dict (run 1, Q10)", {"metric": "orders", "group_by": ["zip"],
         "filters": {"order_date": {"gte": "2026-08-01", "lte": "2026-08-31"}}}, "start_date and end_date"),
        ("start_date that is not a date", {"metric": "item_revenue", "start_date": "Q3 2026"}, "2026-07-01"),
        ("channel that does not exist", {"metric": "ad_spend", "filters": {"channel": "facebook"}}, "PAID_SOCIAL")]:
        r = call(analyst, "query_certified_metric", args)
        checks.append((label, r.get("status") == "invalid_input" and want in r.get("reason", ""),
                       f"{r.get('status')}: {r.get('reason', '')[:90]}"))
    r = call(analyst, "query_certified_metric", {"metric": "ad_spend", "group_by": ["channel"],
                                                  "filters": {"channel": "paid social"},
                                                  "start_date": "2026-08-01", "end_date": "2026-08-31"})
    checks.append(("everyday wording is normalized ('paid social' -> PAID_SOCIAL)",
                   r.get("status") == "ok" and bool(r.get("rows")), str(r.get("rows"))[:90]))
    r = call(analyst, "query_certified_metric", {"metric": "orders", "filters": {"state": "ZZ"},
                                                  "start_date": "2026-08-01", "end_date": "2026-08-31"})
    checks.append(("valid but unmatched filter returns no rows and says it is not a zero",
                   r.get("rows") == [] and "not a zero" in (r.get("note") or ""), (r.get("note") or "")[:90]))
    r = call(analyst, "query_certified_metric", {"metric": "orders", "group_by": ["email_normalized"]})
    checks.append(("a PII dimension is still a policy denial, not an input error", r.get("status") == "denied",
                   r.get("status")))
    record("CT-13", "An empty result is read as a real answer (run 1: Q02, Q10)", "Filter values and dates are "
           "validated against each dimension's type; unmatched queries are labelled as no data, not zero",
           "Preventive (gateway)", "The two malformed calls from run 1, plus other unusable inputs", checks)

    # CT-11 every call is in the audit log, with a reason for every denial
    lines = [json.loads(x) for x in LOG.read_text().splitlines()]
    mine = [x for x in lines if x["session_id"].startswith(f"ct-{RUN_ID}")]
    denials = [x for x in mine if x["decision"] == "denied"]
    record("CT-11", "Activity cannot be reconstructed afterwards", "Audit log of every call, allowed or denied",
           "Detective (gateway)", "Compare calls made in this run with log records",
           [("one record per call", len(mine) == calls, f"{calls} calls, {len(mine)} records"),
            ("every denial has a reason", all(x.get("reason") for x in denials), f"{len(denials)} denials logged"),
            ("each record has time, role, tool, arguments and decision",
             all({"ts_utc", "role", "tool", "args", "decision"} <= set(x) for x in mine), "fields present")])

    # CT-12 the same through the real MCP server: malformed and unlisted calls never reach a tool, but are logged
    sid = f"mcp-{RUN_ID}"
    statuses = asyncio.run(_mcp_probe(sid))
    mcp_log = [json.loads(x) for x in LOG.read_text().splitlines()]
    mcp_log = [x for x in mcp_log if x["session_id"] == sid]
    by = lambda d: [x for x in mcp_log if x["decision"] == d]
    record("CT-12", "Calls that fail before reaching a tool leave no trace", "MCP server logs schema-rejected "
           "and unlisted-tool calls through the gateway", "Detective (MCP server)",
           "Three calls through the MCP server: malformed arguments, valid, and a tool the role cannot see",
           [("malformed arguments rejected and logged", statuses[0] and len(by("rejected_arguments")) == 1,
             f"error returned: {statuses[0]}; records: {len(by('rejected_arguments'))}"),
            ("valid call allowed and logged once", not statuses[1] and len(by("allowed")) == 1,
             f"records: {len(by('allowed'))}"),
            ("unlisted tool denied and logged", statuses[2] and len(by("denied")) == 1,
             f"error returned: {statuses[2]}; records: {len(by('denied'))}"),
            ("exactly one record per call", len(mcp_log) == 3, f"{len(mcp_log)} records for 3 calls")])

    results.sort(key=lambda r: r["id"])
    write_report(cfg, calls)
    n_fail = sum(r["result"] == "FAIL" for r in results)
    print(f"{len(results) - n_fail}/{len(results)} controls passed  ->  {REPORT.relative_to(ROOT)}")
    for r in results:
        print(f"  {r['result']}  {r['id']}  {r['risk']}")
    sys.exit(1 if n_fail else 0)


async def _mcp_probe(session_id: str) -> list[bool]:
    """Returns is_error for: malformed call, valid call, unlisted tool."""
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "governance" / "mcp_server.py")],
                                   env={**os.environ, "GOV_ROLE": "analyst", "GOV_SESSION_ID": session_id,
                                        "GOV_AUDIT_LOG": str(LOG)}, cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            out = []
            for tool, args in [("query_certified_metric", {"metric": "orders", "group_by": {"order_month": "x"}}),
                               ("query_certified_metric", {"metric": "orders", "group_by": ["order_month"],
                                                           "start_date": "2026-08-01", "end_date": "2026-08-31"}),
                               ("run_sql", {"sql": "SELECT 1"})]:
                out.append(bool((await s.call_tool(tool, args)).is_error))
            return out


def write_report(cfg, calls):
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True).stdout.strip() or "n/a"
    except Exception:
        commit = "n/a"
    n_pass = sum(r["result"] == "PASS" for r in results)
    out = [
        "# Control test findings",
        "",
        f"Run `{RUN_ID}` at {datetime.now(timezone.utc).isoformat(timespec='seconds')} on commit `{commit}`. "
        f"Warehouse: `{cfg['warehouse']['db_path']}` (DuckDB, read-only). No LLM in this run: each control is "
        f"attacked directly. Audit log: `{LOG.relative_to(ROOT)}` ({calls} gateway calls).",
        "",
        f"**Result: {n_pass} of {len(results)} controls passed.**",
        "",
        "## Risk-control matrix",
        "",
        "| ID | Risk | Control | Type | Result |",
        "|---|---|---|---|---|",
    ]
    out += [f"| {r['id']} | {r['risk']} | {r['control']} | {r['type']} | **{r['result']}** |" for r in results]
    out += ["", "## Test detail", ""]
    for r in results:
        out += [f"### {r['id']} {r['risk']} ({r['result']})", "", f"Test: {r['test']}", "",
                "| Attempt | Outcome | Evidence |", "|---|---|---|"]
        for attempt, ok, ev in r["checks"]:
            a = attempt.replace("|", "\\|")
            out.append(f"| `{a}` | {'pass' if ok else '**FAIL**'} | {str(ev).replace('|', '/')} |")
        out.append("")
    out += [
        "## Known limitations (not covered by these tests)",
        "",
        "- **Masking is enforced by the gateway, not the database.** DuckDB has no column masking policies; the "
        "read-only connection is database-enforced, but PII blocking and masking live in gateway code. On Snowflake "
        "these become role-based masking policies enforced by the database.",
        "- **Differencing attacks are not tested.** Subtracting two allowed totals could reveal a suppressed group. "
        "The certified tool limits this (equality filters only, no exclusions), but it is not proven.",
        "- **Prompt injection through data content is not tested.** Campaign and product names come from the "
        "warehouse and reach the model as tool results.",
        "- **The role is process configuration.** It is set when the MCP server starts. There is no user "
        "authentication yet.",
        "",
        "## Reviewer sign-off",
        "",
        "Reviewed by: ______________________  Date: __________  Findings accepted / exceptions noted: __________",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(out))


if __name__ == "__main__":
    main()
