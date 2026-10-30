#!/usr/bin/env python3
"""SQL over HTTP against a SQLite file. Same `query` shape as the Postgres
handler, so scripts can swap one for the other.

  GET  /tables              tables and views from sqlite_master
  GET  /query?sql=...       read-only: the file is opened with mode=ro
  POST /query               body = SQL, or {"sql": "...", "params": [...]}; may write

GET cannot mutate (ADR 0005) because SQLite itself refuses: the connection
is opened read-only and a write raises "attempt to write a readonly
database", which comes back as a 405 pointing at POST. A missing file is a
502, never a silently created empty database (mode=rw, not rwc).
"""
from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import parse_qs, quote, urlsplit

import jci

ROUTES = [
    "GET  /_info",
    "GET  /tables",
    "GET  /query?sql=SELECT...      (read-only: file opened with mode=ro)",
    "POST /query                    (body = SQL or {sql, params}; may write unless readonly = true)",
]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def connect(cfg: dict, write: bool) -> sqlite3.Connection:
    path = os.path.expanduser(cfg.get("path", "").strip())
    if not path:
        raise jci.BackendConfigError("missing 'path' in backends.conf")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    mode = "rw" if write else "ro"
    uri = f"file:{quote(path)}?mode={mode}"
    return sqlite3.connect(uri, uri=True, timeout=jci.timeout(cfg))


def run_query(cfg: dict, sql: str, params=None, write: bool = False) -> None:
    try:
        conn = connect(cfg, write)
    except FileNotFoundError as e:
        return jci.send_error(502, f"no such database: {e}")
    try:
        try:
            cur = conn.execute(sql, params or ())
        except sqlite3.OperationalError as e:
            if "readonly" in str(e):
                return jci.send_error(405, f"GET /query is read-only: {e}",
                                      hint="use POST /query to run statements that write",
                                      allow=["POST"])
            return jci.send_error(400, f"{type(e).__name__}: {e}")
        except sqlite3.Error as e:
            return jci.send_error(400, f"{type(e).__name__}: {e}")
        if cur.description is None:
            conn.commit()
            return jci.send_json(200, {"rowCount": cur.rowcount})
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        jci.send_json(200, {"columns": cols, "rowCount": len(rows),
                            "rows": [list(r) for r in rows]})
    finally:
        conn.close()


def parse_sql_from_body(headers: dict, body: bytes):
    if "json" in headers.get("content-type", "").lower():
        data = json.loads(body.decode("utf-8"))
        return data["sql"], data.get("params")
    return body.decode("utf-8"), None


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("query", ROUTES)

    cfg = jci.load_backend()

    if method == "GET" and p == "/tables":
        return run_query(cfg, "SELECT name, type FROM sqlite_master "
                              "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                              "ORDER BY name")

    if p == "/query":
        if method == "GET":
            sql = qs.get("sql", [""])[0]
            if not sql:
                return jci.send_error(400, "missing ?sql=")
            return run_query(cfg, sql)
        if method == "POST":
            if _truthy(cfg.get("readonly")):
                return jci.send_error(405, "this database is configured readonly = true in backends.conf",
                                      allow=["GET"])
            try:
                sql, params = parse_sql_from_body(headers, body)
            except Exception as e:
                return jci.send_error(400, f"bad body: {e}")
            return run_query(cfg, sql, params, write=True)

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
