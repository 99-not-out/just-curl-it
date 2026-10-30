# mqtt

Publish and subscribe to MQTT topics with curl. The `stream` shape, shared
with Kafka, RabbitMQ and Redis pub/sub. Needs `jci handler install mqtt`.

```sh
curl -X POST http://mylocal.mqtt/stream/home/kitchen/light -d 'on'
curl -N  http://mylocal.mqtt/stream/home/+/light/tail                 # SSE, '+' is one level
curl -N 'http://mylocal.mqtt/stream/home/%23/tail'                    # '#' is the rest, sent as %23
curl     http://mylocal.mqtt/stream/home/kitchen/temperature           # the retained reading, if any
```

## Routes

```
POST   /stream/{topic}?qos=1&retain=0   publish; body = payload -> {topic, published, bytes, qos, retain}
GET    /stream/{topic}/tail             subscribe; Server-Sent Events until you disconnect
GET    /stream/{topic}                  retained message -> {topic, payload, qos, retain}; 404 if none
                                        (a wildcard returns {count, messages})
DELETE /stream/{topic}                  clear the retained message (publishes an empty retained)
GET    /streams                         501: MQTT has no topic listing
GET    /stream/{topic}/peek             501: MQTT has no replay
GET    /_info
```

Topics contain `/`, so the topic is everything between `/stream/` and an
optional trailing `/tail` or `/peek`. A topic that itself ends in `/tail`
can't be addressed — rename it.

## backends.conf

```ini
[mylocal.mqtt]
url       = mqtts://user:${MQTT_PASSWORD}@broker.example:8883     # or mqtt://host:1883
# or the pieces:
host      = broker.example
port      = 8883
tls       = true
username  = user
password  = ${MQTT_PASSWORD}
ca_cert   = /path/ca.pem          # private CA
client_id = jci                   # default jci-<pid>
timeout   = 5                     # connect / publish-ack timeout
```

## Notes

- `GET` is read-only (ADR 0005): subscribing and reading retained messages
  change nothing. `POST` publishes, `DELETE` clears a retained message —
  both blocked by a `GET`-only route.
- `/tail` subscribes at QoS 1 and holds the connection open; `-N` stops
  curl buffering. Events are `data: {"topic","payload","qos","retain"}`;
  a `: idle` comment every 5 s keeps proxies awake.
- `GET /stream/{topic}` waits up to 2 s for the broker to deliver retained
  messages after subscribing; a wildcard collects all it sees in that window.
- Publish waits for the broker's ack at QoS 1/2 (502 if it never comes);
  QoS 0 returns immediately.
- MQTT 3.1.1. Shared subscriptions and MQTT 5 properties aren't exposed.
