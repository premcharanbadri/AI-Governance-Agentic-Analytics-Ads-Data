"""MCP server for the governed analytics agent.

The role is fixed when the server starts (GOV_ROLE env var, default 'analyst'). The model cannot change it.
Only the tools the role may use are listed, and every call still goes through PolicyGateway.call(), so a
tool that is somehow called anyway is denied and logged (defense in depth).

Run:  GOV_ROLE=analyst python governance/mcp_server.py      (stdio transport)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway import PolicyGateway  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

ROLE = os.environ.get("GOV_ROLE", "analyst")
gw = PolicyGateway(role=ROLE, session_id=os.environ.get("GOV_SESSION_ID"),
                   audit_log=os.environ.get("GOV_AUDIT_LOG"))   # None -> path from config.yaml


class GovernedServer(MCPServer):
    """Every call attempt reaches the audit log, including ones that never get to a tool:
    calls to tools this role cannot see, and calls whose arguments fail the MCP schema check."""

    async def call_tool(self, name, arguments, context=None):
        if name not in gw.allowed_tools():
            gw.call(name, arguments or {})       # records the denied attempt
            return await super().call_tool(name, arguments, context)
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as e:                   # schema validation failed before the tool ran
            gw.log_event(name, arguments, "rejected_arguments", " ".join(str(e).split())[:300])
            raise


server = GovernedServer(
    "lumen-governed-analytics",
    instructions=("Governed analytics tools for Lumen Goods. Resolve a metric's certified definition before "
                  "querying it. Never estimate a metric that is not certified. Report suppressed groups as "
                  "suppressed."))


def resolve_metric(name: str) -> dict:
    """Look up the single certified definition of a business metric (e.g. 'orders', 'revenue', 'customers',
    'ad spend'). Returns the definition, owner and allowed dimensions, or 'not_certified'. Call this first."""
    return gw.call("resolve_metric", {"name": name})


def _as_list(v):
    """Small models often send 'channel' or '["channel"]' instead of ["channel"]."""
    if v is None or isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (ValueError, TypeError):
        return [x.strip() for x in str(v).split(",") if x.strip()]


def _as_dict(v):
    if v is None or isinstance(v, dict):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def query_certified_metric(metric: str, group_by: list[str] | str | None = None, start_date: str | None = None,
                           end_date: str | None = None, filters: dict | str | None = None,
                           sort: str | None = None, limit: int | None = None) -> dict:
    """Compute a certified metric. Use the metric name returned by resolve_metric.

    - group_by: a LIST of dimension names, e.g. ["channel"] or ["order_month", "category"].
    - start_date / end_date: dates as YYYY-MM-DD, inclusive. Use these for every date range
      (a quarter, a month, a week). Q3 2026 is start_date "2026-07-01", end_date "2026-09-30".
    - filters: {dimension: value} or {dimension: [values]}, e.g. {"channel": "PAID_SOCIAL"}. Not for dates.
    - sort and limit: to rank ("top 3", "most"), use sort "desc" and limit 3.
    Example: {"metric": "orders", "group_by": ["order_month"], "start_date": "2026-07-01", "end_date": "2026-08-31"}
    Groups with too few customers come back SUPPRESSED. An empty result means nothing matched, not zero."""
    return gw.call("query_certified_metric", {"metric": metric, "group_by": _as_list(group_by),
                                              "start_date": start_date, "end_date": end_date,
                                              "filters": _as_dict(filters), "sort": sort, "limit": limit})


def check_freshness() -> dict:
    """Latest available date per data source, the benchmark as-of date, and provisional-data warnings."""
    return gw.call("check_freshness", {})


def get_lineage(metric: str) -> dict:
    """The upstream warehouse models a certified metric is computed from."""
    return gw.call("get_lineage", {"metric": metric})


def request_data_change(description: str, sql: str | None = None) -> dict:
    """Request any change to data (insert, update, delete, drop). Nothing is executed: the request is queued
    for human approval."""
    return gw.call("request_data_change", {"description": description, "sql": sql})


def run_sql(sql: str) -> dict:
    """Run one read-only SELECT on staging or intermediate models. PII columns, raw data, SELECT * on
    tables with PII, and any write are refused."""
    return gw.call("run_sql", {"sql": sql})


for _fn in (resolve_metric, query_certified_metric, check_freshness, get_lineage, request_data_change, run_sql):
    if _fn.__name__ in gw.allowed_tools():      # least privilege: only list what this role may use
        server.tool()(_fn)

if __name__ == "__main__":
    server.run("stdio")
