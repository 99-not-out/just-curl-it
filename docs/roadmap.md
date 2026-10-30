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
| 6 | **spec-aware `/_info`** — `openapi = <url>` on `proxy`; `asyncapi = <path\|url>` on stream handlers: channel menu, schema summary, example curls | feature | PyYAML for YAML specs (JSON needs none) | planned |
| 7 | **`jci_aws.py`** — lift the SigV4 signer out of `aws-sigv4/handler.py` into a shared stdlib module for Athena and Bedrock | refactor | none | planned |
| 8 | **bigquery** — `query` shape; GET dry-runs first and executes only `statementType == SELECT`; token via `gcloud auth print-access-token` | handler | none | planned |
| 9 | **athena** — `query` shape via `jci_aws.py`; start → poll → page; README ships the IAM deny-writes policy since Athena has no pre-execution read-only check | handler | none | planned |
| 10 | **llm** — text in, text out: `POST /summarize` etc. from stdin, `POST /chat`, `/chat/stream` SSE normalised, `GET /models`; Anthropic + OpenAI stdlib, Bedrock via `jci_aws.py`; key via `${VAR}`/keychain/`_cmd`, `max_tokens` cap per shell | handler | none | planned |
| 11 | **asyncapi payload validation** — `validate = true` on stream handlers rejects a `POST` whose body fails the channel's message schema | feature | jsonschema | planned |
| 12 | **memcached** — `kv` shape, text protocol | handler | none | planned |
| 13 | **ldap** — `GET /search?base=&filter=`; read-only by nature | handler | ldap3 | planned |
| 14 | **snowflake** — `query` shape, SQL REST API with key-pair JWT | handler | none (jwt via `hmac`? needs RSA → `cryptography`) | planned |
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
