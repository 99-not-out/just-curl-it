# 0004. Method allow-lists are enforced by the shim, not by handlers

- Status: Accepted
- Date: 2026-10-30
- Governs: `lib/intercept.c` (`peek_method`, `method_allowed`, `gate_reject`), the handler contract

## Context

A route line may carry an HTTP method allow-list (`mylocal.db  GET  ...`).
The README sells this as the LLM-sandbox safety story: "agent can read
prod, can't mutate it" as a config line.

Until this decision the shim only *exported* the list (`INTERCEPT_METHODS`)
and each handler was expected to check it. `proxy.py`, `shim.py` and
`hello.sh` each carried a copy of the check. A handler that omitted it —
any user-written one — was ungated, and nothing told you. The safety
property depended on every handler author remembering, which is the
opposite of what the README promised.

The shim does not otherwise parse HTTP, and ADR 0002 wants handlers to see
the raw, unconsumed request. `recv(MSG_PEEK)` resolves that: the request
line can be read without removing it from the socket.

## Decision

In the forked child, after `accept()` and before `exec`, a gated route
peeks the request line and compares the method against the allow-list. A
denied method is answered with `405 Method Not Allowed`, an `Allow:`
header and a small JSON body, and the handler is never spawned. Bytes
that are not an HTTP/1.x request line (a TLS hello, garbage) fail closed
with `400`. After answering, the child drains the client's bytes so the
close is a FIN rather than a RST. The wait is bounded by the same alarm
that bounds `accept()`.

`INTERCEPT_METHODS` is still exported, for display only. Handlers do not
check methods. The bundled copies of the check were deleted, along with
the unit tests that existed only to cover them; the suite instead proves
with a marker-file handler that a denied request spawns nothing.

## Consequences

- The gate is uniform: a five-line bash handler is gated exactly as well
  as a Python one, and a handler cannot opt out.
- Handlers got simpler, and one piece of duplicated logic is gone.
- The shim now contains ~120 lines that know what an HTTP request line
  looks like. That is the full extent of HTTP it understands, and should
  stay so; richer policy belongs in handlers.
- A request whose method is split across TCP segments is handled by
  re-peeking; a client that connects and sends nothing dies on the alarm
  as before.
- `GET` is allowed through by the gate, so the gate alone does not make a
  route read-only. That is ADR 0005.
