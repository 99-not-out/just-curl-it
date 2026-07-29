#!/usr/bin/env python3
"""HTTP -> RabbitMQ (AMQP 0-9-1) adapter spawned per intercepted request.

Implements the unified `stream` shape (queues are streams here):

  GET    /streams                       list queues via management API
  GET    /stream/<q>                    queue_declare (idempotent) + counts
  POST   /stream/<q>                    basic_publish; body = message
  GET    /stream/<q>/peek?limit=N       basic_get N (DESTRUCTIVE; auto-ack)
  GET    /stream/<q>/tail               basic_consume SSE
  DELETE /stream/<q>                    queue_purge

Plus the non-stream:
  POST   /exchange/<x>/publish?routing_key=r    basic_publish to a named exchange

Note: peek for Rabbit is destructive (auto_ack=true). Kafka's peek is not.
This is the only place the unified shape is leaky; documented in /_info.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import traceback
import urllib.parse
import urllib.request
from urllib.parse import parse_qs, urlsplit

from shim import (read_request, send_json, load_backend,
                  send_sse_headers, sse_event, sse_comment)

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "rabbit",
    "shape": "stream",
    "routes": [
        "GET    /_info",
        "GET    /streams",
        "GET    /stream/{q}                  (idempotent declare + counts)",
        "POST   /stream/{q}                  (body = message)",
        "GET    /stream/{q}/peek?limit=N     (destructive; basic_get with auto-ack)",
        "GET    /stream/{q}/tail             (SSE stream)",
        "DELETE /stream/{q}                  (purge)",
        "POST   /exchange/{x}/publish?routing_key=r",
    ],
}


def amqp_conn():
    import pika
    cfg = load_backend(HOST)
    return pika.BlockingConnection(pika.URLParameters(cfg["url"]))


def mgmt_get(path: str):
    cfg = load_backend(HOST)
    mgmt = cfg.get("mgmt_url", "http://guest:guest@rabbit:15672")
    u = urllib.parse.urlsplit(mgmt)
    auth = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
    url = f"{u.scheme}://{u.hostname}:{u.port or 15672}/api{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


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
                         handler="rabbit")

    if method == "GET" and p == "/streams":
        queues = mgmt_get("/queues")
        return send_json(200, {"streams": [
            {"name": q["name"], "vhost": q.get("vhost"),
             "messages": q.get("messages"), "consumers": q.get("consumers")}
            for q in queues
        ]}, handler="rabbit")

    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m:
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            if method == "GET":
                ok = ch.queue_declare(queue=q)
                return send_json(200, {
                    "name": q,
                    "messageCount": ok.method.message_count,
                    "consumerCount": ok.method.consumer_count,
                }, handler="rabbit")
            if method == "POST":
                ch.queue_declare(queue=q)
                ch.basic_publish(exchange="", routing_key=q, body=body)
                return send_json(200, {
                    "name": q, "published": True, "bytes": len(body),
                }, handler="rabbit")
            if method == "DELETE":
                ch.queue_declare(queue=q)
                ok = ch.queue_purge(queue=q)
                return send_json(200, {"name": q, "purged": ok.method.message_count},
                                 handler="rabbit")
        finally:
            conn.close()

    m = re.fullmatch(r"/stream/([^/]+)/peek", p)
    if m and method == "GET":
        q = m.group(1)
        limit = int(qs.get("limit", ["1"])[0])
        conn = amqp_conn()
        msgs = []
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            for _ in range(max(1, limit)):
                mf, _props, msg_body = ch.basic_get(queue=q, auto_ack=True)
                if mf is None:
                    break
                msgs.append({
                    "deliveryTag": mf.delivery_tag,
                    "redelivered": mf.redelivered,
                    "exchange": mf.exchange,
                    "routingKey": mf.routing_key,
                    "body": msg_body.decode("utf-8", "replace"),
                })
            return send_json(200, {
                "name": q, "count": len(msgs),
                "destructive": True,  # peek for Rabbit consumes
                "messages": msgs,
            }, handler="rabbit")
        finally:
            conn.close()

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            send_sse_headers(handler="rabbit")
            sse_comment(f"tailing queue {q}")
            try:
                for mf, _props, msg_body in ch.consume(
                        queue=q, auto_ack=True, inactivity_timeout=1.0):
                    if mf is None:
                        sse_comment("idle")
                        continue
                    sse_event({
                        "name": q,
                        "deliveryTag": mf.delivery_tag,
                        "redelivered": mf.redelivered,
                        "exchange": mf.exchange,
                        "routingKey": mf.routing_key,
                        "body": msg_body.decode("utf-8", "replace"),
                    })
            except (BrokenPipeError, OSError):
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return

    m = re.fullmatch(r"/exchange/([^/]+)/publish", p)
    if m and method == "POST":
        x = m.group(1)
        rk = qs.get("routing_key", [""])[0]
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ch.basic_publish(exchange=x, routing_key=rk, body=body)
            return send_json(200, {
                "exchange": x, "routingKey": rk,
                "published": True, "bytes": len(body),
            }, handler="rabbit")
        finally:
            conn.close()

    send_json(404, {"error": f"no route for {method} {p}"}, handler="rabbit")


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"}, handler="rabbit")
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"}, handler="rabbit")


if __name__ == "__main__":
    main()
