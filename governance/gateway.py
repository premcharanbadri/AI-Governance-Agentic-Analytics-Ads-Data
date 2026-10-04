"""Policy gateway: the single path between the agent's tools and the warehouse.

Every tool call goes through `PolicyGateway.call()`, which
  1. checks the caller's role may use the tool        (preventive: tool permissions)
  2. runs the tool on a read-only DuckDB connection   (preventive: database-enforced read-only)
  3. blocks queries that touch PII columns or raw data (preventive: PII and schema controls)
  4. suppresses groups smaller than the minimum size   (preventive: small-cell suppression, Q49)
  5. masks anything that looks like PII in the output  (detective: output scan)
  6. records the call, allowed or denied, in the audit log (detective: traceability)

The role is fixed when the gateway is created (from config / server start), never chosen by the model.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import sqlglot
import yaml
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
SUPPRESSED = "SUPPRESSED (group below minimum size)"

# Output scan patterns (detective control; the preventive controls should mean these never fire).
PII_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"),
    "ipv4": re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
}
# Statements and functions that can write, read files, or change the session. Denied outright.
DENIED_FUNCTIONS = re.compile(
    r"\b(read_csv\w*|read_parquet|read_json\w*|read_text|read_blob|parquet_scan|csv_scan|glob|"
    r"sniff_csv|query_table|query|columns)\s*\(", re.I)
DENIED_KEYWORDS = re.compile(r"\b(attach|detach|copy|export|import|install|load|pragma|set|reset|call|"
                             r"checkpoint|vacuum|use)\b", re.I)


class Denied(Exception):
    """A policy decision to refuse a call. The message is shown to the agent and logged."""


class InvalidInput(Denied):
    """Arguments that cannot be applied (bad date, unknown value). Logged separately from policy denials, so
    input mistakes are not counted as over-blocking."""


def load_config(path: str | Path = ROOT / "governance" / "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_semantic_layer(path: str | Path = ROOT / "governance" / "semantic_layer.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text())


def pii_columns_from_manifest(manifest_path: Path) -> dict[str, set[str]]:
    """{'schema.model': {pii columns}} from dbt column meta `classification: pii`."""
    m = json.loads(manifest_path.read_text())
    out: dict[str, set[str]] = {}
    for node in m["nodes"].values():
        if node["resource_type"] not in ("model", "seed", "snapshot"):
            continue
        for col, spec in node.get("columns", {}).items():
            meta = {**(spec.get("meta") or {}), **((spec.get("config") or {}).get("meta") or {})}
            if meta.get("classification") == "pii":
                out.setdefault(f"{node['schema']}.{node['name']}".lower(), set()).add(col.lower())
    return out


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float):
        return round(v, 2)
    return v


class PolicyGateway:
    def __init__(self, role: str, config: dict | None = None, semantic: dict | None = None,
                 session_id: str | None = None, audit_log: str | Path | None = None):
        self.cfg = config or load_config()
        self.sem = semantic or load_semantic_layer()
        if role not in self.cfg["roles"]:
            raise ValueError(f"unknown role {role!r}")
        self.role = role
        self.session_id = session_id or uuid.uuid4().hex[:12]
        wh = self.cfg["warehouse"]
        self.k = int(self.cfg["privacy"]["min_group_size"])
        self.max_rows = int(self.cfg["privacy"]["max_rows"])
        self.allowed_schemas = {s.lower() for s in wh["allowed_schemas"]}
        self.tz = wh["reporting_timezone"]
        self.as_of = wh["as_of_date"]
        self.pii = pii_columns_from_manifest(ROOT / wh["dbt_manifest"])
        self.pii_names = set().union(*self.pii.values()) if self.pii else set()
        self.audit_path = Path(audit_log or ROOT / self.cfg["audit_log"])
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # Database-enforced controls: read-only file handle, no file or network access, settings locked.
        self.con = duckdb.connect(str(ROOT / wh["db_path"]), read_only=True,
                                  config={"enable_external_access": False})
        try:
            self.con.execute("SET lock_configuration = true")
        except duckdb.InvalidInputException as e:   # another gateway in this process already locked it
            if "locked" not in str(e):
                raise
        self.pending_changes: list[dict] = []
        self._tools = {
            "resolve_metric": self.resolve_metric,
            "query_certified_metric": self.query_certified_metric,
            "check_freshness": self.check_freshness,
            "get_lineage": self.get_lineage,
            "request_data_change": self.request_data_change,
            "run_sql": self.run_sql,
        }

    # ------------------------------------------------------------------ entry point
    def allowed_tools(self) -> list[str]:
        return list(self.cfg["roles"][self.role]["tools"])

    def log_event(self, tool: str, args: dict | None, decision: str, reason: str) -> None:
        """Record a call that never reached a tool (e.g. arguments rejected by the MCP schema check)."""
        record = {"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "session_id": self.session_id, "role": self.role, "tool": tool, "args": dict(args or {}),
                  "decision": decision, "reason": reason, "rows": 0, "duration_ms": 0}
        with self.audit_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def call(self, tool: str, args: dict | None = None) -> dict:
        """Run one tool call under policy. Always returns a dict and always writes one audit record."""
        args = dict(args or {})
        t0 = time.perf_counter()
        record = {"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "session_id": self.session_id, "role": self.role, "tool": tool, "args": args}
        try:
            if tool not in self._tools:
                raise Denied(f"unknown tool '{tool}'")
            if tool not in self.allowed_tools():
                raise Denied(f"role '{self.role}' is not permitted to use '{tool}'")
            result = self._tools[tool](**args)
            result = self._scan_output(result)
            record.update(decision=result.pop("_decision", "allowed"), reason=result.get("note"),
                          rows=len(result.get("rows", [])), suppressed_rows=result.get("suppressed_rows", 0),
                          masked_values=result.get("masked_values", 0))
        except InvalidInput as e:
            result = {"status": "invalid_input", "reason": str(e)}
            record.update(decision="invalid_input", reason=str(e), rows=0)
        except Denied as e:
            result = {"status": "denied", "reason": str(e)}
            record.update(decision="denied", reason=str(e), rows=0)
        except TypeError as e:            # bad or missing arguments from the model
            result = {"status": "error", "reason": f"invalid arguments: {e}"}
            record.update(decision="error", reason=str(e), rows=0)
        except duckdb.Error as e:         # includes the database refusing a write
            result = {"status": "error", "reason": f"database error: {e}"}
            record.update(decision="error", reason=str(e)[:300], rows=0)
        record["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        with self.audit_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return result

    # ------------------------------------------------------------------ context tools
    def _metric(self, name: str) -> tuple[str, dict] | None:
        key = (name or "").strip().lower().replace(" ", "_")
        for mname, spec in self.sem["metrics"].items():
            names = {mname} | {s.lower().replace(" ", "_") for s in spec.get("synonyms", [])}
            if key in names:
                return mname, spec
        return None

    def resolve_metric(self, name: str) -> dict:
        """Look up the one official definition of a business metric."""
        hit = self._metric(name)
        if not hit:
            certified = sorted(self.sem["metrics"])
            return {"status": "not_certified",
                    "note": f"'{name}' has no certified definition. Do not estimate it. Certified metrics: {certified}"}
        mname, spec = hit
        dims = [d for d, s in self.sem["dimensions"].items()
                if s["grain"] in (("order", "item") if spec["source"] == "orders" else ("spend",))]
        return {"status": "certified", "metric": mname, "definition": spec["description"],
                "owner": spec["owner"], "definition_status": spec["status"], "version": self.sem["version"],
                "allowed_dimensions": dims}

    def get_lineage(self, metric: str) -> dict:
        hit = self._metric(metric)
        if not hit:
            return {"status": "not_certified", "note": f"no lineage: '{metric}' is not a certified metric"}
        return {"status": "ok", "metric": hit[0], "upstream_models": hit[1]["lineage"]}

    def check_freshness(self) -> dict:
        ld = f"cast((o.order_ts_utc at time zone 'UTC') at time zone '{self.tz}' as date)"
        rows = self.con.execute(f"""
            SELECT 'shop orders', max({ld}) FROM staging.stg_shop__orders o
            UNION ALL SELECT 'google ads', max(report_date) FROM staging.stg_google__ad_performance_daily
            UNION ALL SELECT 'meta ads', max(report_date) FROM staging.stg_meta__insights""").fetchall()
        prov = self.con.execute("SELECT min(report_date) FROM staging.stg_meta__insights WHERE is_provisional"
                                ).fetchone()[0]
        return {"status": "ok", "as_of_date": self.as_of,
                "latest_date_by_source": {r[0]: _jsonable(r[1]) for r in rows},
                "note": f"Meta days from {_jsonable(prov)} onward are provisional and may be restated." if prov else None}

    # ------------------------------------------------------------------ governed query
    def query_certified_metric(self, metric: str, group_by: list[str] | None = None,
                               start_date: str | None = None, end_date: str | None = None,
                               filters: dict | None = None, sort: str | None = None,
                               limit: int | None = None) -> dict:
        """Compute a certified metric. Only certified dimensions; values are bound as parameters."""
        hit = self._metric(metric)
        if not hit:
            raise Denied(f"'{metric}' is not a certified metric; resolve_metric first and do not estimate")
        mname, spec = hit
        src = spec["source"]
        grains = ("order", "item") if src == "orders" else ("spend",)
        group_by = [g.lower() for g in (group_by or [])]
        filters = {k.lower(): v for k, v in (filters or {}).items()}
        dims = self.sem["dimensions"]
        for d in list(group_by) + list(filters):
            if d not in dims or dims[d]["grain"] not in grains:
                raise Denied(f"'{d}' is not a certified dimension for '{mname}'. Allowed: "
                             f"{[k for k, s in dims.items() if s['grain'] in grains]}")
        local_date = f"cast((o.order_ts_utc at time zone 'UTC') at time zone '{self.tz}' as date)"
        dexpr = lambda d: dims[d]["expr"].replace("{local_date}", local_date)
        date_col = self.sem["date_column"][src].replace("{local_date}", local_date)

        start_date = self._check_date("start_date", start_date)
        end_date = self._check_date("end_date", end_date)
        filters = {d: self._check_filter(d, v, dims[d]) for d, v in filters.items()}

        where, params = [], []
        if start_date:
            where.append(f"{date_col} >= CAST(? AS DATE)"); params.append(str(start_date))
        if end_date:
            where.append(f"{date_col} <= CAST(? AS DATE)"); params.append(str(end_date))
        for d, v in filters.items():
            vals = v if isinstance(v, list) else [v]
            if d == "product_name":   # partial match on product name, case-insensitive
                where.append("(" + " OR ".join([f"{dexpr(d)} ILIKE ?"] * len(vals)) + ")")
                params += [f"%{x}%" for x in vals]
            else:
                where.append(f"CAST({dexpr(d)} AS VARCHAR) IN ({', '.join('?' * len(vals))})")
                params += [str(x) for x in vals]

        if sort is not None and str(sort).lower() not in ("asc", "desc"):
            raise InvalidInput("sort must be 'asc' or 'desc' (sorts by the metric value)")
        try:
            n_limit = self.max_rows if limit in (None, "") else max(1, min(int(limit), self.max_rows))
        except (TypeError, ValueError):
            raise InvalidInput("limit must be a whole number")
        ckey = self.sem.get("customer_key", {}).get(src)
        select = [f"{dexpr(d)} AS {d}" for d in group_by] + [f"{spec['expr']} AS {mname}"]
        if ckey:
            select.append(f"COUNT(DISTINCT {ckey}) AS _group_customers")
        select.append("COUNT(*) AS _source_rows")
        pos = [str(i + 1) for i in range(len(group_by))]
        order = ([f"{len(group_by) + 1} {str(sort).upper()} NULLS LAST"] if sort else []) + pos
        sql = (f"SELECT {', '.join(select)} {self.sem['sources'][src]}"
               + (f" WHERE {' AND '.join(where)}" if where else "")
               + (f" GROUP BY {', '.join(pos)}" if group_by else "")
               + (f" ORDER BY {', '.join(order)}" if order else "")
               + f" LIMIT {n_limit + 1}")
        cur = self.con.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, map(_jsonable, r))) for r in cur.fetchall()]
        truncated = len(rows) > n_limit
        rows = rows[:n_limit]

        matched = sum(r.pop("_source_rows") or 0 for r in rows)
        suppressed = 0
        if ckey:   # small-cell suppression: never report a group of fewer than k people, and never as zero
            for r in rows:
                n = r.pop("_group_customers")
                if n is not None and 0 < n < self.k:
                    r[mname] = SUPPRESSED
                    suppressed += 1
        out = {"status": "ok", "metric": mname, "definition": spec["description"],
               "definition_status": spec["status"], "group_by": group_by, "filters": filters,
               "start_date": start_date, "end_date": end_date, "sort": sort, "rows": rows,
               "suppressed_rows": suppressed}
        if suppressed:
            out["_decision"] = "suppressed"
            out["note"] = (f"{suppressed} group(s) had fewer than {self.k} customers and were suppressed. "
                           "Report them as suppressed, not as zero or missing.")
        if matched == 0:
            rows = []
            out["rows"] = rows
            out["_decision"] = "no_rows"
            out["note"] = ("No data matched these filters and dates. This is not a zero: check the filter values and "
                           f"the date range. Order data covers 2023-01-01 to {self.as_of}.")
        if truncated:
            out["note"] = ((out.get("note") or "") + f" Showing the first {n_limit} rows"
                           + (" by metric value." if sort else "; use sort='desc' and a limit to rank.")).strip()
        return out

    @staticmethod
    def _check_date(name: str, value) -> str | None:
        if value in (None, ""):
            return None
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError:
            raise InvalidInput(f"{name} must be a date like 2026-08-01 (got {value!r}). For a quarter or month, give the "
                         "first and last day, e.g. Q3 2026 is start_date 2026-07-01, end_date 2026-09-30.")

    def _check_filter(self, dim: str, value, spec: dict):
        """Reject filter values that cannot match anything, with guidance, instead of returning an empty result."""
        vals = value if isinstance(value, list) else [value]
        if not vals or any(isinstance(v, (dict, list)) or v is None for v in vals):
            raise InvalidInput(f"filter '{dim}' must be a value or a list of values (got {value!r}). For date ranges use "
                         "start_date and end_date, not filters.")
        kind = spec.get("type", "text")
        out = []
        for v in vals:
            v = str(v).strip()
            if kind == "date":
                out.append(self._check_date(dim, v))
            elif kind == "month":
                if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
                    raise InvalidInput(f"'{dim}' values look like 2026-08 (got {v!r}). For a quarter, use start_date and "
                                 "end_date, e.g. Q3 2026 is 2026-07-01 to 2026-09-30.")
                out.append(v)
            elif kind == "enum":
                match = [a for a in spec["values"] if a.lower() == v.lower().replace(" ", "_")
                         or a.lower() == v.lower()]
                if not match:
                    raise InvalidInput(f"'{v}' is not a valid {dim}. Valid values: {spec['values']}")
                out.append(match[0])
            else:
                out.append(v)
        return out if isinstance(value, list) else out[0]

    # ------------------------------------------------------------------ change requests (human approval)
    def request_data_change(self, description: str, sql: str | None = None) -> dict:
        """Writes are never executed by the agent. They are queued for a human approver."""
        ticket = {"ticket_id": f"CHG-{uuid.uuid4().hex[:8]}", "description": description, "sql": sql,
                  "requested_by_role": self.role, "status": "pending_human_approval"}
        self.pending_changes.append(ticket)
        return {"status": "pending_human_approval", "ticket": ticket, "_decision": "pending_approval",
                "note": "No change was made. A human approver must review this request."}

    # ------------------------------------------------------------------ exploratory SQL (engineer role)
    def _check_sql(self, sql: str) -> str:
        if DENIED_KEYWORDS.search(sql) or DENIED_FUNCTIONS.search(sql):
            raise Denied("statement uses a command or function that is not allowed (file access, settings, "
                         "attach, copy, pragma, ...)")
        try:
            stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
        except sqlglot.errors.ParseError as e:
            raise Denied(f"could not parse SQL safely: {str(e).splitlines()[0]}")
        if len(stmts) != 1:
            raise Denied("exactly one statement is allowed")
        tree = stmts[0]
        if not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
            raise Denied(f"only read-only SELECT queries are allowed (got {type(tree).__name__})")
        for node in tree.walk():
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
                                 exp.Command, exp.Into)):
                raise Denied("statement contains a write or DDL clause")
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            name = t.name.lower()
            if name in cte_names and not t.db:
                continue
            schema = (t.db or "").lower()
            if schema not in self.allowed_schemas:
                raise Denied(f"table '{t.sql()}' is outside the allowed schemas {sorted(self.allowed_schemas)}; "
                             "use a schema-qualified staging or intermediate model")
            if f"{schema}.{name}" in self.pii and self._projects_star(tree):
                raise Denied(f"SELECT * is not allowed on '{schema}.{name}', which contains PII columns")
        row_refs = set(cte_names)
        for t in tree.find_all(exp.Table):
            row_refs |= {t.name.lower(), (t.alias or "").lower()}
        for col in tree.find_all(exp.Column):
            if not col.table and col.name.lower() in row_refs - {""}:
                raise Denied(f"selecting a whole row ('{col.name}') is not allowed; name the columns you need")
            if col.name.lower() in self.pii_names:
                raise Denied(f"column '{col.name}' is classified as PII and cannot be queried, filtered or joined on")
        return tree.sql(dialect="duckdb")

    @staticmethod
    def _projects_star(tree) -> bool:
        """True if any SELECT list contains * or t.* (COUNT(*) is fine: it returns no column values)."""
        for sel in tree.find_all(exp.Select):
            for e in sel.expressions:
                if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
                    return True
        return False

    def run_sql(self, sql: str) -> dict:
        """Ad-hoc read-only SQL for engineers. PII columns, raw data and writes are blocked."""
        safe = self._check_sql(sql)
        cur = self.con.execute(f"SELECT * FROM ({safe}) AS q LIMIT {self.max_rows + 1}")
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, map(_jsonable, r))) for r in cur.fetchall()]
        out = {"status": "ok", "rows": rows[: self.max_rows]}
        if len(rows) > self.max_rows:
            out["note"] = f"Result truncated to {self.max_rows} rows."
        return out

    # ------------------------------------------------------------------ detective control
    def _scan_output(self, result: dict) -> dict:
        masked = 0

        def mask(v):
            nonlocal masked
            if isinstance(v, str):
                for kind, pat in PII_PATTERNS.items():
                    if kind == "phone" and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v):
                        continue          # dates are not phone numbers
                    v, n = pat.subn(f"[MASKED {kind}]", v)
                    masked += n
            return v

        for r in result.get("rows", []):
            for k in list(r):
                r[k] = mask(r[k])
        if masked:
            result["masked_values"] = masked
        return result
