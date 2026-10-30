# 0005. `GET` must not mutate; handlers enforce it at the backend

- Status: Accepted
- Date: 2026-10-30
- Governs: the handler contract; `handlers/lib/postgres/` and `handlers/lib/sqlite/` as the reference implementations

## Context

ADR 0004 makes `GET`-only routes real at the HTTP layer. It says nothing
about what a handler does with a `GET`. The Postgres handler ran whatever
SQL arrived in `GET /query?sql=...`, so `sql=DROP TABLE users` sailed
through a `GET`-only route and the README's "can't mutate prod" claim was
false for the backend where it matters most.

The tempting fix is a regex over the statement. It is wrong in all the
usual ways (`WITH ... AS (DELETE ...)`, functions with side effects,
dialect differences) and would have to be re-invented per backend.

## Decision

A handler that exposes any `GET` route must ensure that `GET` cannot
change backend state, and must do so using the backend's own mechanism
rather than by inspecting the request. For Postgres that is a read-only
transaction (`default_transaction_read_only`), which the server enforces;
a refused write is reported as `405` with `Allow: POST` and a hint. Writes
go through `POST` (or `PUT`/`DELETE` where the shape calls for it), which
a `GET`-only route never admits.

Where a backend has no such mechanism, the handler must not offer the
operation on `GET` at all. One deliberate exception to "never inspect the
request": the `graphql` handler reads a document's top-level operation
keyword and refuses `mutation`/`subscription` on `GET`. That keyword is
the protocol's own declaration of intent (a `query` that writes is a
server bug no client can detect), not a heuristic over what the body
does, so it counts as the backend's classification.

Where the backend offers nothing pre-execution — Athena has no dry run and
no read-only session — the mechanism is the credential: IAM / Lake
Formation grants that make writes fail at the data layer. The `athena`
handler's README ships that policy, and the handler reports the post-hoc
`statementType` with a `warning` on any `GET` that turns out to have run
DDL or non-SELECT DML, so a credential that isn't read-only is visible the
first time rather than never. That is weaker than the other SQL handlers
and the README says so in its own section. Redis `GET /kv/{k}` and Mongo `GET .../coll/{c}`
are reads by construction. RabbitMQ `GET /stream/{q}/peek` was destructive
(`basic_get` with auto-ack) and documented as the one leak in the unified
shape; since ADR 0006 it fetches without ack and requeues, and `tail`
holds messages unacked so the broker requeues them on disconnect. The
remaining cost — messages held by an open tail are invisible to other
consumers — is stated in the handler's README. Queues are found by passive
declare, so a `GET` creates nothing (a missing queue is a `404`).

Two holes the first review of the library found, and the fixes that are
now regression tests: Postgres's simple-query protocol runs several
statements, so `GET /query?sql=COMMIT;DROP TABLE x` ended the read-only
transaction — `GET` now uses a prepared (single-statement) execute. SQLite
opened read-only still wrote *other* files via `VACUUM INTO` and `ATTACH`
— read-only connections now refuse attachments. MySQL's `START
TRANSACTION READ ONLY` refuses DML but DDL implicitly commits it and runs,
so the `mysql` handler adds a second server-side gate: `EXPLAIN <sql>`,
which the parser accepts only for queries and DML and which executes
nothing. The lesson for new handlers: "the backend refuses writes" must
cover every channel the statement language has, not just the one you
first thought of — and the test suite must try the hostile statements.

## Consequences

- Combining a `GET`-only route with a conforming handler gives a
  read-only view of a backend that the backend itself guarantees, not a
  heuristic.
- Handler authors have one more rule to follow, and the README says so
  plainly: `GET` is only as safe as the handler makes it.
- Some backends cannot express "read-only" at all; for those, the honest
  answer is a smaller `GET` surface, not a clever parser.
