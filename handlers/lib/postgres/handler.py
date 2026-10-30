#!/usr/bin/env python3
"""HTTP -> PostgreSQL. `query` shape, plus `stream` shape over LISTEN/NOTIFY.

  query:
    GET  /tables                  tables outside pg_catalog / information_schema
    GET  /query?sql=SELECT...     READ ONLY transaction -> {columns, rows, rowCount}
    POST /query                   body = SQL (text) or {"sql": "...", "params": [...]} (JSON); may write

  stream (LISTEN/NOTIFY):
    POST /stream/<channel>        pg_notify(channel, body)
    GET  /stream/<channel>/tail   LISTEN channel; SSE stream

GET runs with default_transaction_read_only on, so Postgres itself refuses
writes (ADR 0005): `GET /query?sql=DROP+TABLE+x` is a 405 pointing at POST.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /tables",
    "GET    /query?sql=SELECT...    (READ ONLY transaction)",
    "POST   /query                  (body = SQL or {sql, params}; may mutate)",
    "POST   /stream/{channel}       (pg_notify; body = payload)",
    "GET    /stream/{channel}/tail  (LISTEN; SSE stream)",
]


def dsn(cfg: dict) -> str:
    """The configured DSN with connect_timeout added if absent; URI or keyword form."""
    d = cfg.get("dsn", "").strip()
    if not d:
        raise jci.BackendConfigError("missing 'dsn' in backends.conf")
    if "connect_timeout" in d:
        return d
    t = int(jci.timeout(cfg))
    if "://" in d:
        return d + ("&" if "?" in d else "?") + f"connect_timeout={t}"
    return f"{d} connect_timeout={t}"


def connect(cfg: dict, **kw):
    import psycopg
    return psycopg.connect(dsn(cfg), **kw)


def run_query(cfg: dict, sql: str, params=None, read_only: bool = False) -> None:
    from psycopg.errors import ReadOnlySqlTransaction
    with connect(cfg) as conn, conn.cursor() as cur:
        # HTTP semantics onto SQL: GET may not mutate. Postgres enforces it
        # (default_transaction_read_only), not a regex over the statement.
        conn.read_only = read_only
        try:
            cur.execute(sql, params or None)
        except ReadOnlySqlTransaction as e:
            return jci.send_error(405, f"GET /query is read-only: {e.diag.message_primary}",
                                  hint="use POST /query to run statements that write",
                                  allow=["POST"])
        if cur.description is None:
            conn.commit()
            return jci.send_json(200, {"rowCount": cur.rowcount})
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
        jci.send_json(200, {"columns": cols, "rowCount": len(rows),
                            "rows": [list(r) for r in rows]})


def parse_sql_from_body(headers: dict, body: bytes):
    if "json" in headers.get("content-type", "").lower():
        data = json.loads(body.decode("utf-8"))
        return data["sql"], data.get("params")
    return body.decode("utf-8"), None


def handle_tail(cfg: dict, channel: str) -> None:
    # LISTEN takes an identifier, not a parameter, so validate strictly
    # before interpolating it quoted.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", channel):
        return jci.send_error(400, "channel must be a SQL identifier")
    conn = connect(cfg, autocommit=True)
    try:
        conn.execute(f'LISTEN "{channel}"')
        jci.send_sse_headers()
        jci.sse_comment(f"LISTEN {channel} (try: curl -X POST http://{jci.HOST}/stream/{channel} -d hi)")
        try:
            while True:
                got_any = False
                for n in conn.notifies(timeout=1.0, stop_after=None):
                    got_any = True
                    jci.sse_event({"name": n.channel, "payload": n.payload, "pid": n.pid})
                if not got_any:
                    jci.sse_comment("idle")
        except (BrokenPipeError, OSError):
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handle_notify(cfg: dict, channel: str, payload: bytes) -> None:
    """pg_notify is a function -- both args bind safely with no quoting."""
    text = payload.decode("utf-8", "replace")
    with connect(cfg, autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (channel, text))
    jci.send_json(200, {"name": channel, "sent": True, "bytes": len(payload)})


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info(["query", "stream"], ROUTES)

    cfg = jci.load_backend()

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        return handle_tail(cfg, m.group(1))
    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m and method == "POST":
        return handle_notify(cfg, m.group(1), body)
    if re.fullmatch(r"/stream(/[^/]+)?(/peek)?", p) or p == "/streams":
        return jci.send_error(501, "postgres: only POST /stream/{ch} (NOTIFY) and "
                                   "GET /stream/{ch}/tail (LISTEN) are supported")

    if method == "GET" and p == "/tables":
        return run_query(cfg,
                         "SELECT schemaname, tablename FROM pg_catalog.pg_tables "
                         "WHERE schemaname NOT IN ('pg_catalog','information_schema') "
                         "ORDER BY schemaname, tablename", read_only=True)

    if p == "/query":
        if method == "GET":
            sql = qs.get("sql", [""])[0]
            if not sql:
                return jci.send_error(400, "missing ?sql=")
            return run_query(cfg, sql, read_only=True)
        if method == "POST":
            try:
                sql, params = parse_sql_from_body(headers, body)
            except Exception as e:
                return jci.send_error(400, f"bad body: {e}")
            return run_query(cfg, sql, params)

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
