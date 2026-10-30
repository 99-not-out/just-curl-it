# postgres

SQL over HTTP, plus LISTEN/NOTIFY as a stream. `curl -G
http://mylocal.postgres/query --data-urlencode 'sql=select now()'`. Needs
`jci handler install postgres`.

## Routes

```
GET  /tables                  {"columns":["schemaname","tablename"],"rows":[...]}
GET  /query?sql=SELECT...     {"columns","rowCount","rows"}  -- READ ONLY transaction
POST /query                   body = SQL text, or {"sql": "...", "params": [...]}
                              statements -> {"rowCount": n}; queries -> rows
POST /stream/{channel}        pg_notify(channel, body)
GET  /stream/{channel}/tail   LISTEN channel; Server-Sent Events until you disconnect
GET  /_info
```

```sh
curl -X POST http://mylocal.postgres/query -H 'content-type: application/json' \
     -d '{"sql":"insert into users(name) values (%s)","params":["ada"]}'
curl -N http://mylocal.postgres/stream/events/tail &
curl -X POST http://mylocal.postgres/stream/events -d 'deployed v12'
```

## backends.conf

```ini
[mylocal.postgres]
dsn     = postgresql://app:${PGPASSWORD}@db.example:5432/app?sslmode=require
timeout = 5          # added to the DSN as connect_timeout unless it sets one
```

Any libpq DSN works, URI or keyword form; TLS, client certs and auth are all
libpq parameters (`sslmode`, `sslrootcert`, `sslcert`, `sslkey`).

## Notes

- `GET /query` runs with `default_transaction_read_only = on`, so Postgres
  itself refuses a write with a `405` pointing at POST (ADR 0005). A
  `GET`-only route plus this handler is a read-only database the server
  guarantees, not a regex.
- `params` use psycopg placeholders (`%s`); the handler never interpolates
  SQL. Channel names for LISTEN are validated as identifiers because LISTEN
  cannot take a parameter.
- One connection per request (ADR 0002). Point `dsn` at a pgbouncer if the
  connect cost matters.
