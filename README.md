# just curl it

> Talk to Kafka, Postgres, Redis, Mongo, RabbitMQ, gRPC and SMTP with plain `curl`.
> No client library. No SDK. No server. **Just curl it.**

An `LD_PRELOAD` shim hijacks HTTP calls to a set of fake hostnames and pipes them
to small handler processes that speak each backend's real wire protocol. From
inside the session, every service looks like a plain HTTP endpoint.

`jci use <env>` drops you in a subshell where plain HTTP `curl`s reach Kafka,
Postgres, Redis, SMTP, gRPC, the GitHub API — anything you can wrap in a shell
or Python script — through your own handler scripts. No daemon, no `/etc/hosts`
edits, no `sudo`, no port-80 fights. Exit the shell and it's all gone.

```sh
jci use demos
curl http://mylocal.postgres/query?sql=select+now\(\)
curl http://mylocal.redis/kv/foo --data-binary 'hello' -X PUT
```

## But why?

- **Per-shell, per-secret scope.** Tokens injected by the auth-proxy handler
  are visible only to processes in *this* terminal session. Other terminals,
  browsers, daemons, leaky containers — none of them see your shell's
  routing or its credentials.
- **Scripting / REPL feel.** Handlers are stdin/stdout filters — a 5-line
  bash script can be a handler. No HTTP server framework, no port plumbing,
  no daemon lifecycle. Edit a handler, next request picks it up.
- **Multi-protocol uniformity.** Once a service is wrapped, everything in
  the shell talks to it the same way: `curl`, `python -m urllib`,
  `java -cp ... HttpClient`, `jq | curl | jq` pipelines, `httpie`. No SDKs.
- **Sandbox for an LLM.** Start `jci use prod-readonly` then start an
  agentic tool (Claude, Aider, anything) inside it. Two wins:
  - **Permissions.** Routes are scoped by the credentials in *that* env,
    and per-route HTTP-method allow-lists turn `GET`-only into "agent can
    read prod, can't mutate it" — a config line, not a code review.
  - **No protocol side-quests.** The agent doesn't need a Kafka client
    library, a Postgres driver, a gRPC stub generator, a BSON encoder, or
    a fight with whichever Python venv is on PATH today. Every service
    looks like `curl http://mylocal.<service>/<path>` — one tool the
    agent already knows perfectly. Protocol-specific knowledge lives in
    the handler, written once by a human; the agent just makes HTTP
    requests. Hours of "let me figure out how Kafka authenticates"
    collapse into a single curl. **Just curl it.**

## What it's not

- **Not production infrastructure.** Fork-per-request is slow (Python cold
  imports show), there's no observability, no HA, no graceful shutdown.
- **Not a full reverse proxy.** No HTTPS termination on the listening side,
  no caching, no rate limiting. For prod use nginx / Caddy / Envoy.
- **Not universal.** Won't intercept Go binaries (pure-Go DNS + raw
  syscalls), statically-linked binaries, musl-linked binaries, or anything
  that doesn't go through libc.

If you want a service mesh or a reverse proxy, use one. jci's the right
tool when the answer to "where should this run" is "in this terminal, for
the next hour."

## Install

```sh
make install
export PATH="$HOME/.just_curl_it/bin:$PATH"
jci ls          # see envs
jci doctor      # preflight
jci use dev     # activate
```

macOS needs Homebrew zsh + curl (SIP strips `DYLD_INSERT_LIBRARIES` from
`/bin/zsh` and `/usr/bin/curl`):

```sh
brew install zsh curl
```

`jci use` auto-detects brew curl and prepends it to `PATH` inside the
activated shell, so plain `curl` works.

## Inside the env

`jci use` sources `lib/helpers.sh` into the activated shell, which adds:

| helper | what it does |
|---|---|
| `jci-routes` | print the active env's route table, columns aligned |
| `jci-info` | fetch `/_info` from every route — the URL menu for an unfamiliar env |
| `jci-run '<cmd>'` | run `<cmd>` with the shim re-established (see below) |

`jci doctor` reports whether the helpers are installed.

**When you need `jci-run`.** In the interactive shell, plain `curl` just works.
But macOS SIP strips `DYLD_*` from any `/usr/bin/*` binary — including
`/usr/bin/env`, which is what `#!/usr/bin/env bash` runs. So a *script* you
launch from inside the env loses the shim before its first line executes, and
its `curl`s fail to resolve `mylocal.*`. Wrap them:

```sh
#!/usr/bin/env bash
. "$JCI_HELPERS"                       # exported by `jci use`
jci-run 'curl -s http://mylocal.shim/routes | jq'
```

This is macOS-only — Linux `LD_PRELOAD` survives `exec` — but `jci-run` is a
no-op cost on Linux, so scripts meant to be portable can just use it.
`demos/scripts/tour.sh` does exactly this. For the record, `jci use` also
exports `JCI_LIB`, `JCI_PRELOAD_VAR` and `JCI_SHELL`, so you can re-establish
the shim by hand:

```sh
env "$JCI_PRELOAD_VAR=$JCI_LIB" "$JCI_SHELL" -c 'curl -s http://mylocal.hello/'
```

## Routes

A route line is `<hostname>  [<METHODS>]  <handler-cmd>` — methods optional,
defaults to all. The handler runs under `/bin/sh -c` with stdin = request,
stdout = response.

```ini
# everything allowed
mylocal.kafka       ${JCI_HANDLERS}/run kafka

# only GET; POST/PUT/DELETE return 405
mylocal.prod-db     GET             ${JCI_HANDLERS}/run postgres

# read + write, no delete
mylocal.api         GET,POST,PUT    ${JCI_HANDLERS}/run proxy
```

The allow-list is enforced by the shim itself, before the handler is
spawned: it peeks the request line off the socket, and a denied method gets
a `405` (with an `Allow:` header) without any handler code running. Handlers
don't check methods, can't forget to, and a five-line bash handler is gated
exactly as well as a Python one.

Method-level perms are the headline safety story for the LLM-sandbox case:
"agent can read prod, can't mutate it" is a config line, not a code review.
One caveat worth knowing: `GET` is only as safe as the handler makes it.
The bundled Postgres handler runs `GET /query` in a read-only transaction
for exactly this reason; a handler you write should do the same for its
own backend.

## Auth proxy

The `proxy` handler forwards plain HTTP to an HTTPS upstream with an
`Authorization` header injected — your shell history says `curl http://gh.api/...`
instead of pasting tokens into every command. Auth comes from one of:

```ini
[gh.api]
upstream = https://api.github.com

# 1) Env var indirection — token never on disk:
authorization          = Bearer ${GITHUB_TOKEN}

# 2) macOS Keychain — encrypted at rest, Touch ID unlock:
#    security add-generic-password -a $USER -s github-api-token -w 'ghp_xxx'
authorization_keychain = github-api-token

# 3) Arbitrary shell pipe — for pass / age / sops / bw / etc:
authorization_cmd      = pass show github/api

# Plus any other headers, prefixed with `header_`:
header_accept = application/vnd.github+json
```

No bespoke encryption — the OS keychain (or whatever secret store you
trust) handles secrets at rest. The proxy is stdlib-only Python and runs
under whatever `python3` is on `PATH`, including macOS's system one:
handlers run with the shim unset, so the SIP caveats elsewhere in this
README don't apply to them. Nothing in the core needs a venv.

### Client certificates (mTLS upstreams)

For an upstream that wants a client cert — Teleport app access, a corp TLS
gateway — point `client_cert`/`client_key` at PEM files, or at a command that
produces them. `${VAR}` expands from the shell, same as `authorization`:

```ini
[cdk.console]
upstream        = https://console.internal.example.com

client_cert     = ${TSH_APP_CERT}                 # a path
client_key      = ${TSH_APP_KEY}

# ...or a command printing a path (ephemeral certs, refreshed per request):
client_cert_cmd = tsh apps config --format=cert
client_key_cmd  = tsh apps config --format=key

ca_cert         = /path/ca.pem     # optional; replaces the system trust store
```

A `_cmd` may print the PEM itself instead of a path, for secret stores that
hand back material rather than files — it goes to a `0600` temp file that's
unlinked as soon as the TLS context has loaded it. The two keys must be set
together, and a path that doesn't exist is reported as such, since the usual
cause is a login session that has quietly expired.

Redirects stay inside the shim: a `Location` header pointing back at the
upstream is rewritten onto the intercepted hostname, so `curl -L` follows
it through the proxy instead of escaping to the real host without auth.

Handlers can reuse all of this: `handlers/lib/jci.py` exposes
`load_backend`, `upstream_target`, `connect_upstream`, `inject_headers`
and `forward_response`, so a protocol-specific handler reaches an
authenticated upstream without reimplementing auth resolution or TLS
setup. The `proxy` handler itself is forty lines of glue over them.

**Scope reminder:** once `jci use gh-api` is active, every libc-using tool
in that shell that hits `gh.api` goes through the proxy — `curl`, `gh`,
`git` over HTTP, Python `requests`, etc. That's the point, but it's worth
knowing.

## Streaming

Handlers can stream — the shim doesn't bound their lifetime once the
client connects. `handlers/lib/jci.py` ships SSE primitives
(`send_sse_headers`, `sse_event`, `sse_comment`), and the demos use them:

- `GET /stream/<n>/tail` — uniform across Kafka, Postgres LISTEN, Redis
  pubsub, RabbitMQ. (See each handler's `/_info` for backend specifics.)

The only timeout the shim imposes is the *initial* accept window (default
30s, between handler spawn and the caller's `connect()`). Override with
`INTERCEPT_ACCEPT_TIMEOUT=<seconds>` — `0` disables it entirely.

## How it works

The shim is an `LD_PRELOAD` (Linux) / `DYLD_INSERT_LIBRARIES` (macOS) library
that hooks `getaddrinfo` and `connect`. Configured hosts resolve to a
per-route sentinel IP (`127.0.0.2`, `127.0.0.3`, …); when `connect()` sees
that IP, it binds an ephemeral loopback socket, forks the handler with
stdin/stdout = the accepted socket, and redirects the original connect
there. No daemon, no root, no loopback aliases needed — the sentinel IP
never reaches the kernel.

**Is this a hack that'll brick your laptop?**

No — `LD_PRELOAD`-on-`connect`
is well-trodden ground. [proxychains-ng](https://github.com/rofl0r/proxychains-ng)
and [tsocks](https://linux.die.net/man/8/tsocks) have used the same
mechanism since the early 2000s to redirect TCP through SOCKS. Address
Sanitizer, Thread Sanitizer, and gperftools all rely on the same library
interposition. On macOS, `DYLD_INSERT_LIBRARIES` is a documented system
feature; jci uses it the same way Apple's own diagnostic tools do. The
behaviour is well-bounded — only the activated shell and its child
processes are affected — and reversible: `exit` the subshell and every
trace of jci is gone. No system files modified, no daemons left running,
no kernel modules.

## Tests

```sh
make test
```

Real shim + real curl, end-to-end: routing, shim-enforced method
permissions, SSE streaming, the `AF_INET6` v4-mapped path, auth-proxy header
injection and mTLS upstreams. See `tests/README.md`.

## Demos

`make demos-install && cd demos && docker compose up -d` gets you a `demos`
env with Kafka / Redis / Postgres / Mongo / RabbitMQ / SMTP / gRPC, plus
walkthrough scripts and demo apps in Python and Java that drive every
backend without importing a single client library. See `demos/README.md`.

## Doesn't work

- **Go** binaries — pure-Go DNS + raw `connect(2)` syscall.
- **musl-linked** binaries (Alpine, distroless-static).
- **Statically-linked** binaries.
- **HTTPS** on the listening side (handlers speak plain HTTP).
- **macOS hardened-runtime binaries** without
  `com.apple.security.cs.allow-dyld-environment-variables`. Most Homebrew
  binaries are non-hardened, so usually fine.

## Honest comparison

| if you want | use |
|---|---|
| A real reverse proxy / service mesh | nginx, Caddy, Traefik, Envoy |
| HTTPS interception + scriptable inspect | mitmproxy |
| Per-port one-shot socket handlers | socat |
| LD_PRELOAD blanket SOCKS routing | proxychains, tsocks |
| Per-shell hostname routing with `cat`-simple handlers | jci |

That last row is small but real. Everything else here is convenience and
aesthetics. But sometimes you just want to curl it!

## Contributing

Design decisions live in `docs/adr/` as short architecture decision
records — why sentinel IPs, why no daemon, why the method gate is in the
shim, why the core stays bash + C. If a change contradicts one, supersede
it; if a change makes a new decision of that kind, add one in the same
commit. `make hooks` installs a pre-commit reminder that nudges when core
files change without an ADR alongside. `AGENTS.md` spells the same rules
out for coding agents.

## License

MIT
