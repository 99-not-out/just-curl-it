# 0006. Handlers are an opt-in library with per-handler venvs, managed by `jci handler`

- Status: Accepted
- Date: 2026-10-30
- Governs: `handlers/run`, `handlers/lib/`, `handlers/lib/jci.py`, `bin/jci` (`handler` subcommand), `Makefile` (`install`, `demos-install`)
- Amends: ADR 0003 (the "third-party packages belong in `demos/`" clause, and the reading of "never make `jci` need Python")

## Context

The moment this exists for is "I just downloaded it and now I want to curl
Kafka." The protocol handlers that make that possible lived under `demos/`,
installed as one lump with one venv by `make demos-install`, and read as
toys. They are the product.

ADR 0003 kept the core light by sending anything with third-party
dependencies to `demos/`. That protected the fast first run but left no
home for a real handler library: a Kafka handler needs `confluent-kafka`,
a Postgres one needs `psycopg`, and there is no honest way to make those
stdlib.

Two other things were wrong with the demo handlers as a library: the shared
helpers were duplicated between `demos/handlers/shim.py` and
`handlers/proxy.py`, and the request reader ignored chunked bodies and
`Expect: 100-continue`, both of which real clients send.

## Decision

A handler is a directory `handlers/lib/<name>/` containing exactly one
entrypoint (`handler.py` or `handler.sh`), a `README.md` with the sections
*purpose*, `## Routes`, `## backends.conf`, `## Notes`, and optionally a
`requirements.txt`. A present, non-empty `requirements.txt` means "needs a
venv"; otherwise the handler is ready as soon as `python3` exists. There is
no other manifest. `hello` and `proxy` move into the library too, so there
is one model.

`make install` copies the whole library inert — source and READMEs, a few
hundred KB — to `~/.just_curl_it/handlers/lib/`. Nothing is pip-installed
until asked. `jci handler install <name>` creates a venv at
`~/.just_curl_it/handlers/venv/<name>/` and installs that handler's
requirements; `uninstall` removes it; `ls` and `info` describe the library.
One venv per handler: grpcio's protobuf pin cannot fight pymongo's.

Routes reference `${JCI_HANDLERS}/run <name>`. `run` is a bash dispatcher
that picks the interpreter (the handler's venv if present, else `python3`
for stdlib handlers) and answers `404`/`503` itself when it cannot — a
missing venv is an HTTP error that names the install command, never a
traceback. `run` never parses the request.

Shared plumbing lives in `handlers/lib/jci.py`, stdlib-only, Python 3.9+:
request parsing (including chunked bodies and `100 Continue`), responses,
SSE, config loading with `${VAR}` expansion and a `timeout` key, secret
redaction for `/_info`, and the upstream-HTTP helpers that `proxy` and the
SigV4 handler share.

ADR 0003's "never make `jci` need Python" is to be read as: *jci's own logic
is bash; Python is the thing it manages.* `jci ls`, `jci use` and `jci
doctor` work with no `python3` on `PATH`; `jci handler ls` then reports
`unknown` for venv-bearing handlers rather than failing.

## Consequences

- A fresh `make install` is as light as before. Protocol handlers cost one
  command each, and only the ones you want.
- Disk: one venv per handler, tens to a couple of hundred MB each for the
  heavier clients. `uninstall --all` reclaims it.
- `jci handler install` needs network for pip. There is no offline
  bundle; the library ships source, not wheels.
- Handler status (`installed` vs `broken`) rests on an import probe derived
  from `requirements.txt` — a heuristic, with a `# import: <module>`
  escape hatch for packages whose module name differs.
- `${JCI_HANDLERS}/proxy.py` and `${JCI_HANDLERS}/run-handler <name>` route
  forms stop working. Pre-1.0, no known external users; the README and the
  demos env change in the same commit.
- Rabbit `peek` and `tail` become non-destructive by holding messages
  unacked (requeued on disconnect); while a tail is open those messages are
  invisible to other consumers. The handler README says so.
- ADR 0002's "chunked request bodies are not decoded" consequence no
  longer holds; `jci.py` decodes them and caps bodies at `JCI_MAX_BODY`.
- `demos/` keeps the docker backends, the `demos` env, the tour and the
  client apps — the examples of *using* handlers — and no handler code.
