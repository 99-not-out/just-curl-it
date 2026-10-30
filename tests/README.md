# tests

```sh
make test           # builds the shim, runs the suite
./tests/run.sh      # equivalent
```

## What's covered

The suite is a single `run.sh` that exercises real shim behavior with a
real curl. Every test maps to a regression that actually bit during
development.

| section | what it checks |
|---|---|
| **build** | shim compiles; `intercept.{so,dylib}` exists |
| **config parsing** | comments + blank lines ignored; methods detected; handler with spaces preserved verbatim |
| **basic routing** | `getaddrinfo` + `connect` interpose chain end-to-end via brew curl + `run hello` |
| **run dispatcher** | `handlers/run`: a bash handler and a Python handler dispatch; unknown name → 404 listing available handlers; a path-traversal name → 404; a handler with `requirements.txt` and no venv → 503 naming `jci handler install`; dirs without an entrypoint (`__pycache__`) are not handlers |
| **jci handler** | `ls`/`info`/`install`/`uninstall` against a throwaway `JCI_HOME`, installing a fixture handler from a wheel built on the spot (`PIP_NO_INDEX`), running it through the shim under its venv, uninstalling and seeing the 503 return; `doctor` lists venv handlers |
| **handler: sqlite** | `/tables`, `GET /query` read-only (insert → 405 with `Allow: POST`), `POST /query` writes, `readonly = true` → POST 405, missing file → 502 and no file created |
| **handler: unix-http** | request forwarded over a temp Unix socket to an echo server: method, path+query, body, header; missing socket → 502 naming it |
| **method permissions** | GET/POST/DELETE allow-listed correctly; `Allow:` header on 405; a denied request never spawns the handler (the gate is in the shim, proven with a marker-file handler) |
| **sse streaming** | `tests/sse_clock.py` emits 5 events at 0.5s intervals; asserts events arrive *and* that `time_total - time_starttransfer > 0.8s`, so a buffering regression fails instead of passing quietly |
| **helpers** | `jci-run` restores the shim inside a `#!/usr/bin/env bash` script (the SIP-stripped case); `jci-routes` lists routes; `jci-run` outside an env gives a real error |
| **ipv6 v4-mapped** | tiny C client connects via `AF_INET6` + `::ffff:127.0.0.X` (the Java HttpClient case) — compiled at test time |
| **auth proxy** | injects `Authorization` and `header_*` against httpbin.org; auto-skipped unless two probes return 200 (httpbin is flaky, and a network blip shouldn't redden the suite) |
| **mtls upstream** | local TLS server with `verify_mode=CERT_REQUIRED`; asserts all three client-cert config forms (path, `_cmd` printing a path, `_cmd` printing PEM), that a missing cert fails the handshake instead of being accepted, and that cert-without-key / missing file / unexpanded `${VAR}` each name the problem. Also re-asserts header injection locally, so `inject_headers` stays covered when the httpbin section skips. Same server covers the proxy's `Location` rewrite (absolute upstream URL rewritten, relative untouched) and the `aws-sigv4` handler end to end (SigV4 `Authorization`, signed headers, `Host` with port, payload hash of a PUT body, encoded key path preserved) |
| **unit: jci.py** | `python3 -m unittest discover -s tests/unit`: `read_request` (Content-Length, duplicate headers, chunked, bad/truncated chunks, `100 Continue`, body cap → 413, bad request line → 400), `load_backend` (expansion, no interpolation, missing section/file named), `redact`, `forward_response`, `send_error`; and the SigV4 signer against the vendored AWS test-suite vectors plus S3-vs-generic path and `+`-in-query rules |

### `tests/handlers.sh` (`make test-handlers`)

Docker-backed, not part of `make test`. Needs `cd demos && docker compose
up -d` and network for pip; each section skips when its service isn't
running. Installs every handler into a throwaway `JCI_HOME` with
`jci handler install`, then exercises the documented routes: redis kv
round-trip, postgres read-only GET vs POST (including a `GET`-only route
where the shim gate and the read-only transaction are both proven), smtp
delivery counted in MailHog, kafka produce/peek/peek-again (non-destructive),
grpc reflection + unary call + 404/400, mongo insert/find/sort/delete,
rabbit peek twice with `redelivered: true` the second time.

## Requirements

- macOS: Homebrew `curl` (`brew install curl`) — system `/usr/bin/curl` is
  SIP-protected and won't load the shim.
- Any working `cc` (clang or gcc) — used to compile the v4-mapped probe.
- `python3` (≥ 3.9) on PATH for the stdlib handlers, the unit tests, the
  SSE clock handler and the fixture servers. `pip` is only used to install
  a locally-built wheel (no network).
- `openssl` for the mTLS fixture's throwaway CA; the section skips without it.
- macOS: a Homebrew `zsh` or `bash` for the helper tests (SIP strips the
  preload var from `/bin/*` shells); skipped if absent.
- Optional: internet for the auth-proxy test (skipped if `httpbin.org` is
  unreachable).
