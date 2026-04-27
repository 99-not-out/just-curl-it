#!/usr/bin/env python3
"""HTTP -> Postgres adapter spawned per intercepted request.

Routes:
  GET  /tables                  list tables (excluding system schemas)
  GET  /query?sql=SELECT...     run SQL, returns {columns, rows, rowCount}
  POST /query                   body = SQL (text/plain)
                                or {"sql": "...", "params": [...]}  (application/json)
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
    "routes": [
        "GET  /_info",
        "GET  /tables",
        "GET  /query?sql=SELECT...",
        "POST /query                (body = SQL or {sql, params})",
        "GET  /listen/{channel}     (SSE stream; pair with pg_notify)",
    ],
}


def connect():
    import psycopg
    cfg = load_backend(HOST)
    return psycopg.connect(cfg["dsn"])


def run_query(sql: str, params=None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
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


def handle_listen(channel: str) -> None:
    import psycopg
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", channel):
        return send_json(400, {"error": "channel must be a SQL identifier"},
                         handler="postgres")
    cfg = load_backend(HOST)
    conn = psycopg.connect(cfg["dsn"], autocommit=True)
    try:
        conn.execute(f'LISTEN "{channel}"')
        send_sse_headers(handler="postgres")
        sse_comment(f"LISTEN {channel} (try: select pg_notify('{channel}','hi'))")
        try:
            while True:
                gen = conn.notifies(timeout=1.0, stop_after=None)
                got_any = False
                for n in gen:
                    got_any = True
                    sse_event({"channel": n.channel, "payload": n.payload, "pid": n.pid})
                if not got_any:
                    sse_comment("idle")
        except (BrokenPipeError, OSError):
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


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

    m = re.fullmatch(r"/listen/([^/]+)", p)
    if m and method == "GET":
        return handle_listen(m.group(1))

    if method == "GET" and p == "/tables":
        return run_query(
            "SELECT schemaname, tablename "
            "FROM pg_catalog.pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog','information_schema') "
            "ORDER BY schemaname, tablename"
        )

    if p == "/query":
        if method == "GET":
            sql = qs.get("sql", [""])[0]
            if not sql:
                return send_json(400, {"error": "missing ?sql="}, handler="postgres")
            return run_query(sql)
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
