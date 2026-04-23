#!/usr/bin/env python3
"""HTTP -> MongoDB adapter spawned per intercepted request.

Routes:
  GET    /dbs                                           list database names
  GET    /db/{d}/colls                                  list collection names
  GET    /db/{d}/coll/{c}?filter={json}&limit=N&sort={json}   find
  POST   /db/{d}/coll/{c}                               insert_one (or insert_many if body is a JSON array)
  DELETE /db/{d}/coll/{c}?filter={json}                 delete_many
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from urllib.parse import parse_qs, urlsplit

from shim import read_request, send, send_json, load_backend

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "mongo",
    "routes": [
        "GET    /_info",
        "GET    /dbs",
        "GET    /db/{d}/colls",
        "GET    /db/{d}/coll/{c}?filter={json}&limit=N&sort={json}",
        "POST   /db/{d}/coll/{c}       (JSON doc, or array for insert_many)",
        "DELETE /db/{d}/coll/{c}?filter={json}",
    ],
}


def client():
    from pymongo import MongoClient
    cfg = load_backend(HOST)
    return MongoClient(cfg["uri"])


def _load(raw):
    from bson import json_util
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json_util.loads(raw)


def _reply(status: int, obj) -> None:
    from bson import json_util
    send(status, json_util.dumps(obj), "application/json", handler="mongo")


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
                         handler="mongo")

    c = client()

    if method == "GET" and p == "/dbs":
        return _reply(200, {"databases": c.list_database_names()})

    m = re.fullmatch(r"/db/([^/]+)/colls", p)
    if m and method == "GET":
        db = m.group(1)
        return _reply(200, {"database": db, "collections": c[db].list_collection_names()})

    m = re.fullmatch(r"/db/([^/]+)/coll/([^/]+)", p)
    if m:
        db, coll = m.group(1), m.group(2)
        col = c[db][coll]
        if method == "GET":
            flt = _load(qs.get("filter", ["{}"])[0])
            limit = int(qs.get("limit", ["50"])[0])
            cur = col.find(flt).limit(limit)
            srt = qs.get("sort", [None])[0]
            if srt:
                cur = cur.sort(list(_load(srt).items()))
            docs = list(cur)
            return _reply(200, {"db": db, "collection": coll,
                                "count": len(docs), "documents": docs})
        if method == "POST":
            if not body:
                return _reply(400, {"error": "empty body"})
            doc = _load(body)
            if isinstance(doc, list):
                r = col.insert_many(doc)
                return _reply(200, {"db": db, "collection": coll,
                                    "insertedCount": len(r.inserted_ids),
                                    "insertedIds": r.inserted_ids})
            r = col.insert_one(doc)
            return _reply(200, {"db": db, "collection": coll,
                                "insertedId": r.inserted_id})
        if method == "DELETE":
            flt = _load(qs.get("filter", ["{}"])[0])
            r = col.delete_many(flt)
            return _reply(200, {"db": db, "collection": coll,
                                "deletedCount": r.deleted_count})

    _reply(404, {"error": f"no route for {method} {p}"})


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return _reply(400, {"error": f"bad request: {e}"})
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        _reply(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
