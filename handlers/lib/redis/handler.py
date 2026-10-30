#!/usr/bin/env python3
"""HTTP -> Redis. Two shapes:

  kv (keys):
    GET    /keys?prefix=X          KEYS X*   (or ?pattern=<glob> verbatim)
    GET    /kv/<k>                 GET; 404 if missing
    PUT    /kv/<k>                 SET; body = value; ?ex=<seconds>
    POST   /kv/<k>                 same as PUT
    DELETE /kv/<k>                 DEL

  stream (pub/sub channels):
    GET    /streams                PUBSUB CHANNELS *
    POST   /stream/<ch>            PUBLISH; body = message
    GET    /stream/<ch>/tail       SUBSCRIBE as SSE
    GET    /stream/<ch>/peek       501 -- pub/sub has no replay
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /keys?prefix=X          (or ?pattern=<glob>)",
    "GET    /kv/{k}",
    "PUT    /kv/{k}                 (body = value; ?ex=<seconds>)",
    "POST   /kv/{k}                 (same as PUT)",
    "DELETE /kv/{k}",
    "GET    /streams                (PUBSUB CHANNELS *)",
    "POST   /stream/{ch}            (PUBLISH; body = message)",
    "GET    /stream/{ch}/tail       (SUBSCRIBE; SSE stream)",
]


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def client(cfg: dict):
    import redis
    t = jci.timeout(cfg)
    if cfg.get("url"):
        return redis.Redis.from_url(cfg["url"], socket_connect_timeout=t,
                                    decode_responses=False)
    kw = dict(host=cfg.get("host", "localhost"), port=int(cfg.get("port", 6379)),
              socket_connect_timeout=t, decode_responses=False)
    if cfg.get("username"):
        kw["username"] = cfg["username"]
    if cfg.get("password"):
        kw["password"] = cfg["password"]
    if _truthy(cfg.get("tls")):
        kw["ssl"] = True
        if cfg.get("ca_cert"):
            kw["ssl_ca_certs"] = cfg["ca_cert"]
    return redis.Redis(**kw)


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info(["kv", "stream"], ROUTES)

    cfg = jci.load_backend()
    r = client(cfg)

    # ---- kv -----------------------------------------------------------------

    if method == "GET" and p == "/keys":
        if "pattern" in qs:
            pattern = qs["pattern"][0]
        elif "prefix" in qs:
            pattern = qs["prefix"][0] + "*"
        else:
            pattern = "*"
        keys = sorted(k.decode("utf-8", "replace") for k in r.keys(pattern))
        return jci.send_json(200, {"pattern": pattern, "count": len(keys), "keys": keys})

    m = re.fullmatch(r"/kv/(.+)", p)
    if m:
        key = m.group(1)
        if method == "GET":
            val = r.get(key)
            if val is None:
                return jci.send_error(404, "not found", key=key)
            try:
                return jci.send(200, val.decode("utf-8"), "text/plain; charset=utf-8")
            except UnicodeDecodeError:
                return jci.send(200, val, "application/octet-stream")
        if method in ("PUT", "POST"):
            ex = qs.get("ex", [None])[0]
            r.set(key, body, ex=int(ex) if ex else None)
            return jci.send_json(200, {"key": key, "set": True, "bytes": len(body)})
        if method == "DELETE":
            return jci.send_json(200, {"key": key, "deleted": int(r.delete(key))})

    # ---- stream (pub/sub) --------------------------------------------------

    if method == "GET" and p == "/streams":
        chans = sorted(c.decode("utf-8", "replace") for c in r.pubsub_channels())
        return jci.send_json(200, {"streams": chans, "count": len(chans)})

    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m and method == "POST":
        ch = m.group(1)
        bad = jci.payload_error(cfg, ch, body)
        if bad:
            return jci.send_error(400, bad, channel=ch)
        return jci.send_json(200, {"name": ch, "receivers": int(r.publish(ch, body))})

    if re.fullmatch(r"/stream/([^/]+)/peek", p):
        return jci.send_error(501, "redis pub/sub has no replay; use /tail")

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        ch = m.group(1)
        ps = r.pubsub()
        ps.subscribe(ch)
        jci.send_sse_headers()
        jci.sse_comment(f"subscribed to {ch}")
        try:
            for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                data = msg["data"]
                if isinstance(data, (bytes, bytearray)):
                    data = data.decode("utf-8", "replace")
                jci.sse_event({"name": ch, "data": data})
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                ps.close()
            except Exception:
                pass
        return

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
