#!/usr/bin/env python3
"""HTTP -> RabbitMQ (AMQP 0-9-1) adapter spawned per intercepted request.

Routes:
  GET  /queues                                list queues via management HTTP API
  POST /queue/{q}/declare                     queue_declare (durable=false, auto_delete=false)
  POST /queue/{q}/publish                     basic_publish to default exchange, routing_key=q
  GET  /queue/{q}/get                         basic_get one message (auto-ack)
  POST /queue/{q}/purge                       queue_purge
  POST /exchange/{x}/publish?routing_key=r    basic_publish to named exchange
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
    "routes": [
        "GET  /_info",
        "GET  /queues",
        "POST /queue/{q}/declare",
        "POST /queue/{q}/publish    (body = message)",
        "GET  /queue/{q}/get",
        "GET  /queue/{q}/consume    (SSE stream)",
        "POST /queue/{q}/purge",
        "POST /exchange/{x}/publish?routing_key=r",
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

    m = re.fullmatch(r"/queue/([^/]+)/consume", p)
    if m and method == "GET":
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            send_sse_headers(handler="rabbit")
            sse_comment(f"consuming queue {q}")
            try:
                for mf, props, msg_body in ch.consume(
                        queue=q, auto_ack=True, inactivity_timeout=1.0):
                    if mf is None:
                        sse_comment("idle")
                        continue
                    sse_event({
                        "queue": q,
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

    if method == "GET" and p == "/queues":
        queues = mgmt_get("/queues")
        return send_json(200, {"queues": [
            {"name": q["name"], "vhost": q.get("vhost"),
             "messages": q.get("messages"), "consumers": q.get("consumers")}
            for q in queues
        ]}, handler="rabbit")

    m = re.fullmatch(r"/queue/([^/]+)/declare", p)
    if m and method == "POST":
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ok = ch.queue_declare(queue=q)
            return send_json(200, {
                "queue": q,
                "messageCount": ok.method.message_count,
                "consumerCount": ok.method.consumer_count,
            }, handler="rabbit")
        finally:
            conn.close()

    m = re.fullmatch(r"/queue/([^/]+)/publish", p)
    if m and method == "POST":
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            ch.basic_publish(exchange="", routing_key=q, body=body)
            return send_json(200, {"queue": q, "published": True, "bytes": len(body)},
                             handler="rabbit")
        finally:
            conn.close()

    m = re.fullmatch(r"/queue/([^/]+)/get", p)
    if m and method == "GET":
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            method_frame, props, msg_body = ch.basic_get(queue=q, auto_ack=True)
            if method_frame is None:
                return send_json(200, {"queue": q, "message": None}, handler="rabbit")
            return send_json(200, {
                "queue": q,
                "deliveryTag": method_frame.delivery_tag,
                "redelivered": method_frame.redelivered,
                "exchange": method_frame.exchange,
                "routingKey": method_frame.routing_key,
                "body": msg_body.decode("utf-8", "replace"),
            }, handler="rabbit")
        finally:
            conn.close()

    m = re.fullmatch(r"/queue/([^/]+)/purge", p)
    if m and method == "POST":
        q = m.group(1)
        conn = amqp_conn()
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            ok = ch.queue_purge(queue=q)
            return send_json(200, {"queue": q, "purged": ok.method.message_count},
                             handler="rabbit")
        finally:
            conn.close()

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
