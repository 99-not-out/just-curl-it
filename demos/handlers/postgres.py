#!/usr/bin/env python3
"""HTTP -> Postgres adapter spawned per intercepted request.

Implements `query` shape natively, plus `stream` shape for LISTEN/NOTIFY:

  query shape:
    GET  /tables                  list tables (excluding system schemas)
    GET  /query?sql=SELECT...     run SQL, returns {columns, rows, rowCount}
    POST /query                   body = SQL (text/plain) or
                                  {"sql": "...", "params": [...]} (application/json)

  GET runs inside a READ ONLY transaction, so a `GET`-only route in
  routes.conf really is read-only: `GET /query?sql=DROP+TABLE+x` is refused
  by Postgres with a 405 pointing at POST. Only POST may mutate.

  stream shape (LISTEN/NOTIFY):
    POST /stream/<channel>        pg_notify(channel, body); body = payload
    GET  /stream/<channel>/tail   LISTEN; SSE stream
    Other /stream/* verbs return 501 (Postgres has no replay/list/purge for these).
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from urllib.parse import parse_qs, urlsplit

from shim import (read_request, send_json, load_backend,
                  send_sse_headers, sse_event, sse_comment)

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "postgres",
    "shape": ["query", "stream"],
    "routes": [
        "GET    /_info",
        "GET    /tables",
        "GET    /query?sql=SELECT...    (READ ONLY transaction)",
        "POST   /query                  (body = SQL or {sql, params}; may mutate)",
        "POST   /stream/{channel}       (pg_notify; body = payload)",
        "GET    /stream/{channel}/tail  (LISTEN; SSE stream)",
    ],
}


def connect():
    import psycopg
    cfg = load_backend(HOST)
    return psycopg.connect(cfg["dsn"])


def run_query(sql: str, params=None, read_only: bool = False) -> None:
    from psycopg.errors import ReadOnlySqlTransaction
    with connect() as conn, conn.cursor() as cur:
        # HTTP semantics onto SQL: GET may not mutate. Postgres enforces it
        # (default_transaction_read_only), not a regex over the statement.
        conn.read_only = read_only
        try:
            cur.execute(sql, params or None)
        except ReadOnlySqlTransaction as e:
            return send_json(405, {
                "error": f"GET /query is read-only: {e.diag.message_primary}",
                "hint": "use POST /query to run statements that write",
                "allow": ["POST"],
            }, handler="postgres")
        if cur.description is None:
            conn.commit()
            return send_json(200, {"rowCount": cur.rowcount}, handler="postgres")
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
        send_json(200, {
            "columns": cols,
            "rowCount": len(rows),
            "rows": [list(r) for r in rows],
        }, handler="postgres")


def parse_sql_from_body(headers: dict, body: bytes):
    ct = headers.get("content-type", "").lower()
    if "json" in ct:
        data = json.loads(body.decode("utf-8"))
        return data["sql"], data.get("params")
    return body.decode("utf-8"), None


def handle_tail(channel: str) -> None:
    import psycopg
    # LISTEN doesn't accept parameters, so the channel is interpolated as a
    # quoted SQL identifier -- validate strictly to keep that safe.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", channel):
        return send_json(400, {"error": "channel must be a SQL identifier"},
                         handler="postgres")
    cfg = load_backend(HOST)
    conn = psycopg.connect(cfg["dsn"], autocommit=True)
    try:
        conn.execute(f'LISTEN "{channel}"')
        send_sse_headers(handler="postgres")
        sse_comment(f"LISTEN {channel} "
                    f"(try: curl -X POST http://$INTERCEPT_HOST/stream/{channel} "
                    f"-d 'hi')")
        try:
            while True:
                gen = conn.notifies(timeout=1.0, stop_after=None)
                got_any = False
                for n in gen:
                    got_any = True
                    sse_event({"name": n.channel, "payload": n.payload, "pid": n.pid})
                if not got_any:
                    sse_comment("idle")
        except (BrokenPipeError, OSError):
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handle_notify(channel: str, payload: bytes) -> None:
    """pg_notify is a function -- both args bind safely with no quoting."""
    import psycopg
    cfg = load_backend(HOST)
    text = payload.decode("utf-8", "replace")
    with psycopg.connect(cfg["dsn"], autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (channel, text))
    send_json(200, {"name": channel, "sent": True, "bytes": len(payload)},
              handler="postgres")


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        try:
            backend = load_backend(HOST)
        except Exception as e:
            backend = {"error": str(e)}
        return send_json(200, {**_INFO, "host": HOST, "backend": backend},
                         handler="postgres")

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        return handle_tail(m.group(1))

    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m and method == "POST":
        return handle_notify(m.group(1), body)

    if re.fullmatch(r"/stream(/[^/]+)?(/peek)?", p) or \
       re.fullmatch(r"/streams", p):
        return send_json(501, {
            "error": "postgres: only POST /stream/{ch} (NOTIFY) and "
                     "GET /stream/{ch}/tail (LISTEN) are supported"},
            handler="postgres")

    if method == "GET" and p == "/tables":
        return run_query(
            "SELECT schemaname, tablename "
            "FROM pg_catalog.pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog','information_schema') "
            "ORDER BY schemaname, tablename",
            read_only=True,
        )

    if p == "/query":
        if method == "GET":
            sql = qs.get("sql", [""])[0]
            if not sql:
                return send_json(400, {"error": "missing ?sql="}, handler="postgres")
            return run_query(sql, read_only=True)
        if method == "POST":
            try:
                sql, params = parse_sql_from_body(headers, body)
            except Exception as e:
                return send_json(400, {"error": f"bad body: {e}"}, handler="postgres")
            return run_query(sql, params)

    send_json(404, {"error": f"no route for {method} {p}"}, handler="postgres")


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"}, handler="postgres")
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"}, handler="postgres")


if __name__ == "__main__":
    main()
