# Google Cloud

One auth line covers every Google API: `gcloud auth print-access-token`
prints a short-lived OAuth token for whoever `gcloud` is logged in as (or
for Application Default Credentials with `--impersonate-service-account`).

## routes.conf

```ini
gcp.bq        GET    ${JCI_HANDLERS}/run proxy     # BigQuery REST
gcp.storage   GET    ${JCI_HANDLERS}/run proxy     # Cloud Storage JSON API
gcp.vertex           ${JCI_HANDLERS}/run proxy     # Vertex AI (POST-only API)
```

## backends.conf

```ini
[gcp.bq]
upstream          = https://bigquery.googleapis.com
authorization_cmd = printf 'Bearer %s' "$(gcloud auth print-access-token)"

[gcp.storage]
upstream          = https://storage.googleapis.com
authorization_cmd = printf 'Bearer %s' "$(gcloud auth print-access-token)"

[gcp.vertex]
upstream          = https://europe-west1-aiplatform.googleapis.com     # pick your region
authorization_cmd = printf 'Bearer %s' "$(gcloud auth print-access-token)"
```

## Try

```sh
# BigQuery: datasets, then a query (the raw API; the `bigquery` handler will wrap the job dance)
curl -s http://gcp.bq/bigquery/v2/projects/$PROJECT/datasets | jq '.datasets[].datasetReference.datasetId'
curl -s -X POST http://gcp.bq/bigquery/v2/projects/$PROJECT/queries \
     -d '{"query":"SELECT 1 AS one","useLegacySql":false}' | jq '.rows'       # needs POST on the route

# Cloud Storage
curl -s "http://gcp.storage/storage/v1/b/$BUCKET/o?prefix=reports/" | jq '.items[].name'
curl -s "http://gcp.storage/storage/v1/b/$BUCKET/o/reports%2Flatest.json?alt=media"

# Vertex AI -- Claude via the Anthropic publisher endpoint (rawPredict speaks the Messages API)
curl -s -X POST "http://gcp.vertex/v1/projects/$PROJECT/locations/europe-west1/publishers/anthropic/models/claude-opus-5-5:rawPredict" \
     -d '{"anthropic_version":"vertex-2023-10-16","max_tokens":1024,
          "messages":[{"role":"user","content":"one line: why curl?"}]}' | jq -r '.content[0].text'
```

## Notes

- `gcloud auth print-access-token` runs on every request (tokens last an
  hour; `gcloud` caches, so it's fast). Nothing is written to disk.
- A `GET`-only route on `gcp.bq` makes BigQuery *metadata* read-only, but
  queries are `POST /queries`, so you can't run SQL through a `GET`-only
  route at all. The `bigquery` handler on the roadmap exposes
  `GET /query?sql=` and dry-runs first so only `SELECT` executes.
- Vertex model IDs for current Claude models are the bare first-party IDs
  (`claude-opus-5-5`); `region` can also be `global`.
- Service accounts: `gcloud auth print-access-token --impersonate-service-account=sa@project.iam.gserviceaccount.com`.

*Not run live from this repo.*
