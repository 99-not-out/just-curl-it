#!/usr/bin/env python3
"""HTTP -> Redis adapter spawned per intercepted request.

Implements both unified shapes:

  kv shape (Redis keys):
    GET    /keys?prefix=X            KEYS X*  (or ?pattern=glob for raw)
    GET    /kv/<k>                   GET; 404 if missing
    PUT    /kv/<k>                   SET; body = value; ?ex=<seconds>
    POST   /kv/<k>                   same as PUT
    DELETE /kv/<k>                   DEL

  stream shape (Redis pubsub channels):
    GET    /streams                  PUBSUB CHANNELS *
    POST   /stream/<ch>              PUBLISH; body = message
    GET    /stream/<ch>/tail         SUBSCRIBE (SSE)
    /stream/<ch>/peek and DELETE     -> 501 (pubsub has no replay/drop)
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from urllib.parse import parse_qs, urlsplit

from shim import (read_request, send, send_json, load_backend,
                  send_sse_headers, sse_event, sse_comment)

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "redis",
    "shape": ["kv", "stream"],
    "routes": [
        "GET    /_info",
        "GET    /keys?prefix=X       (or ?pattern=glob)",
        "GET    /kv/{k}",
        "PUT    /kv/{k}              (body = value, ?ex=<seconds>)",
        "POST   /kv/{k}              (same as PUT)",
        "DELETE /kv/{k}",
        "GET    /streams             (PUBSUB CHANNELS *)",
        "POST   /stream/{ch}         (PUBLISH; body = message)",
        "GET    /stream/{ch}/tail    (SUBSCRIBE; SSE stream)",
    ],
}


def client():
    import redis
    cfg = load_backend(HOST)
    return redis.Redis(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 6379)),
        decode_responses=False,
    )


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
                         handler="redis")

    r = client()

    # ---- kv shape ----------------------------------------------------------

    if method == "GET" and p == "/keys":
        # ?prefix=foo  -> glob "foo*"; ?pattern=raw passes through.
        if "pattern" in qs:
            pattern = qs["pattern"][0]
        elif "prefix" in qs:
            pattern = qs["prefix"][0] + "*"
        else:
            pattern = "*"
        keys = [k.decode("utf-8", "replace") for k in r.keys(pattern)]
        return send_json(200, {"pattern": pattern, "count": len(keys), "keys": keys},
                         handler="redis")

    m = re.fullmatch(r"/kv/(.+)", p)
    if m:
        key = m.group(1)
        if method == "GET":
            val = r.get(key)
            if val is None:
                return send_json(404, {"key": key, "error": "not found"}, handler="redis")
            try:
                return send(200, val.decode("utf-8"),
                            "text/plain; charset=utf-8", handler="redis")
            except UnicodeDecodeError:
                return send(200, val, "application/octet-stream", handler="redis")
        if method in ("PUT", "POST"):
            ex = qs.get("ex", [None])[0]
            r.set(key, body, ex=int(ex) if ex else None)
            return send_json(200, {"key": key, "set": True, "bytes": len(body)},
                             handler="redis")
        if method == "DELETE":
            n = r.delete(key)
            return send_json(200, {"key": key, "deleted": int(n)}, handler="redis")

    # ---- stream shape (pubsub) --------------------------------------------

    if method == "GET" and p == "/streams":
        chans = [c.decode("utf-8", "replace") for c in r.pubsub_channels()]
        return send_json(200, {"streams": chans, "count": len(chans)},
                         handler="redis")

    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m and method == "POST":
        ch = m.group(1)
        n = r.publish(ch, body)
        return send_json(200, {"name": ch, "receivers": int(n)}, handler="redis")

    m = re.fullmatch(r"/stream/([^/]+)/peek", p)
    if m:
        return send_json(501, {
            "error": "redis: peek not supported (pubsub has no replay)"},
            handler="redis")

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        ch = m.group(1)
        ps = r.pubsub()
        ps.subscribe(ch)
        send_sse_headers(handler="redis")
        sse_comment(f"subscribed to {ch}")
        try:
            for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                data = msg["data"]
                if isinstance(data, (bytes, bytearray)):
                    try:
                        data = data.decode("utf-8")
                    except UnicodeDecodeError:
                        data = data.decode("utf-8", "replace")
                sse_event({"name": ch, "data": data})
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                ps.close()
            except Exception:
                pass
        return

    send_json(404, {"error": f"no route for {method} {p}"}, handler="redis")


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"}, handler="redis")
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"}, handler="redis")


if __name__ == "__main__":
    main()
