#!/usr/bin/env python3
"""HTTP -> MySQL / MariaDB. The `query` shape, same as postgres and sqlite:

  GET  /tables                  tables in the configured database
  GET  /query?sql=SELECT...     READ ONLY transaction -> {columns, rows, rowCount}
  POST /query                   body = SQL (text) or {"sql": "...", "params": [...]} (JSON); may write

GET runs inside `START TRANSACTION READ ONLY`, which the server enforces
(ADR 0005), and the connection is opened without CLIENT_MULTI_STATEMENTS,
so "SELECT 1; DROP TABLE x" is a syntax error, not two statements.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /tables",
    "GET    /query?sql=SELECT...    (READ ONLY transaction, one statement)",
    "POST   /query                  (body = SQL or {sql, params}; may mutate)",
]

# MySQL error numbers we translate rather than pass through as 400.
_ER_READ_ONLY = {1290,   # OPTION_PREVENTS_STATEMENT (read_only server)
                 1792}   # CANT_EXECUTE_IN_READ_ONLY_TRANSACTION
_ER_PARSE_ERROR = 1064   # the parser itself rejected it (EXPLAIN gate)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def connect(cfg: dict):
    import pymysql
    kw = dict(host=cfg.get("host", "localhost"), port=int(cfg.get("port", 3306)),
              user=cfg.get("user", "root"), password=cfg.get("password", ""),
              database=cfg.get("database") or None,
              connect_timeout=int(jci.timeout(cfg)), read_timeout=60, write_timeout=60,
              charset="utf8mb4", autocommit=False)
    if cfg.get("unix_socket"):
        kw["unix_socket"] = cfg["unix_socket"]
    if _truthy(cfg.get("tls")):
        ssl: dict = {"ca": cfg["ca_cert"]} if cfg.get("ca_cert") else {}
        kw["ssl"] = ssl or {"check_hostname": False}
        kw["ssl_verify_identity"] = bool(cfg.get("ca_cert"))
    return pymysql.connect(**kw)


def run_query(cfg: dict, sql: str, params=None, read_only: bool = False) -> None:
    import pymysql
    conn = connect(cfg)
    try:
        with conn.cursor() as cur:
            if read_only:
                # Two server-side gates. READ ONLY refuses DML -- but MySQL DDL
                # implicitly *commits* the transaction and then runs, so it
                # needs a second one: EXPLAIN is accepted by the parser only for
                # SELECT/TABLE/DML and executes nothing. A statement EXPLAIN
                # rejects as syntax is not a query, and GET refuses it.
                cur.execute("START TRANSACTION READ ONLY")
                try:
                    cur.execute("EXPLAIN " + sql)
                    cur.fetchall()
                except pymysql.err.MySQLError as e:
                    if e.args and e.args[0] == _ER_PARSE_ERROR:
                        return jci.send_error(405, f"GET /query takes a single query (SELECT/TABLE): {e.args[-1]}",
                                              hint="use POST /query for DDL and scripts", allow=["POST"])
                    # anything else (unknown table, bad column): let the real
                    # execute report it as a 400 below
            try:
                cur.execute(sql, params or None)
            except pymysql.err.OperationalError as e:
                if e.args and e.args[0] in _ER_READ_ONLY:
                    return jci.send_error(405, f"GET /query is read-only: {e.args[1]}",
                                          hint="use POST /query to run statements that write",
                                          allow=["POST"])
                return jci.send_error(400, f"{type(e).__name__}: {e.args[-1]}")
            except pymysql.err.ProgrammingError as e:
                # A ';' in a GET lands here: multi-statements are disabled on
                # the connection, so the second statement is a syntax error.
                return jci.send_error(400, f"{type(e).__name__}: {e.args[-1]}")
            except pymysql.err.MySQLError as e:
                return jci.send_error(400, f"{type(e).__name__}: {e.args[-1]}")
            if cur.description is None:
                if not read_only:
                    conn.commit()
                return jci.send_json(200, {"rowCount": cur.rowcount})
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if not read_only:
                conn.commit()
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
    import pymysql
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("query", ROUTES)

    cfg = jci.load_backend()
    try:
        if method == "GET" and p == "/tables":
            return run_query(cfg,
                             "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                             "WHERE table_schema = DATABASE() ORDER BY table_name", read_only=True)
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
    except pymysql.err.OperationalError as e:
        # connect() failures: unreachable host, bad credentials, timeout
        return jci.send_error(502, f"{type(e).__name__}: {e.args[-1]}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
