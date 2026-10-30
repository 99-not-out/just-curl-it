#!/usr/bin/env python3
"""HTTP -> ClickHouse, over its own HTTP interface. Stdlib only. The `query`
shape, same as postgres / mysql / sqlite:

  GET  /tables                  tables in the configured database
  GET  /query?sql=SELECT...     run with the server setting readonly=1 -> {columns, types, rows, rowCount}
  POST /query                   body = SQL; may write. INSERT ... FORMAT JSONEachRow with data works as-is

GET is read-only because ClickHouse is told so: `readonly=1` makes the
server refuse DDL, DML and settings changes (ADR 0005). Multi-statements
are refused by the server by default.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlencode, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /tables",
    "GET    /query?sql=SELECT...    (server readonly=1)",
    "POST   /query                  (body = SQL; may mutate; INSERT ... FORMAT ... with data ok)",
]

_READONLY_CODE = "164"   # DB::Exception READONLY
_SYNTAX_CODES = {"62", "47", "60", "81"}   # SYNTAX_ERROR, UNKNOWN_IDENTIFIER, UNKNOWN_TABLE, UNKNOWN_DATABASE


def _headers(cfg: dict) -> dict:
    h = {"Content-Type": "text/plain; charset=utf-8"}
    if cfg.get("user"):
        h["X-ClickHouse-User"] = cfg["user"]
    if cfg.get("password"):
        h["X-ClickHouse-Key"] = cfg["password"]
    if cfg.get("database"):
        h["X-ClickHouse-Database"] = cfg["database"]
    return h


def run_query(cfg: dict, sql: str, read_only: bool) -> None:
    u = jci.upstream_target(cfg)
    params = {"default_format": "JSONCompact"}
    if read_only:
        params["readonly"] = "1"
    path = (u.path.rstrip("/") or "") + "/?" + urlencode(params)
    conn = jci.connect_upstream(cfg, u)
    try:
        conn.request("POST", path, body=sql.encode("utf-8"), headers=_headers(cfg))
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    if resp.status != 200:
        code = resp.getheader("X-ClickHouse-Exception-Code", "")
        msg = raw.decode("utf-8", "replace").strip().split("(version")[0].strip()
        if code == _READONLY_CODE:
            return jci.send_error(405, f"GET /query is read-only: {msg}",
                                  hint="use POST /query to run statements that write",
                                  allow=["POST"])
        if code in _SYNTAX_CODES or resp.status in (400, 404):
            return jci.send_error(400, msg, clickhouse_code=code or None)
        if resp.status in (401, 403, 516):
            return jci.send_error(502, f"clickhouse refused the credentials: {msg}")
        return jci.send_error(400 if code else 502, msg or f"clickhouse HTTP {resp.status}",
                              clickhouse_code=code or None)

    if not raw.strip():
        # INSERT / DDL: nothing comes back but the summary header.
        summary = {}
        try:
            summary = json.loads(resp.getheader("X-ClickHouse-Summary", "{}"))
        except ValueError:
            pass
        return jci.send_json(200, {"rowCount": int(summary.get("written_rows", 0) or 0),
                                   "summary": summary})
    try:
        data = json.loads(raw)
    except ValueError:
        # A FORMAT other than JSONCompact in the statement: pass it through verbatim.
        return jci.send(200, raw, resp.getheader("Content-Type", "text/plain; charset=utf-8"))
    return jci.send_json(200, {
        "columns": [m["name"] for m in data.get("meta", [])],
        "types": [m["type"] for m in data.get("meta", [])],
        "rowCount": data.get("rows", len(data.get("data", []))),
        "rows": data.get("data", []),
        "statistics": data.get("statistics", {}),
    })


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("query", ROUTES)

    cfg = jci.load_backend()
    try:
        if method == "GET" and p == "/tables":
            return run_query(cfg, "SELECT database, name, engine FROM system.tables "
                                  "WHERE database = currentDatabase() ORDER BY name", read_only=True)
        if p == "/query":
            if method == "GET":
                sql = qs.get("sql", [""])[0]
                if not sql:
                    return jci.send_error(400, "missing ?sql=")
                return run_query(cfg, sql, read_only=True)
            if method == "POST":
                return run_query(cfg, body.decode("utf-8"), read_only=False)
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
