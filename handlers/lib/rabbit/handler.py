#!/usr/bin/env python3
"""HTTP -> RabbitMQ (AMQP 0-9-1). The `stream` shape over queues:

  GET    /streams                     queues via the management API
  GET    /stream/<q>                  declare (idempotent) + counts
  POST   /stream/<q>                  publish to the default exchange with routing key q
  GET    /stream/<q>/peek?limit=N     up to N messages, non-destructive (requeued)
  GET    /stream/<q>/tail             consume as SSE, non-destructive (requeued on disconnect)
  DELETE /stream/<q>                  purge
  POST   /exchange/<x>/publish?routing_key=r    publish to a named exchange

Peek fetches with auto_ack=False and nacks each message back with
requeue=True, so it is still on the queue afterwards (flagged redelivered).
Tail consumes with auto_ack=False and never acks: messages are held unacked
while the tail is open -- invisible to other consumers meanwhile -- and the
broker requeues them when the tail disconnects. That is the honest cost of
watching an AMQP queue without consuming it.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /streams                        (queues, via management API)",
    "GET    /stream/{q}                     (declare + counts)",
    "POST   /stream/{q}                     (publish; body = message)",
    "GET    /stream/{q}/peek?limit=1        (non-destructive: messages are requeued)",
    "GET    /stream/{q}/tail                (SSE; held unacked while open, requeued on disconnect)",
    "DELETE /stream/{q}                     (purge)",
    "POST   /exchange/{x}/publish?routing_key=r",
]


def amqp_conn(cfg: dict):
    import pika
    url = cfg.get("url", "").strip()
    if not url:
        raise jci.BackendConfigError("missing 'url' in backends.conf")
    params = pika.URLParameters(url)
    params.socket_timeout = jci.timeout(cfg)
    params.blocked_connection_timeout = jci.timeout(cfg)
    return pika.BlockingConnection(params)


def mgmt_get(cfg: dict, path: str):
    mgmt = cfg.get("mgmt_url", "").strip()
    if not mgmt:
        raise jci.BackendConfigError("GET /streams needs 'mgmt_url' (the management API) in backends.conf")
    u = urllib.parse.urlsplit(mgmt)
    auth = base64.b64encode(f"{u.username or 'guest'}:{u.password or 'guest'}".encode()).decode()
    url = f"{u.scheme}://{u.hostname}:{u.port or 15672}/api{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=jci.timeout(cfg)) as resp:
        return json.loads(resp.read())


def _msg(mf, body: bytes) -> dict:
    return {"deliveryTag": mf.delivery_tag, "redelivered": mf.redelivered,
            "exchange": mf.exchange, "routingKey": mf.routing_key,
            "body": body.decode("utf-8", "replace")}


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("stream", ROUTES)

    cfg = jci.load_backend()

    if method == "GET" and p == "/streams":
        queues = mgmt_get(cfg, "/queues")
        return jci.send_json(200, {"streams": [
            {"name": q["name"], "vhost": q.get("vhost"), "messages": q.get("messages"),
             "consumers": q.get("consumers")} for q in queues]})

    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m:
        q = m.group(1)
        conn = amqp_conn(cfg)
        try:
            ch = conn.channel()
            if method == "GET":
                ok = ch.queue_declare(queue=q)
                return jci.send_json(200, {"name": q, "messageCount": ok.method.message_count,
                                           "consumerCount": ok.method.consumer_count})
            if method == "POST":
                ch.queue_declare(queue=q)
                ch.basic_publish(exchange="", routing_key=q, body=body)
                return jci.send_json(200, {"name": q, "published": True, "bytes": len(body)})
            if method == "DELETE":
                ch.queue_declare(queue=q)
                ok = ch.queue_purge(queue=q)
                return jci.send_json(200, {"name": q, "purged": ok.method.message_count})
        finally:
            conn.close()

    m = re.fullmatch(r"/stream/([^/]+)/peek", p)
    if m and method == "GET":
        q = m.group(1)
        limit = max(1, int(qs.get("limit", ["1"])[0]))
        conn = amqp_conn(cfg)
        msgs, tags = [], []
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            for _ in range(limit):
                mf, _props, mbody = ch.basic_get(queue=q, auto_ack=False)
                if mf is None:
                    break
                msgs.append(_msg(mf, mbody))
                tags.append(mf.delivery_tag)
            # Put them all back. They come off the queue again as redelivered.
            for tag in tags:
                ch.basic_nack(delivery_tag=tag, requeue=True)
            return jci.send_json(200, {"name": q, "count": len(msgs), "destructive": False,
                                       "messages": msgs})
        finally:
            conn.close()

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        q = m.group(1)
        conn = amqp_conn(cfg)
        try:
            ch = conn.channel()
            ch.queue_declare(queue=q)
            jci.send_sse_headers()
            jci.sse_comment(f"tailing {q}; messages are held unacked and requeued when you disconnect")
            try:
                for mf, _props, mbody in ch.consume(queue=q, auto_ack=False, inactivity_timeout=1.0):
                    if mf is None:
                        jci.sse_comment("idle")
                        continue
                    jci.sse_event({"name": q, **_msg(mf, mbody)})
            except (BrokenPipeError, OSError):
                pass
        finally:
            try:
                conn.close()   # unacked deliveries are requeued by the broker
            except Exception:
                pass
        return

    m = re.fullmatch(r"/exchange/([^/]+)/publish", p)
    if m and method == "POST":
        x, rk = m.group(1), qs.get("routing_key", [""])[0]
        conn = amqp_conn(cfg)
        try:
            conn.channel().basic_publish(exchange=x, routing_key=rk, body=body)
            return jci.send_json(200, {"exchange": x, "routingKey": rk, "published": True,
                                       "bytes": len(body)})
        finally:
            conn.close()

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
