# sqlite

Query a SQLite file over HTTP. Stdlib only, nothing to install — point a
route at any `.db` on disk and `curl` it. Same `query` shape as the
Postgres handler, so a script written against one works against the other.

## Routes

```
GET  /tables                  {"columns":["name","type"],"rows":[["t","table"],...]}
GET  /query?sql=SELECT...     {"columns","rowCount","rows"}        -- read-only
POST /query                   body = SQL text, or {"sql": "...", "params": [...]}
                              {"rowCount": n} for statements, rows for queries
GET  /_info
```

```sh
curl -G http://mylocal.db/query --data-urlencode 'sql=select * from users limit 5' | jq
curl -X POST http://mylocal.db/query -d 'insert into users(name) values ("ada")'
curl -X POST http://mylocal.db/query -H 'content-type: application/json' \
     -d '{"sql":"insert into users(name) values (?)","params":["grace"]}'
```

## backends.conf

```ini
[mylocal.db]
path     = ~/data/app.db     # required; ~ expanded
readonly = false             # true: POST /query answers 405 too
timeout  = 5                 # sqlite3 busy timeout, seconds
```

## Notes

- `GET` is read-only because SQLite is told so: the file is opened with
  `mode=ro`, and a write attempt is a `405` with `Allow: POST`. Not a regex
  over the SQL (ADR 0005).
- A path that doesn't exist is a `502` naming it. The handler never
  creates a database (`mode=rw`, never `rwc`).
- Put a `GET`-only method list on the route and `readonly = true` in the
  config and you have a database an agent can read and provably not
  change — twice over.
- Row values come back as JSON; `BLOB` columns are stringified.
