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
    "GET    /stream/{q}                     (counts; 404 if the queue doesn't exist)",
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


class QueueNotFound(Exception):
    pass


def _queue(conn, q: str, create: bool = False):
    """(channel, declare-ok) for an existing queue, found by *passive*
    declare -- which never creates anything and never argues about
    durable/quorum arguments the way a default declare does. A missing
    queue raises QueueNotFound (-> 404) unless create=True, in which case it
    is declared with default arguments (POST only)."""
    import pika.exceptions
    ch = conn.channel()
    try:
        return ch, ch.queue_declare(queue=q, passive=True)
    except pika.exceptions.ChannelClosedByBroker as e:
        if e.reply_code != 404:
            raise
        if not create:
            raise QueueNotFound(q) from None
        ch = conn.channel()   # the broker closed the first one
        return ch, ch.queue_declare(queue=q)


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
            if method == "GET":
                _, ok = _queue(conn, q)
                return jci.send_json(200, {"name": q, "messageCount": ok.method.message_count,
                                           "consumerCount": ok.method.consumer_count})
            if method == "POST":
                ch, _ = _queue(conn, q, create=True)
                ch.basic_publish(exchange="", routing_key=q, body=body)
                return jci.send_json(200, {"name": q, "published": True, "bytes": len(body)})
            if method == "DELETE":
                ch, _ = _queue(conn, q)
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
            ch, _ = _queue(conn, q)
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
            ch, _ = _queue(conn, q)
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


def main_route(method: str, path: str, headers: dict, body: bytes) -> None:
    try:
        route(method, path, headers, body)
    except QueueNotFound as e:
        jci.send_error(404, f"no such queue: {e}",
                       hint="POST /stream/{q} declares it with default arguments")


if __name__ == "__main__":
    jci.main(main_route)
