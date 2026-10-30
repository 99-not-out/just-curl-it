# 0005. `GET` must not mutate; handlers enforce it at the backend

- Status: Accepted
- Date: 2026-10-30
- Governs: the handler contract; `demos/handlers/postgres.py` as the reference implementation

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
operation on `GET` at all. Redis `GET /kv/{k}` and Mongo `GET .../coll/{c}`
are reads by construction. RabbitMQ `GET /stream/{q}/peek` is destructive
(`basic_get` with auto-ack) and is documented as the one leak in the
unified shape; it should become non-destructive or move off `GET`.

## Consequences

- Combining a `GET`-only route with a conforming handler gives a
  read-only view of a backend that the backend itself guarantees, not a
  heuristic.
- Handler authors have one more rule to follow, and the README says so
  plainly: `GET` is only as safe as the handler makes it.
- Some backends cannot express "read-only" at all; for those, the honest
  answer is a smaller `GET` surface, not a clever parser.
