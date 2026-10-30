# clickhouse

SQL over HTTP for ClickHouse — the `query` shape shared with postgres,
mysql and sqlite, on top of ClickHouse's own HTTP interface. Stdlib only,
nothing to install. Results are JSON with column names *and* ClickHouse
types.

```sh
curl -G http://mylocal.clickhouse/query --data-urlencode 'sql=select count() from events where ts > now() - 3600' | jq .rows
curl -X POST http://mylocal.clickhouse/query --data-binary $'insert into events format JSONEachRow\n{"ts":"2026-10-30 12:00:00","k":"a"}'
```

## Routes

```
GET  /tables                  {"columns":["database","name","engine"],"rows":[...]}
GET  /query?sql=SELECT...     {"columns","types","rowCount","rows","statistics"} -- server readonly=1
POST /query                   body = SQL; statements -> {"rowCount": written_rows, "summary": {...}}
GET  /_info
```

## backends.conf

```ini
[mylocal.clickhouse]
upstream = https://clickhouse.example:8443      # the HTTP(S) interface; http://host:8123 locally
user     = app
password = ${CLICKHOUSE_PASSWORD}
database = app                                  # optional default database
ca_cert  = /path/ca.pem                         # optional, for a private CA
timeout  = 5
```

## Notes

- `GET` sends the server setting `readonly=1`, so ClickHouse itself refuses
  DDL, DML and settings changes with a `405` pointing at POST (ADR 0005).
  Multi-statements are refused by the server by default. Nothing here
  parses your SQL.
- Numbers wider than JSON's double (`UInt64`, `Int64`, `Decimal`) come back
  as strings — that's ClickHouse's `JSONCompact` output; `types` tells you
  which columns.
- A statement that names its own `FORMAT` (e.g. `... FORMAT CSV`) is
  returned verbatim with ClickHouse's content type instead of the JSON
  envelope.
- Each request is one HTTP call to ClickHouse — no session, no connection
  pool (ADR 0002).
- `readonly=1` also forbids `SETTINGS ...` in a GET query (that's what
  makes it airtight), so limits like `max_execution_time` belong in the
  handler user's profile on the server, not in the SQL.
