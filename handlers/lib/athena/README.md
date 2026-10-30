# athena

SQL over HTTP for Amazon Athena — the shared `query` shape, with
StartQueryExecution → poll → page collapsed into one response and the
result set typed. Stdlib only; requests are SigV4-signed by `jci_aws`.

```sh
curl -sG http://mylocal.athena/query --data-urlencode 'sql=select region, count(*) n from logs.requests where dt = current_date group by 1' | jq .rows
curl -s http://mylocal.athena/databases
curl -s 'http://mylocal.athena/tables?database=logs'
```

## Routes

```
GET  /databases                 {"databases": [...]}
GET  /tables?database=D         {"database", "tables": [{name, type}]}
GET  /query?sql=...             {"columns","types","rows","rowCount","executionId","state",
                                 "statementType","dataScannedBytes","executionMillis"}
POST /query                     body = SQL text, or {"sql", "database", "workgroup"}
GET  /executions/{id}           status + results of an earlier execution (long queries)
GET  /_info
```

A failed query is a `400` carrying Athena's `StateChangeReason`.

## backends.conf

```ini
[mylocal.athena]
region     = eu-west-1                         # required
database   = logs                              # default database
workgroup  = primary                           # optional
output     = s3://my-athena-results/jci/       # optional if the workgroup enforces one
catalog    = AwsDataCatalog                    # default
max_rows   = 10000
timeout    = 5

# credentials, as for aws-sigv4: first configured wins
profile         = default
# credentials_cmd = aws configure export-credentials --format env
# access_key = ${AWS_ACCESS_KEY_ID} / secret_key = ${AWS_SECRET_ACCESS_KEY} / session_token = ...
```

## Read-only means the credential, not the handler

Athena has no pre-execution classifier (no dry run, no read-only
session). The handler **cannot** refuse `CREATE TABLE AS` or `INSERT INTO`
on `GET` before they run — so unlike `postgres`, `mysql`, `sqlite`,
`clickhouse` and `bigquery`, a `GET`-only route here is read-only exactly
to the extent the credential is. Make it so at the data layer (ADR 0005:
the backend's own mechanism, which for Athena is IAM / Lake Formation):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution",
                                   "athena:GetQueryResults", "athena:ListDatabases", "athena:ListTableMetadata",
                                   "glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables",
                                   "glue:GetPartition", "glue:GetPartitions"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": ["arn:aws:s3:::my-data-*", "arn:aws:s3:::my-data-*/*"]},
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": ["arn:aws:s3:::my-athena-results", "arn:aws:s3:::my-athena-results/jci/*"]},
    {"Effect": "Deny",  "Action": ["glue:Create*", "glue:Update*", "glue:Delete*", "glue:BatchCreate*", "glue:BatchDelete*"], "Resource": "*"},
    {"Effect": "Deny",  "Action": ["s3:PutObject", "s3:DeleteObject"], "NotResource": ["arn:aws:s3:::my-athena-results/jci/*"]}
  ]
}
```

With that, `CREATE TABLE` fails on `glue:CreateTable`, `INSERT INTO` fails
on `s3:PutObject` to the table's location, and the only writable place is
the results prefix. Or grant `SELECT` only through Lake Formation.

What the handler does do: every response carries Athena's post-hoc
`statementType`, and a `GET` that turns out to have executed DDL or
non-SELECT DML gets a `"warning"` field — so a credential that isn't
read-only shows itself the first time, not never.

## Notes

- Results are typed from the column metadata: integer types, `double`,
  `boolean` become JSON numbers/booleans; `decimal`, `date`, `timestamp`,
  `varchar` and complex types stay strings. The header row Athena puts
  first on `SELECT` results is dropped.
- Polling backs off from 0.3 s to 3 s, up to 10 minutes; past that a `504`
  with the `executionId` — fetch it later with `GET /executions/{id}`.
- Each query costs a scan; `dataScannedBytes` is in every response.
- Not run against AWS from this repo; the suite verifies the SigV4
  signature upstream and drives the state machine against a fake.
