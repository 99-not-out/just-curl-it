#!/usr/bin/env python3
"""HTTP -> MQTT. The `stream` shape over topics:

  POST   /stream/<topic>?qos=1&retain=0    publish; body = payload
  GET    /stream/<topic>/tail              subscribe; Server-Sent Events ('+' and '#' wildcards work)
  GET    /stream/<topic>                   the retained message(s) on the topic, or 404
  DELETE /stream/<topic>                   clear the retained message
  GET    /streams                          501 -- MQTT has no topic listing
  GET    /stream/<topic>/peek              501 -- no replay (retained is GET /stream/<topic>)

Topics contain '/', so the topic is everything between /stream/ and an
optional trailing /tail or /peek. '#' must be sent percent-encoded (%23).
"""
from __future__ import annotations

import os
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "POST   /stream/{topic}?qos=1&retain=0   (publish; body = payload)",
    "GET    /stream/{topic}/tail             (subscribe; SSE; + and # wildcards, # as %23)",
    "GET    /stream/{topic}                  (retained message(s), or 404)",
    "DELETE /stream/{topic}                  (clear retained)",
]


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _conn_params(cfg: dict) -> dict:
    """host/port/tls/username/password from `url` (mqtt:// or mqtts://) or the pieces."""
    url = cfg.get("url", "").strip()
    if url:
        u = urlsplit(url)
        if u.scheme not in ("mqtt", "mqtts", "tcp", "ssl"):
            raise jci.BackendConfigError(f"url scheme must be mqtt:// or mqtts://, got {u.scheme!r}")
        tls = u.scheme in ("mqtts", "ssl") or _truthy(cfg.get("tls"))
        return {"host": u.hostname or "localhost", "port": u.port or (8883 if tls else 1883),
                "tls": tls, "username": u.username or cfg.get("username"),
                "password": u.password or cfg.get("password")}
    tls = _truthy(cfg.get("tls"))
    return {"host": cfg.get("host", "localhost"), "port": int(cfg.get("port", 8883 if tls else 1883)),
            "tls": tls, "username": cfg.get("username"), "password": cfg.get("password")}


def client(cfg: dict):
    import paho.mqtt.client as mqtt
    p = _conn_params(cfg)
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=cfg.get("client_id") or f"jci-{os.getpid()}",
                    protocol=mqtt.MQTTv311)
    if p["username"]:
        c.username_pw_set(p["username"], p["password"] or None)
    if p["tls"]:
        c.tls_set(ca_certs=cfg.get("ca_cert") or None)
    c.connect(p["host"], p["port"], keepalive=30)
    # Wait for CONNACK so an auth/TLS failure is a clean 502, not a hang.
    deadline = time.monotonic() + jci.timeout(cfg)
    while not c.is_connected() and time.monotonic() < deadline:
        c.loop(timeout=0.2)
    if not c.is_connected():
        raise ConnectionError(f"no CONNACK from {p['host']}:{p['port']} within {jci.timeout(cfg)}s")
    return c


def _event(msg) -> dict:
    payload = msg.payload
    try:
        payload = payload.decode("utf-8")
    except UnicodeDecodeError:
        payload = payload.decode("utf-8", "replace")
    return {"topic": msg.topic, "payload": payload, "qos": msg.qos, "retain": bool(msg.retain)}


def handle_publish(cfg: dict, topic: str, body: bytes, qos: int, retain: bool) -> None:
    c = client(cfg)
    try:
        info = c.publish(topic, body, qos=qos, retain=retain)
        deadline = time.monotonic() + jci.timeout(cfg)
        while not info.is_published() and time.monotonic() < deadline:
            c.loop(timeout=0.2)
        if qos > 0 and not info.is_published():
            return jci.send_error(502, f"broker did not acknowledge the publish within {jci.timeout(cfg)}s")
        jci.send_json(200, {"topic": topic, "published": True, "bytes": len(body),
                            "qos": qos, "retain": retain})
    finally:
        c.disconnect()


def handle_retained(cfg: dict, topic: str) -> None:
    """Subscribe and collect what the broker sends immediately: retained messages."""
    c = client(cfg)
    found: list[dict] = []

    def on_message(_c, _u, msg):
        if msg.retain:
            found.append(_event(msg))

    c.on_message = on_message
    try:
        c.subscribe(topic, qos=0)
        deadline = time.monotonic() + min(2.0, jci.timeout(cfg))
        while time.monotonic() < deadline:
            c.loop(timeout=0.2)
            if found and "+" not in topic and "#" not in topic:
                break
    finally:
        c.disconnect()
    if not found:
        return jci.send_error(404, f"no retained message on {topic}",
                              hint="POST /stream/{topic}?retain=1 sets one")
    if "+" in topic or "#" in topic:
        return jci.send_json(200, {"topic": topic, "count": len(found), "messages": found})
    jci.send_json(200, found[0])


def handle_tail(cfg: dict, topic: str) -> None:
    c = client(cfg)
    lock = threading.Lock()
    pending: list[dict] = []

    def on_message(_c, _u, msg):
        with lock:
            pending.append(_event(msg))

    c.on_message = on_message
    c.subscribe(topic, qos=1)
    jci.send_sse_headers()
    jci.sse_comment(f"subscribed to {topic}")
    idle = 0.0
    try:
        while True:
            c.loop(timeout=0.5)
            with lock:
                batch, pending[:] = pending[:], []
            if batch:
                idle = 0.0
                for ev in batch:
                    jci.sse_event(ev)
            else:
                idle += 0.5
                if idle >= 5.0:
                    jci.sse_comment("idle")
                    idle = 0.0
            if not c.is_connected():
                jci.sse_event({"error": "disconnected from broker"}, event="error")
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            c.disconnect()
        except Exception:
            pass


def handle_clear(cfg: dict, topic: str) -> None:
    if "+" in topic or "#" in topic:
        return jci.send_error(400, "DELETE needs a concrete topic, not a wildcard")
    c = client(cfg)
    try:
        info = c.publish(topic, b"", qos=1, retain=True)   # empty retained = clear
        deadline = time.monotonic() + jci.timeout(cfg)
        while not info.is_published() and time.monotonic() < deadline:
            c.loop(timeout=0.2)
        jci.send_json(200, {"topic": topic, "cleared": True})
    finally:
        c.disconnect()


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("stream", ROUTES)

    cfg = jci.load_backend()

    if p == "/streams":
        return jci.send_error(501, "MQTT has no topic listing; subscribe to a wildcard with GET /stream/{filter}/tail")

    m = re.fullmatch(r"/stream/(.+?)(/tail|/peek)?", p)
    if not m:
        return jci.send_error(404, f"no route for {method} {p}")
    topic, suffix = unquote(m.group(1)), m.group(2)
    if not topic or "\x00" in topic:
        return jci.send_error(400, "bad topic")

    try:
        if suffix == "/peek":
            return jci.send_error(501, "MQTT has no replay; GET /stream/{topic} returns the retained message")
        if suffix == "/tail" and method == "GET":
            return handle_tail(cfg, topic)
        if suffix is None:
            if method == "GET":
                return handle_retained(cfg, topic)
            if method == "POST":
                qos = int(qs.get("qos", ["1"])[0])
                if qos not in (0, 1, 2):
                    return jci.send_error(400, "qos must be 0, 1 or 2")
                return handle_publish(cfg, topic, body, qos, _truthy(qs.get("retain", ["0"])[0]))
            if method == "DELETE":
                return handle_clear(cfg, topic)
    except (ConnectionError, OSError, TimeoutError) as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
