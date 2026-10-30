# Handler roadmap

Agreed candidates, in the order we intend to build them. The test for a
handler earning a place: curl can't reasonably get there alone (binary
protocol, request signing, a multi-call dance) *and* a `GET`-only route
means something. Anything that's already REST + bearer is `proxy`'s job,
and gets a recipe instead of code.

Each handler lands under ADR 0006's directory contract; none needs a new
ADR unless it changes the handler contract (noted where it might).

| # | item | kind | deps | status |
|---|---|---|---|---|
| 1 | **recipes** — `docs/recipes/`: `proxy`/`aws-sigv4` configs + example curls for GitHub, GCP (`gcloud auth print-access-token`), Azure, Vault, k8s, Prometheus, Linear, Slite, Slack, Jira; LLM pages for Anthropic, OpenAI, **Bedrock via `aws-sigv4`**, Vertex. Added `header_<name>_keychain` / `_cmd` to `proxy` so `x-api-key`-style APIs get the same secret sources | docs | — | **done** |
| 2 | **mysql** — `query` shape; GET under `START TRANSACTION READ ONLY` *plus* an `EXPLAIN` gate (MySQL DDL implicitly commits a read-only transaction and runs), single statement | handler | pymysql | **done** |
| 3 | **clickhouse** — `query` shape over its HTTP port; GET sets server-side `readonly=1` | handler | none | **done** |
| 4 | **graphql** — `GET /gql?query=&vars=&op=`, `POST /gql`, `GET /schema`, `GET /types/{T}`; GET refuses `mutation`/`subscription` documents (ADR 0005 records the exception) | handler | none | **done** |
| 5 | **mqtt** — `stream` shape: publish, SSE tail with `+`/`#`, retained message as `GET /stream/{t}`, `DELETE` clears retained | handler | paho-mqtt | **done** |
| 6 | **spec-aware `/_info`** — `openapi =` on `proxy`; `asyncapi =` on stream handlers (v2 and v3): operation/channel menu, schema summary, example curls on the intercepted hostname; shared in `jci.info()` so every handler has it | feature | PyYAML for YAML specs (JSON needs none) | **done** |
| 7 | **`jci_aws.py`** — SigV4 signer, credential resolution and `sign_request(cfg, ...)` lifted out of `aws-sigv4/handler.py`; the handler is now 40 lines of glue | refactor | none | **done** |
| 8 | **bigquery** — `query` shape; GET dry-runs first and executes only `statementType == SELECT` (and reports `estimatedBytes`); submit/poll/page collapsed; typed rows; token via `token_cmd = gcloud auth print-access-token` | handler | none | **done** (tested against a fake REST API; not run live) |
| 9 | **athena** — `query` shape via `jci_aws.py`; start → poll → page, typed rows, `/executions/{id}`; GET can't be gated pre-execution so the README ships the IAM deny-writes policy and the handler warns post-hoc; the test fixture recomputes every SigV4 signature | handler | none | **done** (fake API; not run live) |
| 10 | **llm** — text in, text out verbs (`/summarize`, `/explain`, `/fix`, `/translate?to=`, `/complete`, `prompt_<verb>`), `POST /chat`, `/chat/stream` SSE normalised, `GET /models`; Anthropic + OpenAI stdlib, Bedrock Converse via `jci_aws.py` (stream = one chunk); key via `api_key*`, `max_tokens` hard cap | handler | none | **done** (fake providers; not run live) |
| 11 | **asyncapi payload validation** — `validate = true` on kafka/rabbit/mqtt/redis rejects a `POST` whose body fails the channel's message schema (templated channels match); `strict` also refuses unknown channels | feature | jsonschema | **done** |
| 12 | **memcached** — `kv` shape, text protocol; `/keys` via `lru_crawler metadump`, `/stats` | handler | none | **done** |
| 13 | **ldap** — `GET /search`, `/find/{term}` (escaped quick lookup), `/entry/{dn}`, `/whoami`; read-only by construction; OpenLDAP in the demo compose | handler | ldap3 | **done** |
| 14 | **snowflake** — `query` shape over the SQL REST API v2 (sync, async poll, partitions); auth by PAT/OAuth token, key-pair JWT via `token_cmd`; GET runs under `readonly_role` (server-enforced) or warns post-hoc | handler | none | **done** (fake API; not run live) |
| 15 | **nats** — `stream` shape; JetStream gives real peek/replay | handler | nats-py | later |

## Parked

- **Env generation from a spec** (`jci env init --from-asyncapi` /
  `--from-openapi`): routes + backends for every server and channel. Needs
  YAML parsing in the CLI path; the CLI is bash (ADR 0003/0006). Possible
  as a Python tool jci shells out to — decide when 6 has landed.
- **per-SaaS handlers** (Linear, Slite, …): no. Recipes + `graphql` +
  `openapi` cover it without the drift.
- **`exec`/shell handler**: no. The sandbox story ends where it begins.
- **fs / git / sftp handlers**: curl, ssh and the remote-handler trick
  already cover these.

## Deferred minors from the library review (2026-10-30)

Polish items the whole-branch review raised and we chose not to fix in
that pass; pick up opportunistically.

- `make install` `rm -rf`s `handlers/lib`, losing user-written handlers
  placed there, and leaves stale `proxy.py`/`.venv`/`run-handler` from
  older installs. Document a `handlers/local/` or similar, clean stale files.
- Mongo `DELETE /db/{d}/coll/{c}` with no `?filter=` deletes everything;
  consider requiring an explicit filter.
- `run`'s `respond()` closes fd 1 but doesn't shut down the socket; drain
  is unbounded; invalid-name JSON isn't escaped.
- `forward_response` matches the `Location` origin as a bare prefix.
- `read_request`: `100 Continue` sent before the `JCI_MAX_BODY` check;
  lenient chunk-size parsing; no read timeout for a stalled client; error
  paths don't drain the body before closing.
- `handler_install` removes an existing venv when a re-run `pip` fails.
- SigV4: non-UTF-8 percent-escapes in S3 keys; e2e test should recompute
  the signature.
- Kafka: `_topic_meta` ignores the configured timeout; possible topic
  auto-creation on brokers with `auto.create.topics.enable` (document).
- Rabbit: quorum-queue delivery limits vs repeated nack-requeue peeks
  (document); tail non-destructiveness untested.
- Linux build and CI matrix — still never exercised.
