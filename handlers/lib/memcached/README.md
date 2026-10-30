# memcached

Get, set and delete memcached keys with curl — the same `kv` shape as the
redis handler's keys. Stdlib only (the text protocol is a few lines);
nothing to install.

```sh
curl -X PUT http://mylocal.memcached/kv/session:42 --data-binary '{"user":"ada"}'
curl http://mylocal.memcached/kv/session:42
curl 'http://mylocal.memcached/kv/flash?ex=30' -X PUT -d 'saved'
```

## Routes

```
GET    /keys?limit=1000       key names, via `lru_crawler metadump all` (memcached >= 1.5)
GET    /kv/{k}                value as text/plain (octet-stream if not UTF-8); 404 if missing
PUT    /kv/{k}?ex=<seconds>   set; body = value; ex absent or 0 = no expiry
POST   /kv/{k}                same as PUT
DELETE /kv/{k}                -> {"deleted": 0|1}
GET    /stats                 the `stats` command as a dict
GET    /_info
```

## backends.conf

```ini
[mylocal.memcached]
host    = cache.example
port    = 11211
timeout = 5
```

## Notes

- `GET` routes never mutate (ADR 0005). `/keys` walks the LRU crawler's
  metadump, which is cheap but not a snapshot: keys can appear twice or
  be missed while the cache churns, and it's capped by `limit`.
- Keys follow memcached's rules: up to 250 bytes, no whitespace or control
  characters; anything else is a `400`. Percent-encode what you must.
- Flags are not exposed (always 0); values are opaque bytes. No SASL, no
  TLS — memcached is a LAN service; run it behind the handler's host.
