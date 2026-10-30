# mysql

SQL over HTTP for MySQL and MariaDB — the same `query` shape as the
`postgres` and `sqlite` handlers, so one script works against all three.
Needs `jci handler install mysql` (pure-Python driver, no client libs).

```sh
curl -G http://mylocal.mysql/query --data-urlencode 'sql=select id, email from users limit 5' | jq
curl -X POST http://mylocal.mysql/query -H 'content-type: application/json' \
     -d '{"sql":"insert into users(email) values (%s)","params":["ada@example.com"]}'
```

## Routes

```
GET  /tables                  {"columns":["table_schema","table_name","table_type"],"rows":[...]}
GET  /query?sql=SELECT...     {"columns","rowCount","rows"}  -- READ ONLY transaction, one statement
POST /query                   body = SQL text, or {"sql": "...", "params": [...]}
                              statements -> {"rowCount": n}; queries -> rows
GET  /_info
```

## backends.conf

```ini
[mylocal.mysql]
host        = db.example
port        = 3306
user        = app
password    = ${MYSQL_PASSWORD}
database    = app                    # optional; /tables lists this schema
timeout     = 5                      # connect timeout, seconds
tls         = false                  # true -> TLS; ca_cert verifies the server
ca_cert     = /path/ca.pem
# unix_socket = /var/run/mysqld/mysqld.sock
```

## Notes

- `GET /query` is gated twice, both by the server (ADR 0005). It runs
  inside `START TRANSACTION READ ONLY`, which refuses DML — but MySQL DDL
  *implicitly commits* that transaction and then runs, so the statement is
  first passed through `EXPLAIN`, which the parser accepts only for
  `SELECT`/`TABLE`/DML and which executes nothing; anything else is a `405`.
  Multi-statements are disabled on the connection, so `select 1; drop
  table x` is a syntax error and the second statement never runs.
- Consequence: `EXPLAIN ...` itself and `SHOW ...` are `405` on `GET`; use
  `POST` for them. `SELECT ... INTO OUTFILE` needs the `FILE` privilege —
  don't give the handler's user that.
- `params` use `%s` placeholders (PyMySQL style) and are sent escaped, not
  interpolated by you.
- One connection per request (ADR 0002). Rows come back as JSON; `DECIMAL`
  and `DATETIME` values are stringified.
