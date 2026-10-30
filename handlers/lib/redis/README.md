# redis

Keys as a `kv` shape, pub/sub channels as a `stream` shape. `curl -X PUT
http://mylocal.redis/kv/greeting -d hello` and `curl http://mylocal.redis/kv/greeting`.
Needs `jci handler install redis`.

## Routes

```
GET    /keys?prefix=X          KEYS X*      (or ?pattern=<glob> verbatim)
GET    /kv/{k}                 value as text/plain (octet-stream if not UTF-8); 404 if missing
PUT    /kv/{k}                 SET; body = value; ?ex=<seconds> for a TTL
POST   /kv/{k}                 same as PUT
DELETE /kv/{k}                 DEL -> {"deleted": 0|1}

GET    /streams                PUBSUB CHANNELS *
POST   /stream/{ch}            PUBLISH; body = message -> {"receivers": n}
GET    /stream/{ch}/tail       SUBSCRIBE; Server-Sent Events until you disconnect
GET    /stream/{ch}/peek       501: pub/sub has no replay
GET    /_info
```

## backends.conf

```ini
[mylocal.redis]
# either a URL...
url      = rediss://default:${REDIS_PASSWORD}@cache.example:6380/0
# ...or the pieces:
host     = localhost
port     = 6379
username = default           # optional (ACL user)
password = ${REDIS_PASSWORD} # optional
tls      = false             # true -> TLS; pair with ca_cert for a private CA
ca_cert  = /path/ca.pem
timeout  = 5                 # connect timeout, seconds
```

## Notes

- `GET` routes never mutate (ADR 0005). `GET /keys` is `KEYS`, which is
  O(N) on the keyspace — fine for a dev box, rude on a big production
  instance; use a narrow prefix.
- `/tail` holds the connection open and streams `data: {"name","data"}`
  events; `curl -N` to see them as they arrive.
- Binary values come back as `application/octet-stream`; everything else
  as `text/plain`.
