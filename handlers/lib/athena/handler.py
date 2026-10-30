#!/usr/bin/env python3
"""HTTP -> Amazon Athena. Stdlib only, signed with jci_aws. The `query` shape:

  GET  /databases                 databases in the catalog
  GET  /tables?database=D         tables in a database
  GET  /query?sql=SELECT...       start, poll, page -> {columns, types, rows, rowCount, executionId, ...}
  POST /query                     body = SQL (text) or {"sql", "database", "workgroup"}
  GET  /executions/{id}           status and results of an earlier execution

Athena has no pre-execution classifier, so GET here cannot refuse DDL or
DML before it runs -- the credential's IAM / Lake Formation grants are the
read-only mechanism (ADR 0005), and the README ships the policy. What the
handler can do it does: GET reports Athena's post-hoc StatementType and
adds a `warning` when a GET turned out to run DDL or DML, so a credential
that isn't actually read-only shows itself the first time.
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import jci
import jci_aws

ROUTES = [
    "GET    /_info",
    "GET    /databases",
    "GET    /tables?database=D",
    "GET    /query?sql=SELECT...    (runs it; read-only only if the credential is -- see README)",
    "POST   /query                  (body = SQL or {sql, database, workgroup})",
    "GET    /executions/{id}",
]

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class AthenaError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def call(cfg: dict, action: str, params: dict) -> dict:
    """One Athena JSON API call, SigV4-signed."""
    region, _ = jci_aws.region_and_service(cfg, "athena")
    cfg = {**cfg, "upstream": cfg.get("upstream") or f"https://athena.{region}.amazonaws.com"}
    u = jci.upstream_target(cfg)
    default_port = 443 if u.scheme == "https" else 80
    authority = (u.hostname or "") + (f":{u.port}" if u.port and u.port != default_port else "")
    path = u.path or "/"
    body = json.dumps(params).encode("utf-8")
    headers = {"host": authority, "content-type": "application/x-amz-json-1.1",
               "x-amz-target": f"AmazonAthena.{action}"}
    headers.update(jci_aws.sign_request(cfg, "POST", path, "", headers, body, default_service="athena"))
    conn = jci.connect_upstream(cfg, u)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except ValueError:
        doc = {"message": raw[:300].decode("utf-8", "replace")}
    if resp.status >= 400:
        code = str(doc.get("__type", "")).split("#")[-1] or f"HTTP{resp.status}"
        raise AthenaError(resp.status, code, doc.get("message") or doc.get("Message") or code)
    return doc


# ---------------------------------------------------------------------------
# results

def columns(meta: dict) -> tuple[list[str], list[str]]:
    info = (meta or {}).get("ColumnInfo") or []
    return [c.get("Name", "") for c in info], [c.get("Type", "") for c in info]


def _scalar(t: str, v):
    if v is None:
        return None
    t = (t or "").lower()
    if t in ("tinyint", "smallint", "integer", "int", "bigint"):
        return int(v)
    if t in ("double", "float", "real"):
        return float(v)
    if t == "boolean":
        return v.lower() == "true"
    return v   # varchar, decimal, date, timestamp, json, array/map/row as text


def first_row_is_header(meta: dict, rows: list) -> bool:
    """Athena's first result row of a SELECT repeats the column names."""
    names, _ = columns(meta)
    if not rows or not names:
        return False
    first = [c.get("VarCharValue") for c in rows[0].get("Data", [])]
    return first == names


def convert_rows(meta: dict, rows: list, skip_header: bool) -> list[list]:
    _, types = columns(meta)
    out = []
    for r in (rows[1:] if skip_header else rows):
        out.append([_scalar(t, c.get("VarCharValue")) for t, c in zip(types, r.get("Data", []))])
    return out


# ---------------------------------------------------------------------------
# executions

def start(cfg: dict, sql: str, database: str | None, workgroup: str | None) -> str:
    params: dict = {"QueryString": sql}
    ctx = {}
    if database or cfg.get("database"):
        ctx["Database"] = database or cfg["database"]
    if cfg.get("catalog"):
        ctx["Catalog"] = cfg["catalog"]
    if ctx:
        params["QueryExecutionContext"] = ctx
    if workgroup or cfg.get("workgroup"):
        params["WorkGroup"] = workgroup or cfg["workgroup"]
    if cfg.get("output"):
        params["ResultConfiguration"] = {"OutputLocation": cfg["output"]}
    return call(cfg, "StartQueryExecution", params)["QueryExecutionId"]


def wait(cfg: dict, qid: str, max_wait: float = 600.0) -> dict:
    deadline = time.monotonic() + max_wait
    delay = 0.3
    while True:
        ex = call(cfg, "GetQueryExecution", {"QueryExecutionId": qid}).get("QueryExecution") or {}
        state = (ex.get("Status") or {}).get("State")
        if state in TERMINAL:
            return ex
        if time.monotonic() > deadline:
            raise TimeoutError(f"execution {qid} still {state} after {int(max_wait)}s")
        time.sleep(delay)
        delay = min(delay * 1.5, 3.0)


def results(cfg: dict, qid: str, max_rows: int) -> tuple[dict, list[list], bool]:
    res = call(cfg, "GetQueryResults", {"QueryExecutionId": qid, "MaxResults": min(1000, max_rows + 1)})
    rs = res.get("ResultSet") or {}
    meta = rs.get("ResultSetMetadata") or {}
    raw_rows = rs.get("Rows") or []
    rows = convert_rows(meta, raw_rows, first_row_is_header(meta, raw_rows))
    while res.get("NextToken") and len(rows) < max_rows:
        res = call(cfg, "GetQueryResults", {"QueryExecutionId": qid, "NextToken": res["NextToken"],
                                            "MaxResults": min(1000, max_rows - len(rows))})
        rows.extend(convert_rows(meta, (res.get("ResultSet") or {}).get("Rows") or [], False))
    return meta, rows[:max_rows], bool(res.get("NextToken")) or len(rows) > max_rows


def describe(cfg: dict, ex: dict, from_get: bool, max_rows: int) -> None:
    qid = ex.get("QueryExecutionId")
    status = ex.get("Status") or {}
    stats = ex.get("Statistics") or {}
    out: dict = {"executionId": qid, "state": status.get("State"),
                 "statementType": ex.get("StatementType"),
                 "dataScannedBytes": int(stats.get("DataScannedInBytes") or 0),
                 "executionMillis": int(stats.get("TotalExecutionTimeInMillis") or 0)}
    if status.get("State") != "SUCCEEDED":
        return jci.send_error(400 if status.get("State") == "FAILED" else 502,
                              status.get("StateChangeReason") or f"query {status.get('State')}", **out)
    meta, rows, truncated = results(cfg, qid, max_rows)
    cols, types = columns(meta)
    if cols:
        out.update({"columns": cols, "types": types, "rows": rows, "rowCount": len(rows)})
    else:
        out["rowCount"] = 0
    if truncated:
        out["truncated"] = True
    if from_get and ex.get("StatementType") in ("DDL", "DML") and not _looks_like_select(ex.get("Query", "")):
        out["warning"] = ("this GET executed a statement Athena classifies as "
                          f"{ex.get('StatementType')}; this credential is not read-only -- see the README")
    jci.send_json(200, out)


def _looks_like_select(sql: str) -> bool:
    """Athena files SELECT under DML. The warning is about the *other* DML
    (INSERT, UPDATE, DELETE, MERGE) and DDL; this only decides whether to warn."""
    return (sql.strip().split() or [""])[0].upper() in ("SELECT", "WITH")


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    import re
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("query", ROUTES)

    cfg = jci.load_backend()
    max_rows = int(cfg.get("max_rows", 10000))
    try:
        if method == "GET" and p == "/databases":
            res = call(cfg, "ListDatabases", {"CatalogName": cfg.get("catalog", "AwsDataCatalog")})
            return jci.send_json(200, {"databases": sorted(d.get("Name", "") for d in res.get("DatabaseList") or [])})
        if method == "GET" and p == "/tables":
            db = qs.get("database", [cfg.get("database", "")])[0]
            if not db:
                return jci.send_error(400, "missing ?database= (or a `database` default in backends.conf)")
            res = call(cfg, "ListTableMetadata", {"CatalogName": cfg.get("catalog", "AwsDataCatalog"),
                                                  "DatabaseName": db})
            return jci.send_json(200, {"database": db, "tables": sorted(
                ({"name": t.get("Name", ""), "type": t.get("TableType", "")} for t in res.get("TableMetadataList") or []),
                key=lambda t: t["name"])})
        m = re.fullmatch(r"/executions/([A-Za-z0-9-]+)", p)
        if m and method == "GET":
            ex = call(cfg, "GetQueryExecution", {"QueryExecutionId": m.group(1)}).get("QueryExecution") or {}
            return describe(cfg, ex, from_get=False, max_rows=max_rows)
        if p == "/query":
            if method == "GET":
                sql = qs.get("sql", [""])[0]
                if not sql:
                    return jci.send_error(400, "missing ?sql=")
                qid = start(cfg, sql, qs.get("database", [None])[0], qs.get("workgroup", [None])[0])
                return describe(cfg, wait(cfg, qid), from_get=True, max_rows=max_rows)
            if method == "POST":
                database = workgroup = None
                if "json" in headers.get("content-type", "").lower():
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except ValueError as e:
                        return jci.send_error(400, f"bad JSON body: {e}")
                    sql, database, workgroup = data.get("sql", ""), data.get("database"), data.get("workgroup")
                else:
                    sql = body.decode("utf-8")
                if not sql.strip():
                    return jci.send_error(400, "empty SQL")
                qid = start(cfg, sql, database, workgroup)
                return describe(cfg, wait(cfg, qid), from_get=False, max_rows=max_rows)
    except AthenaError as e:
        if e.status in (401, 403) or "Signature" in e.code or "signature" in str(e).lower():
            return jci.send_error(502, f"Athena refused the request: {e.code}: {e}")
        if e.status == 400:
            return jci.send_error(400, f"{e.code}: {e}")
        return jci.send_error(502, f"Athena HTTP {e.status}: {e.code}: {e}")
    except TimeoutError as e:
        return jci.send_error(504, str(e))
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
