# kafka

Produce, peek and tail Kafka topics with curl. The `stream` shape, shared
with RabbitMQ and Redis pub/sub so scripts don't care which broker is
behind the hostname. Needs `jci handler install kafka` (librdkafka wheels,
no JVM).

```sh
curl -X POST 'http://mylocal.kafka/stream/orders?key=o-1' -d '{"sku":"x","qty":2}'
curl 'http://mylocal.kafka/stream/orders/peek?limit=5' | jq '.records[].value'
curl -N 'http://mylocal.kafka/stream/orders/tail?from=latest'
```

## Routes

```
GET    /streams                      {"topics":[{name,partitions,internal}], "streams":[names]}
GET    /stream/{T}                   partitions, leaders, replicas, ISR
POST   /stream/{T}?key=K             produce one record; body = value -> {partition, offset}
GET    /stream/{T}/peek?limit=50     last N records across partitions, oldest first
GET    /stream/{T}/tail?from=latest  Server-Sent Events until you disconnect (or from=earliest)
DELETE /stream/{T}                   501: Kafka has no purge primitive
GET    /_info
```

## backends.conf

```ini
[mylocal.kafka]
bootstrap         = broker1:9092,broker2:9092      # required
timeout           = 5                              # socket.timeout.ms = timeout*1000

# Optional: an AsyncAPI document (path or URL; JSON, or YAML with PyYAML in
# this handler's venv) turns GET /_info into a channel menu with example curls.
# asyncapi = ~/specs/events.json

# Friendly names for the common auth settings:
security_protocol = SASL_SSL
sasl_mechanism    = SCRAM-SHA-512
sasl_username     = ${KAFKA_USER}
sasl_password     = ${KAFKA_PASSWORD}
ssl_ca_location   = /path/ca.pem

# Any other librdkafka option: prefix rdkafka_, '_' becomes '.'
rdkafka_ssl_certificate_location = /path/client.pem
rdkafka_ssl_key_location         = /path/client.key
rdkafka_client_id                = jci
```

## Notes

- **Nothing here joins a consumer group or commits an offset.** Peek and
  tail `assign()` partitions directly, so they never trigger a rebalance
  or move a real consumer's position. `GET` is safe to point at production
  (ADR 0005).
- Peek reads the last `limit` records spread across partitions, so on a
  12-partition topic `limit=12` is one per partition.
- Produce waits for `acks=all`; a broker that never acks is a `502` after
  30 seconds, not a silent drop.
- One client per request means one metadata fetch per request (ADR 0002).
  Expect ~200 ms per call against a local broker.
