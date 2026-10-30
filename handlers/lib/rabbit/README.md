# rabbit

RabbitMQ queues as the `stream` shape — the same routes as Kafka, so a
script that peeks a topic can peek a queue. Needs `jci handler install rabbit`.

```sh
curl -X POST http://mylocal.rabbit/stream/jobs -d '{"id":42}'
curl 'http://mylocal.rabbit/stream/jobs/peek?limit=5' | jq '.messages[].body'
curl -N http://mylocal.rabbit/stream/jobs/tail
```

## Routes

```
GET    /streams                      queues with message/consumer counts (management API)
GET    /stream/{q}                   declare (idempotent) -> {"messageCount", "consumerCount"}
POST   /stream/{q}                   publish to the default exchange, routing key q
GET    /stream/{q}/peek?limit=1      up to N messages; non-destructive (see Notes)
GET    /stream/{q}/tail              Server-Sent Events; non-destructive (see Notes)
DELETE /stream/{q}                   purge -> {"purged": n}
POST   /exchange/{x}/publish?routing_key=r    publish to a named exchange
GET    /_info
```

## backends.conf

```ini
[mylocal.rabbit]
url      = amqps://app:${RABBIT_PASSWORD}@mq.example:5671/%2Fprod   # pika URL; amqps for TLS
mgmt_url = https://app:${RABBIT_PASSWORD}@mq.example:15671          # for GET /streams only
timeout  = 5
```

## Notes

- **Peek is non-destructive** since 0006: messages are fetched with
  `auto_ack=False` and nacked back with `requeue=True`, so they stay on the
  queue — flagged `redelivered: true` the next time anyone sees them.
- **Tail holds messages unacked while open.** Other consumers won't see
  them until you disconnect, at which point the broker requeues them.
  That is what "watch a queue without consuming it" costs in AMQP; the
  alternative was a tail that silently ate messages.
- `GET /streams` needs the management plugin and `mgmt_url`; everything
  else is plain AMQP.
- Queues are looked up with a *passive* declare, so durable and quorum
  queues work (a default declare would fail with `PRECONDITION_FAILED`),
  and `GET`/`DELETE` on a queue that doesn't exist is a `404` — nothing
  is created. Only `POST /stream/{q}` declares a missing queue, with
  default (non-durable) arguments.
