#!/usr/bin/env python3
"""HTTP -> MongoDB.

  GET    /dbs                                       database names
  GET    /db/{d}/colls                              collection names
  GET    /db/{d}/coll/{c}?filter={json}&limit=N&sort={json}   find
  POST   /db/{d}/coll/{c}                           insert_one, or insert_many for a JSON array
  DELETE /db/{d}/coll/{c}?filter={json}             delete_many

Filters and documents are Extended JSON (bson.json_util), so ObjectIds and
dates round-trip.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /dbs",
    "GET    /db/{d}/colls",
    "GET    /db/{d}/coll/{c}?filter={json}&limit=N&sort={json}",
    "POST   /db/{d}/coll/{c}       (JSON doc, or array for insert_many)",
    "DELETE /db/{d}/coll/{c}?filter={json}",
]


def client(cfg: dict):
    from pymongo import MongoClient
    uri = cfg.get("uri", "").strip()
    if not uri:
        raise jci.BackendConfigError("missing 'uri' in backends.conf")
    ms = int(jci.timeout(cfg) * 1000)
    return MongoClient(uri, serverSelectionTimeoutMS=ms, connectTimeoutMS=ms)


def _load(raw):
    from bson import json_util
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json_util.loads(raw)


def _reply(status: int, obj) -> None:
    from bson import json_util
    jci.send(status, json_util.dumps(obj), "application/json")


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    from pymongo.errors import PyMongoError
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("document", ROUTES)

    cfg = jci.load_backend()
    c = client(cfg)
    try:
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
            try:
                flt = _load(qs.get("filter", ["{}"])[0])
                srt = _load(qs["sort"][0]) if qs.get("sort") else None
            except Exception as e:
                return jci.send_error(400, f"bad filter/sort JSON: {e}")
            if method == "GET":
                cur = col.find(flt).limit(int(qs.get("limit", ["50"])[0]))
                if srt:
                    cur = cur.sort(list(srt.items()))
                docs = list(cur)
                return _reply(200, {"db": db, "collection": coll, "count": len(docs), "documents": docs})
            if method == "POST":
                if not body:
                    return jci.send_error(400, "empty body")
                try:
                    doc = _load(body)
                except Exception as e:
                    return jci.send_error(400, f"bad JSON body: {e}")
                if isinstance(doc, list):
                    r = col.insert_many(doc)
                    return _reply(200, {"db": db, "collection": coll,
                                        "insertedCount": len(r.inserted_ids), "insertedIds": r.inserted_ids})
                r = col.insert_one(doc)
                return _reply(200, {"db": db, "collection": coll, "insertedId": r.inserted_id})
            if method == "DELETE":
                r = col.delete_many(flt)
                return _reply(200, {"db": db, "collection": coll, "deletedCount": r.deleted_count})

        jci.send_error(404, f"no route for {method} {p}")
    except PyMongoError as e:
        jci.send_error(502, f"{type(e).__name__}: {e}")
    finally:
        c.close()


if __name__ == "__main__":
    jci.main(route)
