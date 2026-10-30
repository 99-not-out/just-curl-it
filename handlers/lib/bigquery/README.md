# bigquery

SQL over HTTP for Google BigQuery — the shared `query` shape, with the
job dance (submit, poll, page) collapsed into one response. Stdlib only;
the token comes from `gcloud`, so there's nothing to install.

```sh
curl -sG http://mylocal.bq/query --data-urlencode 'sql=select date, count(*) n from `proj.ds.events` group by 1 order by 1 desc limit 7' | jq .rows
curl -s http://mylocal.bq/datasets
curl -s 'http://mylocal.bq/tables?dataset=analytics'
```

## Routes

```
GET  /datasets                  {"project", "datasets": [ids]}
GET  /tables?dataset=D          {"dataset", "tables": [{name, type}]}
GET  /query?sql=SELECT...       {"columns","types","rows","rowCount","totalRows","jobId",
                                 "statementType","estimatedBytes","bytesProcessed"}
POST /query                     body = SQL text, or {"sql": ..., "location": ...}
                                DDL/DML -> {"jobId","affectedRows",...}
GET  /_info
```

## backends.conf

```ini
[mylocal.bq]
project   = my-gcp-project                                  # required
token_cmd = gcloud auth print-access-token                  # or token = ${TOKEN} / token_keychain = ...
location  = EU                                              # optional; BigQuery infers it otherwise
dataset   = analytics                                       # optional default for GET /tables
max_rows  = 10000                                           # page until this many, then "truncated": true
timeout   = 30                                              # per-call wait, seconds (timeoutMs)
# upstream = https://bigquery.googleapis.com                # default; override for a proxy/emulator
```

Service accounts: `token_cmd = gcloud auth print-access-token --impersonate-service-account=sa@proj.iam.gserviceaccount.com`.

## Notes

- **`GET` is gated by BigQuery itself** (ADR 0005): the statement is
  dry-run first — BigQuery parses and plans it without executing — and only
  a `statementType` of `SELECT` goes on to run. DDL, DML, scripts and
  procedure calls are a `405` naming what BigQuery called them. The dry run
  also reports `estimatedBytes`, so you see what a query would scan before
  it does.
- Rows are typed from the schema: `INTEGER`/`FLOAT`/`BOOLEAN` become
  numbers and booleans, `RECORD` becomes an object, `REPEATED` a list.
  `NUMERIC`, `BIGNUMERIC`, `TIMESTAMP`, `DATE` and friends stay strings —
  no precision games.
- Results page until `max_rows`; past that the response says
  `"truncated": true`. Add a `LIMIT` or raise `max_rows`.
- Query parameters aren't exposed yet; interpolate carefully or use a
  scripting `DECLARE`. The token runs through `gcloud` on every request
  (cached by `gcloud`, ~100 ms).
- Legacy SQL is off (`useLegacySql: false`) and not configurable.
