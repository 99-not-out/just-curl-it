#!/usr/bin/env python3
"""HTTP -> Snowflake, over the SQL REST API v2. Stdlib only. The `query` shape:

  GET  /tables?schema=S           SHOW TABLES IN SCHEMA S (or the configured schema)
  GET  /query?sql=SELECT...       -> {columns, types, rows, rowCount, statementHandle, ...}
  POST /query                     body = SQL (text) or {"sql", "database", "schema", "warehouse", "role"}
  GET  /statements/{handle}       results of an earlier (async) statement

Read-only on GET is a role, which Snowflake enforces: set `readonly_role`
and every GET statement runs under it, so a write fails server-side with
"insufficient privileges" (ADR 0005). Without it GET runs as the session
role and the response carries a `warning` when the statement's stats show
rows written -- visible after the fact, not prevented.

Auth: a programmatic access token or OAuth token (`token*`, `token_type`).
Key-pair JWT needs RSA signing, which stdlib can't do; mint the JWT with a
`token_cmd` instead (see README).
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, quote, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /tables?schema=S",
    "GET    /query?sql=SELECT...    (runs under readonly_role if set)",
    "POST   /query                  (body = SQL or {sql, database, schema, warehouse, role})",
    "GET    /statements/{handle}",
]


class SnowflakeError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def _token(cfg: dict) -> str:
    tok = jci.resolve_secret(cfg, "token")
    if not tok:
        raise jci.BackendConfigError("no token: set token, token_keychain or token_cmd (a PAT or OAuth token)")
    return tok.strip()


def call(cfg: dict, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    account = cfg.get("account", "").strip()
    cfg = {**cfg, "upstream": cfg.get("upstream") or (f"https://{account}.snowflakecomputing.com" if account else "")}
    if not cfg["upstream"]:
        raise jci.BackendConfigError("set 'account' (e.g. xy12345.eu-west-1) or 'upstream' in backends.conf")
    u = jci.upstream_target(cfg)
    headers = {"authorization": f"Bearer {_token(cfg)}",
               "x-snowflake-authorization-token-type": cfg.get("token_type", "PROGRAMMATIC_ACCESS_TOKEN").upper(),
               "accept": "application/json", "user-agent": "jci-snowflake/1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    conn = jci.connect_upstream(cfg, u)
    try:
        conn.request(method, (u.path.rstrip("/") or "") + path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except ValueError:
        doc = {"message": raw[:300].decode("utf-8", "replace")}
    if resp.status >= 400:
        raise SnowflakeError(resp.status, str(doc.get("code", "")), doc.get("message") or f"HTTP {resp.status}")
    return resp.status, doc


# ---------------------------------------------------------------------------
# results

def columns(rowtype: list) -> tuple[list[str], list[str]]:
    return [c.get("name", "") for c in rowtype or []], [str(c.get("type", "")).lower() for c in rowtype or []]


def _cell(col: dict, v):
    if v is None:
        return None
    t = str(col.get("type", "")).lower()
    if t == "fixed":
        return int(v) if int(col.get("scale") or 0) == 0 else v      # scaled decimals stay exact strings
    if t == "real":
        return float(v)
    if t == "boolean":
        return str(v).lower() in ("true", "1")
    if t in ("object", "array", "variant"):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v   # text, date, time, timestamp_* (as Snowflake sends them), binary


def convert_rows(rowtype: list, rows: list) -> list[list]:
    return [[_cell(c, v) for c, v in zip(rowtype or [], r)] for r in rows or []]


def wrote(stats: dict) -> bool:
    return any(int(stats.get(k) or 0) > 0 for k in
               ("numRowsInserted", "numRowsUpdated", "numRowsDeleted", "numDuplicateRowsUpdated"))


# ---------------------------------------------------------------------------
# statements

def submit(cfg: dict, sql: str, overrides: dict) -> dict:
    body = {"statement": sql, "timeout": int(cfg.get("statement_timeout", 3600))}
    for k in ("database", "schema", "warehouse", "role"):
        v = overrides.get(k) or cfg.get(k)
        if v:
            body[k] = v
    status, doc = call(cfg, "POST", "/api/v2/statements", body)
    if status == 202:
        handle = doc.get("statementHandle", "")
        deadline = time.monotonic() + 600
        delay = 0.5
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"statement {handle} still running after 10 minutes")
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
            status, doc = call(cfg, "GET", f"/api/v2/statements/{quote(handle)}")
            if status == 200:
                break
    return doc


def collect(cfg: dict, doc: dict, max_rows: int) -> dict:
    meta = doc.get("resultSetMetaData") or {}
    rowtype = meta.get("rowType") or []
    rows = convert_rows(rowtype, doc.get("data") or [])
    handle = doc.get("statementHandle", "")
    parts = meta.get("partitionInfo") or []
    for i in range(1, len(parts)):
        if len(rows) >= max_rows:
            break
        _, more = call(cfg, "GET", f"/api/v2/statements/{quote(handle)}?partition={i}")
        rows.extend(convert_rows(rowtype, more.get("data") or []))
    cols, types = columns(rowtype)
    out = {"statementHandle": handle, "columns": cols, "types": types,
           "rows": rows[:max_rows], "rowCount": min(len(rows), max_rows)}
    if meta.get("numRows") is not None:
        out["totalRows"] = int(meta["numRows"])
    if len(rows) > max_rows or (sum(int(p.get("rowCount") or 0) for p in parts) > max_rows):
        out["truncated"] = True
    for k in ("database", "schema", "warehouse", "role"):
        if doc.get(k):
            out[k] = doc[k]
    if doc.get("stats"):
        out["stats"] = doc["stats"]
    return out


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    import re
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("query", ROUTES)

    cfg = jci.load_backend()
    max_rows = int(cfg.get("max_rows", 10000))
    ro_role = cfg.get("readonly_role", "").strip()
    try:
        if method == "GET" and p == "/tables":
            schema = qs.get("schema", [cfg.get("schema", "")])[0]
            sql = f"SHOW TERSE TABLES IN SCHEMA {schema}" if schema else "SHOW TERSE TABLES"
            doc = submit(cfg, sql, {"role": ro_role} if ro_role else {})
            return jci.send_json(200, collect(cfg, doc, max_rows))

        m = re.fullmatch(r"/statements/([A-Za-z0-9-]+)", p)
        if m and method == "GET":
            status, doc = call(cfg, "GET", f"/api/v2/statements/{quote(m.group(1))}")
            if status == 202:
                return jci.send_json(202, {"statementHandle": m.group(1), "state": "running"})
            return jci.send_json(200, collect(cfg, doc, max_rows))

        if p == "/query":
            if method == "GET":
                sql = qs.get("sql", [""])[0]
                if not sql:
                    return jci.send_error(400, "missing ?sql=")
                overrides = {k: qs.get(k, [None])[0] for k in ("database", "schema", "warehouse")}
                if ro_role:
                    overrides["role"] = ro_role
                try:
                    doc = submit(cfg, sql, overrides)
                except SnowflakeError as e:
                    if ro_role and ("privilege" in str(e).lower() or e.code == "003001"):
                        return jci.send_error(405, f"GET /query runs under role {ro_role}: {e}",
                                              hint="use POST /query to run statements that write", allow=["POST"])
                    raise
                out = collect(cfg, doc, max_rows)
                if not ro_role and wrote(doc.get("stats") or {}):
                    out["warning"] = ("this GET wrote rows; set readonly_role in backends.conf to make "
                                      "Snowflake refuse writes on GET")
                return jci.send_json(200, out)
            if method == "POST":
                overrides: dict = {}
                if "json" in headers.get("content-type", "").lower():
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except ValueError as e:
                        return jci.send_error(400, f"bad JSON body: {e}")
                    sql = data.get("sql", "")
                    overrides = {k: data.get(k) for k in ("database", "schema", "warehouse", "role")}
                else:
                    sql = body.decode("utf-8")
                if not sql.strip():
                    return jci.send_error(400, "empty SQL")
                return jci.send_json(200, collect(cfg, submit(cfg, sql, overrides), max_rows))
    except SnowflakeError as e:
        if e.status in (401, 403):
            return jci.send_error(502, f"Snowflake refused the token: {e}")
        if e.status in (400, 422):
            return jci.send_error(400, f"{e.code}: {e}" if e.code else str(e))
        return jci.send_error(502, f"Snowflake HTTP {e.status}: {e}")
    except TimeoutError as e:
        return jci.send_error(504, str(e))
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
