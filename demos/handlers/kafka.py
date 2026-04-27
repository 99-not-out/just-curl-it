#!/usr/bin/env python3
"""HTTP -> Kafka adapter spawned per intercepted request.

Routes:
  GET  /topics                list topics + partition counts
  GET  /topic/<T>             partitions + leaders + replicas for T
  GET  /topic/<T>/records     last N records across partitions (?limit=50)
  POST /topic/<T>/records     body: one "key|value" per line (key optional)
"""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
from urllib.parse import parse_qs, urlsplit

from shim import (read_request, load_backend,
                  send_sse_headers, sse_event, sse_comment)
from shim import send_json as _raw_send_json

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "kafka",
    "routes": [
        "GET  /_info",
        "GET  /topics",
        "GET  /topic/{T}",
        "GET  /topic/{T}/records?limit=50",
        "POST /topic/{T}/records  (body: key|value per line)",
        "GET  /topic/{T}/consume?from=latest|earliest  (SSE stream)",
    ],
}


def send_json(status, obj):
    _raw_send_json(status, obj, handler="kafka")


def broker_conf() -> dict:
    cfg = load_backend(HOST)
    conf = {"bootstrap.servers": cfg["bootstrap"]}
    if "security_protocol" in cfg:
        conf["security.protocol"] = cfg["security_protocol"]
    if "sasl_mechanism" in cfg:
        conf["sasl.mechanism"] = cfg["sasl_mechanism"]
        conf["sasl.username"] = cfg["sasl_username"]
        conf["sasl.password"] = cfg["sasl_password"]
    if "ssl_ca_location" in cfg:
        conf["ssl.ca.location"] = cfg["ssl_ca_location"]
    return conf


def handle_list_topics(conf: dict) -> None:
    from confluent_kafka.admin import AdminClient
    md = AdminClient(conf).list_topics(timeout=10)
    topics = sorted(
        (
            {
                "name": name,
                "partitions": len(t.partitions),
                "internal": name.startswith("_"),
            }
            for name, t in md.topics.items()
        ),
        key=lambda x: x["name"],
    )
    send_json(200, {"topics": topics})


def handle_topic_details(conf: dict, topic: str) -> None:
    from confluent_kafka.admin import AdminClient
    md = AdminClient(conf).list_topics(topic=topic, timeout=10)
    t = md.topics.get(topic)
    if not t or t.error is not None:
        err = str(t.error) if t and t.error else "not found"
        return send_json(404, {"error": f"topic {topic}: {err}"})
    partitions = [
        {
            "id": pid,
            "leader": p.leader,
            "replicas": list(p.replicas),
            "isr": list(p.isrs),
        }
        for pid, p in sorted(t.partitions.items())
    ]
    send_json(200, {
        "name": topic,
        "partitionCount": len(partitions),
        "replicationFactor": len(partitions[0]["replicas"]) if partitions else 0,
        "partitions": partitions,
    })


def handle_peek_records(conf: dict, topic: str, limit: int) -> None:
    """Assign-only peek. No subscribe, no commit, no group coordinator chatter.

    librdkafka requires group.id even for assign; we set a static dummy that
    is never used for offset storage (enable.auto.commit=false, no commit()).
    """
    from confluent_kafka import Consumer, KafkaError, TopicPartition
    from confluent_kafka.admin import AdminClient

    md = AdminClient(conf).list_topics(topic=topic, timeout=10)
    t = md.topics.get(topic)
    if not t or t.error is not None:
        err = str(t.error) if t and t.error else "not found"
        return send_json(404, {"error": f"topic {topic}: {err}"})

    c = Consumer({
        **conf,
        "group.id": "intercept-peek",   # dummy; never used
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    try:
        per_part = max(1, limit // max(1, len(t.partitions)))
        tps: list[TopicPartition] = []
        for pid in t.partitions:
            low, high = c.get_watermark_offsets(
                TopicPartition(topic, pid), timeout=10, cached=False)
            start = max(low, high - per_part)
            tps.append(TopicPartition(topic, pid, start))
        c.assign(tps)

        records = []
        deadline = time.monotonic() + 10
        idle_polls = 0
        while len(records) < limit and time.monotonic() < deadline:
            msg = c.poll(0.2)
            if msg is None:
                idle_polls += 1
                if idle_polls > 10:
                    break
                continue
            idle_polls = 0
            err = msg.error()
            if err:
                if err.code() == KafkaError._PARTITION_EOF:
                    continue
                break
            key_bytes = msg.key()
            val_bytes = msg.value()
            records.append({
                "partition": msg.partition(),
                "offset": msg.offset(),
                "key": key_bytes.decode("utf-8", "replace") if key_bytes is not None else None,
                "value": val_bytes.decode("utf-8", "replace") if val_bytes is not None else None,
                "timestamp": msg.timestamp()[1] if msg.timestamp()[0] != 0 else None,
            })

        records.sort(key=lambda m: (m["partition"], m["offset"]))
        send_json(200, {"topic": topic, "count": len(records), "records": records[-limit:]})
    finally:
        c.close()


def handle_consume(conf: dict, topic: str, start: str) -> None:
    """Stream records as Server-Sent Events until the client disconnects."""
    from confluent_kafka import Consumer, KafkaError, TopicPartition
    from confluent_kafka.admin import AdminClient

    md = AdminClient(conf).list_topics(topic=topic, timeout=10)
    t = md.topics.get(topic)
    if not t or t.error is not None:
        err = str(t.error) if t and t.error else "not found"
        return send_json(404, {"error": f"topic {topic}: {err}"})

    c = Consumer({
        **conf,
        "group.id": f"intercept-consume-{os.getpid()}",
        "enable.auto.commit": False,
        "auto.offset.reset": start,
    })
    tps: list[TopicPartition] = []
    for pid in t.partitions:
        low, high = c.get_watermark_offsets(
            TopicPartition(topic, pid), timeout=10, cached=False)
        offset = low if start == "earliest" else high
        tps.append(TopicPartition(topic, pid, offset))
    c.assign(tps)

    send_sse_headers(handler="kafka")
    sse_comment(f"consuming {topic} from {start}")
    try:
        while True:
            msg = c.poll(1.0)
            if msg is None:
                sse_comment("idle")
                continue
            err = msg.error()
            if err:
                if err.code() == KafkaError._PARTITION_EOF:
                    continue
                sse_event({"error": str(err)}, event="error")
                continue
            k, v = msg.key(), msg.value()
            sse_event({
                "partition": msg.partition(),
                "offset": msg.offset(),
                "key": k.decode("utf-8", "replace") if k is not None else None,
                "value": v.decode("utf-8", "replace") if v is not None else None,
                "timestamp": msg.timestamp()[1] if msg.timestamp()[0] != 0 else None,
            })
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass


def handle_produce(conf: dict, topic: str, body: bytes) -> None:
    from confluent_kafka import Producer

    lines = [ln for ln in body.decode("utf-8", "replace").splitlines() if ln]
    if not lines:
        return send_json(400, {"error": "empty body; expected key|value per line"})

    results: list[dict] = []

    def on_delivery(err, msg, _sink=results):
        if err:
            _sink.append({"error": str(err)})
        else:
            _sink.append({"partition": msg.partition(), "offset": msg.offset()})

    prod = Producer({**conf, "acks": "all"})
    for line in lines:
        if "|" in line:
            k, _, v = line.partition("|")
            k_bytes = k.encode("utf-8") if k else None
        else:
            k_bytes, v = None, line
        kwargs = {"topic": topic, "value": v.encode("utf-8"), "on_delivery": on_delivery}
        if k_bytes is not None:
            kwargs["key"] = k_bytes
        prod.produce(**kwargs)
    prod.flush(30)

    send_json(200, {"topic": topic, "produced": len(results), "results": results})


def route(method: str, path: str, body: bytes, conf: dict) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        try:
            backend = load_backend(HOST)
        except Exception as e:
            backend = {"error": str(e)}
        return send_json(200, {**_INFO, "host": HOST, "backend": backend})

    if p == "/topics" and method == "GET":
        return handle_list_topics(conf)

    m = re.fullmatch(r"/topic/([^/]+)", p)
    if m and method == "GET":
        return handle_topic_details(conf, m.group(1))

    m = re.fullmatch(r"/topic/([^/]+)/records", p)
    if m:
        topic = m.group(1)
        if method == "GET":
            limit = int(qs.get("limit", ["50"])[0])
            return handle_peek_records(conf, topic, limit)
        if method == "POST":
            return handle_produce(conf, topic, body)

    m = re.fullmatch(r"/topic/([^/]+)/consume", p)
    if m and method == "GET":
        start = qs.get("from", ["latest"])[0]
        if start not in ("earliest", "latest"):
            return send_json(400, {"error": "from must be earliest|latest"})
        return handle_consume(conf, m.group(1), start)

    send_json(404, {"error": f"no route for {method} {p}"})


def main() -> None:
    try:
        method, path, _, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"})
    try:
        conf = broker_conf()
    except Exception as e:
        return send_json(500, {"error": f"config: {e}"})
    try:
        route(method, path, body, conf)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
