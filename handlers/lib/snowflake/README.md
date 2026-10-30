# snowflake

SQL over HTTP for Snowflake — the shared `query` shape over the SQL REST
API v2, with async statements polled and result partitions stitched.
Stdlib only; auth is a programmatic access token or OAuth token.

```sh
curl -sG http://mylocal.snow/query --data-urlencode 'sql=select region, sum(amount) from sales group by 1' | jq .rows
curl -s 'http://mylocal.snow/tables?schema=PUBLIC' | jq '.rows[] | .[1]'
```

## Routes

```
GET  /tables?schema=S           SHOW TERSE TABLES IN SCHEMA S
GET  /query?sql=SELECT...       {"columns","types","rows","rowCount","totalRows","statementHandle",...}
                                runs under readonly_role when set (see below)
POST /query                     body = SQL text, or {"sql","database","schema","warehouse","role"}
GET  /statements/{handle}       results of an earlier statement; 202 while still running
GET  /_info
```

## backends.conf

```ini
[mylocal.snow]
account        = xy12345.eu-west-1               # -> https://xy12345.eu-west-1.snowflakecomputing.com
token_keychain = snowflake-pat                   # or token = ${SNOWFLAKE_TOKEN} / token_cmd = ...
token_type     = PROGRAMMATIC_ACCESS_TOKEN       # or OAUTH
database       = ANALYTICS
schema         = PUBLIC
warehouse      = WH_SMALL
role           = ANALYST                         # session role for POST
readonly_role  = ANALYST_RO                      # role for every GET statement
max_rows       = 10000
statement_timeout = 3600                         # seconds Snowflake may run a statement
timeout        = 10
```

### Key-pair auth

Stdlib can't sign RS256, so a key-pair JWT is minted outside and handed
in with `token_cmd` (and `token_type = KEYPAIR_JWT`):

```ini
token_type = KEYPAIR_JWT
token_cmd  = python3 -c 'import jwt,hashlib,base64,time,sys; ...'   # any script printing the JWT
```

A dozen-line script with PyJWT + cryptography does it; `snowsql` users can
also set `token_cmd = snow connection generate-jwt -c prod` with the
Snowflake CLI.

## Read-only means a role

Snowflake has no dry run. The mechanism it *does* have is privileges:
set `readonly_role` to a role granted only `USAGE` and `SELECT`, and every
`GET` statement runs under it — a write comes back `405` carrying
Snowflake's "insufficient privileges" (ADR 0005, the backend enforcing).
Without `readonly_role`, `GET` runs as the session role and the response
carries a `warning` when the statement's stats show rows written. That is
after the fact; set the role.

## Notes

- Rows are typed from `rowType`: `fixed` with scale 0 → int, scaled
  decimals stay exact strings, `real` → float, `boolean` → bool,
  `object`/`array`/`variant` parsed; dates and timestamps are returned as
  Snowflake sends them in `jsonv2` (epoch-based text) — add `::text` in
  the SQL for readable ones.
- Async statements (Snowflake answers 202 for long ones) are polled with
  backoff up to 10 minutes, then `504` with the handle for
  `GET /statements/{handle}`.
- Result partitions are fetched until `max_rows`; past that
  `"truncated": true`.
- Not run against Snowflake from this repo; the suite drives a fake SQL
  API covering sync, async, partitions, the role gate and auth.
