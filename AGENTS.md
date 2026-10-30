# Working on jci

jci is an `LD_PRELOAD` / `DYLD_INSERT_LIBRARIES` shim that makes a set of
fake hostnames resolve to per-request handler processes, so `curl
http://mylocal.postgres/query?sql=...` works inside one shell and nowhere
else. Read `README.md` first — especially "But why?" and "What it's not".
Those two sections are the product; code that fights them is wrong even if
it works.

## Layout

| path | what | language |
|---|---|---|
| `lib/intercept.c` | the shim: `getaddrinfo`/`connect` interposer, fork, method gate | C |
| `bin/jci` | `jci ls / use / doctor / handler` — the CLI | bash |
| `lib/helpers.sh` | `jci-run`, `jci-routes`, `jci-info`, sourced into the env | bash |
| `handlers/run` | dispatcher: `run <name>` picks the venv or `python3` and execs the handler | bash |
| `handlers/lib/jci.py` | shared stdlib plumbing every Python handler imports | Python |
| `handlers/lib/jci_aws.py` | SigV4 signer + AWS credential resolution, shared by aws-sigv4 / athena / bedrock paths | Python |
| `handlers/lib/<name>/` | one handler: `handler.py\|sh`, `README.md`, optional `requirements.txt` | bash, Python |
| `demos/` | optional: docker backends, the `demos` env, tour, client apps | — |
| `tests/run.sh` | the suite — real shim, real curl; sources `tests/lib.sh` | bash |
| `tests/handlers.sh` | docker-backed handler tests (`make test-handlers`) | bash |
| `tests/unit/` | `unittest` modules for `jci.py` and the SigV4 signer | Python |
| `docs/adr/` | architecture decision records | — |

## Decisions are recorded; check them

Before changing `lib/`, `bin/`, `handlers/`, the `Makefile` or the handler
contract, read `docs/adr/README.md` and the ADRs it indexes. Then:

- If your change **contradicts** an ADR, don't quietly do it. Either stop
  and raise it, or write a new ADR that supersedes the old one (set the
  old one's status to `Superseded by NNNN`) and commit both together.
- If your change **makes a new decision** of the kind ADRs cover —
  routing, the process model, where enforcement lives, what the core is
  written in, the handler contract, install layout — add an ADR in the
  same commit. Copy `docs/adr/template.md`; keep it under a screen; be as
  honest in Consequences as the README is in "What it's not".
- Bug fixes, refactors and new handlers that follow the directory
  contract need no ADR.

`.githooks/pre-commit` reminds about this when core files are staged with
no ADR. If `git config core.hooksPath` is unset in your checkout, run
`make hooks` once. The hook warns; it doesn't block unless
`JCI_ADR_STRICT=1`.

## Rules the ADRs imply (short form)

- **Core stays bash + C** (ADR 0003, amended by 0006). jci's own logic is
  bash; Python is the thing it manages. `jci ls`, `use` and `doctor` must
  work with no `python3` on `PATH`. `handlers/lib/jci.py` and any handler
  without a `requirements.txt` are stdlib-only, Python 3.9+.
- **A handler is a directory** (ADR 0006): `handlers/lib/<name>/` with
  exactly one of `handler.py` / `handler.sh`, a `README.md` with the four
  sections (purpose, `## Routes`, `## backends.conf`, `## Notes`), and a
  `requirements.txt` only if it needs packages. No other manifest. `run`
  never parses the request; dependencies missing is a `503` naming
  `jci handler install <name>`, never a traceback.
- **No daemon** (ADR 0002). Handlers are per-request forked
  stdin/stdout filters. Don't add a long-lived process, a port, or state
  between requests.
- **Handlers don't check methods** (ADR 0004). The shim does, before
  exec. Don't re-add `enforce_methods` anywhere.
- **`GET` must not mutate** (ADR 0005). Use the backend's own read-only
  mechanism, never a regex over the request.
- **The shim understands exactly one line of HTTP** — the request line,
  for the gate. Don't teach it more.
- **Fail closed.** Ambiguous input to a safety check is a refusal.

## Building and testing

```sh
make build            # compiles lib/intercept.{dylib,so}
make test             # builds, then tests/run.sh (no docker, no network beyond httpbin, which skips)
make test-handlers    # tests/handlers.sh against demos/docker-compose.yml; needs `docker compose up -d`
make install          # into ~/.just_curl_it
make demos-install    # install + jci handler install for the demos env
```

The suite is end-to-end against the real shim with the real curl. On
macOS it needs Homebrew `curl` and a Homebrew `zsh`/`bash` (SIP strips the
preload var from `/usr/bin/*` and `/bin/*`); `tests/README.md` lists the
rest. Run `make test` before claiming anything works; paste the summary
line. If you touched a handler with dependencies, run `make test-handlers`
too. Don't run the two concurrently: both rebuild the shim. If you change
`intercept.c`, compile with `-Wall -Wextra` and keep it warning-free.
Linux cannot be tested from a Mac — say so when it applies rather than
implying it was.

A handler can be exercised without `jci use` by setting the env the shim
reads directly:

```sh
env DYLD_INSERT_LIBRARIES=$PWD/lib/intercept.dylib \
    INTERCEPT_CONFIG=/path/routes.conf INTERCEPT_BACKENDS_CONF=/path/backends.conf \
    "$(brew --prefix curl)/bin/curl" -s http://some.host/path
```

## Voice

The README is plain, specific and honest about limits. Match it in docs,
comments and commit messages: say what something does and what it costs,
skip adjectives, and put the caveat next to the claim it qualifies.
