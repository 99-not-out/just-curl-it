# 0002. Handlers are per-request forked stdin/stdout filters; no daemon

- Status: Accepted
- Date: 2026-10-30 (records a decision made at project start)
- Governs: `lib/intercept.c` (`spawn_handler_and_connect`), `handlers/run`, `handlers/lib/`, `README.md` ("What it's not")

## Context

Something has to speak the backend protocol. The conventional shape is a
local daemon: a long-lived process listening on a port, with a config
reload story, a lifecycle, logs, and a `brew services` entry. That is a
reverse proxy, and good ones already exist.

What jci wants is for a handler to be as easy to write as a shell
one-liner, to pick up edits on the next request, and to leave nothing
running when the shell exits. Those three properties point the same way.

## Decision

The shim `fork()`s a fresh handler process for every intercepted
connection, double-forked so it reparents to init and the client never
reaps it. The handler is run as `/bin/sh -c <handler-cmd>` with stdin and
stdout both the accepted socket: it reads one raw HTTP/1.x request and
writes one raw HTTP/1.x response, then exits. `INTERCEPT_HOST` names the
route. The handler runs *without* the shim (the preload var is unset
before exec), so it reaches real backends normally. Nothing persists
between requests, and the shim imposes no lifetime on a handler once the
client has connected, so streaming responses just work.

## Consequences

- A handler can be `cat`, a five-line bash script, a Python file, or
  `ssh box ~/handler.py`. The contract is bytes in, bytes out.
- Edit a handler, next request runs the new one. No reload, no restart.
- `exit` the activated shell and jci is gone; no daemon to find later.
- Fork-per-request is slow in absolute terms. Python handlers that import
  heavy client libraries pay their import cost on every request. This is
  the price of the model and is documented, not hidden.
- There is no keep-alive: every response says `Connection: close`. Clients
  that pool connections reconnect per request, which costs a fork each.
- No shared state between requests means no connection pooling to the
  backend either; a handler that needs that should talk to something that
  has it (a local pgbouncer, say) rather than try to become a daemon.
- Request bodies are read in full before the handler acts (there is no
  streaming request path). Since ADR 0006, `handlers/lib/jci.py` decodes
  chunked bodies and answers `Expect: 100-continue`; bodies are capped at
  `JCI_MAX_BODY` (64 MiB default).
