#!/usr/bin/env python3
"""HTTP -> Redis adapter spawned per intercepted request.

Routes:
  GET    /info                       INFO server
  GET    /keys?pattern=*             KEYS
  GET    /key/{k}                    GET   (404 if missing)
  PUT    /key/{k}                    SET   body = value, optional ?ex=<seconds>
  POST   /key/{k}                    SET   (same as PUT)
  DELETE /key/{k}                    DEL
  POST   /publish/{channel}          PUBLISH  body = message
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
    "routes": [
        "GET    /_info",
        "GET    /info",
        "GET    /keys?pattern=*",
        "GET    /key/{k}",
        "PUT    /key/{k}      (body = value, ?ex=<seconds>)",
        "POST   /key/{k}      (same as PUT)",
        "DELETE /key/{k}",
        "POST   /publish/{channel}   (body = message)",
        "GET    /subscribe/{channel} (SSE stream)",
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

    if method == "GET" and p == "/info":
        return send_json(200, r.info(), handler="redis")

    m = re.fullmatch(r"/subscribe/([^/]+)", p)
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
                sse_event({"channel": ch, "data": data})
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                ps.close()
            except Exception:
                pass
        return

    if method == "GET" and p == "/keys":
        pattern = qs.get("pattern", ["*"])[0]
        keys = [k.decode("utf-8", "replace") for k in r.keys(pattern)]
        return send_json(200, {"pattern": pattern, "count": len(keys), "keys": keys},
                         handler="redis")

    m = re.fullmatch(r"/key/(.+)", p)
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

    m = re.fullmatch(r"/publish/([^/]+)", p)
    if m and method == "POST":
        ch = m.group(1)
        n = r.publish(ch, body)
        return send_json(200, {"channel": ch, "receivers": int(n)}, handler="redis")

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
