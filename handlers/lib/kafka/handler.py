#!/usr/bin/env python3
"""HTTP -> Kafka. The `stream` shape:

  GET    /streams                  list topics
  GET    /stream/<T>               partitions, leaders, replicas, ISR
  POST   /stream/<T>               produce one record; body = value, ?key=K
  GET    /stream/<T>/peek?limit=N  last N records across partitions (non-destructive)
  GET    /stream/<T>/tail?from=    SSE stream; from=latest|earliest
  DELETE /stream/<T>               501 -- Kafka has no purge primitive

Peek and tail assign partitions directly; no consumer group is joined and
no offsets are ever committed, so neither disturbs real consumers.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /streams",
    "GET    /stream/{T}",
    "POST   /stream/{T}                 (body = record value; ?key=K)",
    "GET    /stream/{T}/peek?limit=50   (non-destructive)",
    "GET    /stream/{T}/tail?from=latest|earliest    (SSE stream)",
]

_FRIENDLY = {
    "security_protocol": "security.protocol",
    "sasl_mechanism": "sasl.mechanism",
    "sasl_username": "sasl.username",
    "sasl_password": "sasl.password",
    "ssl_ca_location": "ssl.ca.location",
}


def broker_conf(cfg: dict) -> dict:
    """librdkafka config: bootstrap + the friendly keys + any rdkafka_* key
    passed through with '_' -> '.' (rdkafka_ssl_certificate_location ->
    ssl.certificate.location), so every librdkafka option is reachable."""
    if not cfg.get("bootstrap"):
        raise jci.BackendConfigError("missing 'bootstrap' in backends.conf")
    conf = {"bootstrap.servers": cfg["bootstrap"],
            "socket.timeout.ms": int(jci.timeout(cfg) * 1000),
            "log_level": 3}
    for k, v in cfg.items():
        if k in _FRIENDLY:
            conf[_FRIENDLY[k]] = v
        elif k.startswith("rdkafka_"):
            conf[k[len("rdkafka_"):].replace("_", ".")] = v
    return conf


def _topic_meta(conf: dict, topic: str):
    from confluent_kafka.admin import AdminClient
    md = AdminClient(conf).list_topics(topic=topic, timeout=jci.timeout({}) * 2)
    t = md.topics.get(topic)
    if not t or t.error is not None:
        return None, str(t.error) if t and t.error else "not found"
    return t, None


def _record(msg) -> dict:
    k, v = msg.key(), msg.value()
    return {
        "partition": msg.partition(), "offset": msg.offset(),
        "key": k.decode("utf-8", "replace") if k is not None else None,
        "value": v.decode("utf-8", "replace") if v is not None else None,
        "timestamp": msg.timestamp()[1] if msg.timestamp()[0] != 0 else None,
    }


def handle_list_topics(conf: dict) -> None:
    from confluent_kafka.admin import AdminClient
    md = AdminClient(conf).list_topics(timeout=10)
    topics = sorted(({"name": n, "partitions": len(t.partitions), "internal": n.startswith("_")}
                     for n, t in md.topics.items()), key=lambda x: x["name"])
    jci.send_json(200, {"topics": topics, "streams": [t["name"] for t in topics]})


def handle_topic_details(conf: dict, topic: str) -> None:
    t, err = _topic_meta(conf, topic)
    if err:
        return jci.send_error(404, f"topic {topic}: {err}")
    parts = [{"id": pid, "leader": p.leader, "replicas": list(p.replicas), "isr": list(p.isrs)}
             for pid, p in sorted(t.partitions.items())]
    jci.send_json(200, {"name": topic, "partitionCount": len(parts),
                        "replicationFactor": len(parts[0]["replicas"]) if parts else 0,
                        "partitions": parts})


def handle_peek(conf: dict, topic: str, limit: int) -> None:
    from confluent_kafka import Consumer, KafkaError, TopicPartition
    t, err = _topic_meta(conf, topic)
    if err:
        return jci.send_error(404, f"topic {topic}: {err}")
    c = Consumer({**conf, "group.id": "jci-peek", "enable.auto.commit": False,
                  "auto.offset.reset": "earliest"})
    try:
        per_part = max(1, limit // max(1, len(t.partitions)))
        tps = []
        for pid in t.partitions:
            low, high = c.get_watermark_offsets(TopicPartition(topic, pid), timeout=10, cached=False)
            tps.append(TopicPartition(topic, pid, max(low, high - per_part)))
        c.assign(tps)
        records, deadline, idle = [], time.monotonic() + 10, 0
        while len(records) < limit and time.monotonic() < deadline:
            msg = c.poll(0.2)
            if msg is None:
                idle += 1
                if idle > 10:
                    break
                continue
            idle = 0
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                break
            records.append(_record(msg))
        records.sort(key=lambda m: (m["partition"], m["offset"]))
        jci.send_json(200, {"topic": topic, "count": len(records), "records": records[-limit:]})
    finally:
        c.close()


def handle_tail(conf: dict, topic: str, start: str) -> None:
    from confluent_kafka import Consumer, KafkaError, TopicPartition
    t, err = _topic_meta(conf, topic)
    if err:
        return jci.send_error(404, f"topic {topic}: {err}")
    c = Consumer({**conf, "group.id": f"jci-tail-{os.getpid()}", "enable.auto.commit": False,
                  "auto.offset.reset": start})
    tps = []
    for pid in t.partitions:
        low, high = c.get_watermark_offsets(TopicPartition(topic, pid), timeout=10, cached=False)
        tps.append(TopicPartition(topic, pid, low if start == "earliest" else high))
    c.assign(tps)
    jci.send_sse_headers()
    jci.sse_comment(f"consuming {topic} from {start}")
    try:
        while True:
            msg = c.poll(1.0)
            if msg is None:
                jci.sse_comment("idle")
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    jci.sse_event({"error": str(msg.error())}, event="error")
                continue
            jci.sse_event(_record(msg))
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass


def handle_produce(conf: dict, topic: str, body: bytes, key: bytes | None) -> None:
    from confluent_kafka import Producer
    if not body:
        return jci.send_error(400, "empty body")
    result: dict = {}

    def on_delivery(err, msg):
        if err:
            result["error"] = str(err)
        else:
            result.update(partition=msg.partition(), offset=msg.offset())

    prod = Producer({**conf, "acks": "all"})
    prod.produce(topic, value=body, key=key, on_delivery=on_delivery)
    remaining = prod.flush(30)
    if remaining or "error" in result:
        return jci.send_error(502, result.get("error", "delivery timed out"), topic=topic)
    jci.send_json(200, {"topic": topic, "produced": 1, **result})


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("stream", ROUTES)

    conf = broker_conf(jci.load_backend())

    if method == "GET" and p == "/streams":
        return handle_list_topics(conf)

    m = re.fullmatch(r"/stream/([^/]+)", p)
    if m:
        topic = m.group(1)
        if method == "GET":
            return handle_topic_details(conf, topic)
        if method == "POST":
            key = qs.get("key", [None])[0]
            return handle_produce(conf, topic, body, key.encode("utf-8") if key else None)
        if method == "DELETE":
            return jci.send_error(501, "kafka has no purge primitive; DELETE /stream/{T} is not supported")

    m = re.fullmatch(r"/stream/([^/]+)/peek", p)
    if m and method == "GET":
        return handle_peek(conf, m.group(1), int(qs.get("limit", ["50"])[0]))

    m = re.fullmatch(r"/stream/([^/]+)/tail", p)
    if m and method == "GET":
        start = qs.get("from", ["latest"])[0]
        if start not in ("earliest", "latest"):
            return jci.send_error(400, "from must be earliest|latest")
        return handle_tail(conf, m.group(1), start)

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
