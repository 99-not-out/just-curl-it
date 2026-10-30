#!/usr/bin/env python3
"""HTTP -> Google BigQuery, over its REST API. Stdlib only. The `query` shape:

  GET  /datasets                  dataset ids in the project
  GET  /tables?dataset=D          tables in a dataset
  GET  /query?sql=SELECT...       dry-run, then run if it's a SELECT -> {columns, types, rows, rowCount, ...}
  POST /query                     body = SQL (text) or {"sql": ..., "location": ...}; may write

GET is read-only because BigQuery says what the statement is: the query is
dry-run first (jobs.insert with dryRun, which parses and plans but executes
nothing) and only `statementType == SELECT` goes on to run (ADR 0005). The
dry run also yields the bytes estimate, returned as estimatedBytes.

The job dance -- jobs.query, poll getQueryResults until jobComplete, follow
pageToken -- is collapsed into one response, up to `max_rows`.
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /datasets",
    "GET    /tables?dataset=D",
    "GET    /query?sql=SELECT...    (dry-run gate: SELECT only; estimatedBytes reported)",
    "POST   /query                  (body = SQL or {sql, location}; may mutate)",
]

DEFAULT_UPSTREAM = "https://bigquery.googleapis.com"


# ---------------------------------------------------------------------------
# REST plumbing

def _token(cfg: dict) -> str:
    tok = jci.resolve_secret(cfg, "token")
    if not tok:
        raise jci.BackendConfigError(
            "no token: set token, token_keychain or token_cmd "
            "(e.g. token_cmd = gcloud auth print-access-token)")
    return tok.strip()


def _project(cfg: dict) -> str:
    p = cfg.get("project", "").strip()
    if not p:
        raise jci.BackendConfigError("missing 'project' in backends.conf")
    return p


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def call(cfg: dict, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    """One call to the BigQuery REST API under /bigquery/v2/projects/<project>."""
    cfg = {**cfg, "upstream": cfg.get("upstream") or DEFAULT_UPSTREAM}
    u = jci.upstream_target(cfg)
    full = f"{u.path.rstrip('/')}/bigquery/v2/projects/{quote(_project(cfg))}{path}"
    if query:
        full += "?" + urlencode(query)
    headers = {"authorization": f"Bearer {_token(cfg)}", "accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    conn = jci.connect_upstream(cfg, u)
    try:
        conn.request(method, full, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except ValueError:
        doc = {"raw": raw[:500].decode("utf-8", "replace")}
    if resp.status >= 400:
        err = (doc.get("error") or {}) if isinstance(doc, dict) else {}
        raise ApiError(resp.status, err.get("message") or f"HTTP {resp.status}: {raw[:300]!r}")
    return doc


# ---------------------------------------------------------------------------
# rows

def _type_label(f: dict) -> str:
    t = f.get("type", "STRING")
    return t + ("[]" if f.get("mode") == "REPEATED" else "")


def schema_columns(schema: dict) -> tuple[list[str], list[str]]:
    fields = (schema or {}).get("fields") or []
    return [f["name"] for f in fields], [_type_label(f) for f in fields]


def _scalar(t: str, v):
    if v is None:
        return None
    if t in ("INTEGER", "INT64"):
        return int(v)
    if t in ("FLOAT", "FLOAT64"):
        return float(v)
    if t in ("BOOLEAN", "BOOL"):
        return str(v).lower() == "true"
    return v   # STRING, TIMESTAMP, DATE, NUMERIC, BIGNUMERIC, BYTES, JSON ... as the API sent them


def _cell(f: dict, v):
    if f.get("mode") == "REPEATED":
        return [_cell({**f, "mode": "NULLABLE"}, item.get("v")) for item in (v or [])]
    if f.get("type") == "RECORD":
        if v is None:
            return None
        sub = f.get("fields") or []
        return {sf["name"]: _cell(sf, c.get("v")) for sf, c in zip(sub, v.get("f", []))}
    return _scalar(f.get("type", "STRING"), v)


def convert_rows(schema: dict, rows: list) -> list[list]:
    fields = (schema or {}).get("fields") or []
    return [[_cell(f, c.get("v")) for f, c in zip(fields, r.get("f", []))] for r in rows or []]


# ---------------------------------------------------------------------------
# queries

def refuse_statement(statement_type: str | None) -> str | None:
    if statement_type == "SELECT":
        return None
    return f"GET /query runs SELECT only; BigQuery classified this as {statement_type or 'unknown'}"


def dry_run(cfg: dict, sql: str, location: str | None) -> dict:
    body = {"configuration": {"dryRun": True, "query": {"query": sql, "useLegacySql": False}}}
    if location:
        body["jobReference"] = {"projectId": _project(cfg), "location": location}
    job = call(cfg, "POST", "/jobs", body)
    stats = job.get("statistics") or {}
    return {"statementType": (stats.get("query") or {}).get("statementType"),
            "estimatedBytes": int(stats.get("totalBytesProcessed") or 0)}


def run_query(cfg: dict, sql: str, location: str | None, read_only: bool) -> None:
    out: dict = {}
    if read_only:
        d = dry_run(cfg, sql, location)
        why = refuse_statement(d["statementType"])
        if why:
            return jci.send_error(405, why, statementType=d["statementType"],
                                  hint="use POST /query for DDL and DML", allow=["POST"])
        out.update(d)

    max_rows = int(cfg.get("max_rows", 10000))
    timeout_ms = int(jci.timeout(cfg) * 1000)
    body = {"query": sql, "useLegacySql": False, "timeoutMs": timeout_ms, "maxResults": max_rows}
    if location:
        body["location"] = location
    res = call(cfg, "POST", "/queries", body)
    job = res.get("jobReference") or {}
    job_id, loc = job.get("jobId"), job.get("location") or location

    # Poll until complete (the first call waits up to timeoutMs on the server side).
    deadline = time.monotonic() + 600
    while not res.get("jobComplete"):
        if time.monotonic() > deadline:
            return jci.send_error(504, f"job {job_id} still running after 10 minutes", jobId=job_id)
        time.sleep(0.5)
        q = {"timeoutMs": timeout_ms, "maxResults": max_rows}
        if loc:
            q["location"] = loc
        res = call(cfg, "GET", f"/queries/{quote(job_id or '')}", query=q)

    schema = res.get("schema") or {}
    rows = convert_rows(schema, res.get("rows") or [])
    truncated = False
    while res.get("pageToken") and len(rows) < max_rows:
        q = {"pageToken": res["pageToken"], "maxResults": max_rows - len(rows)}
        if loc:
            q["location"] = loc
        res = call(cfg, "GET", f"/queries/{quote(job_id or '')}", query=q)
        rows.extend(convert_rows(schema, res.get("rows") or []))
    if res.get("pageToken"):
        truncated = True

    cols, types = schema_columns(schema)
    out.update({"jobId": job_id, "rowCount": len(rows)})
    if schema:
        out.update({"columns": cols, "types": types, "rows": rows})
    if res.get("totalRows") is not None:
        out["totalRows"] = int(res["totalRows"])
    if res.get("numDmlAffectedRows") is not None:
        out["affectedRows"] = int(res["numDmlAffectedRows"])
    if res.get("totalBytesProcessed") is not None:
        out["bytesProcessed"] = int(res["totalBytesProcessed"])
    if truncated:
        out["truncated"] = True
        out["hint"] = f"raise max_rows (now {max_rows}) in backends.conf or add LIMIT"
    jci.send_json(200, out)


def handle_datasets(cfg: dict) -> None:
    res = call(cfg, "GET", "/datasets", query={"maxResults": 1000})
    ids = sorted((d.get("datasetReference") or {}).get("datasetId", "") for d in res.get("datasets") or [])
    jci.send_json(200, {"project": _project(cfg), "datasets": ids})


def handle_tables(cfg: dict, dataset: str) -> None:
    res = call(cfg, "GET", f"/datasets/{quote(dataset)}/tables", query={"maxResults": 1000})
    tables = sorted(({"name": (t.get("tableReference") or {}).get("tableId", ""), "type": t.get("type", "")}
                     for t in res.get("tables") or []), key=lambda t: t["name"])
    jci.send_json(200, {"dataset": dataset, "tables": tables})


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("query", ROUTES)

    cfg = jci.load_backend()
    location = cfg.get("location") or None
    try:
        if method == "GET" and p == "/datasets":
            return handle_datasets(cfg)
        if method == "GET" and p == "/tables":
            ds = qs.get("dataset", [cfg.get("dataset", "")])[0]
            if not ds:
                return jci.send_error(400, "missing ?dataset= (or a `dataset` default in backends.conf)")
            return handle_tables(cfg, ds)
        if p == "/query":
            if method == "GET":
                sql = qs.get("sql", [""])[0]
                if not sql:
                    return jci.send_error(400, "missing ?sql=")
                return run_query(cfg, sql, qs.get("location", [location])[0], read_only=True)
            if method == "POST":
                if "json" in headers.get("content-type", "").lower():
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except ValueError as e:
                        return jci.send_error(400, f"bad JSON body: {e}")
                    sql, location = data.get("sql", ""), data.get("location") or location
                else:
                    sql = body.decode("utf-8")
                if not sql.strip():
                    return jci.send_error(400, "empty SQL")
                return run_query(cfg, sql, location, read_only=False)
    except ApiError as e:
        if e.status in (401, 403):
            return jci.send_error(502, f"BigQuery refused the token: {e}")
        if e.status == 400:
            return jci.send_error(400, str(e))
        return jci.send_error(502, f"BigQuery HTTP {e.status}: {e}")
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
