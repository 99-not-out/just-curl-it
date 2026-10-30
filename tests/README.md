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
| **basic routing** | `getaddrinfo` + `connect` interpose chain end-to-end via brew curl + `hello.sh` |
| **method permissions** | GET/POST/DELETE allow-listed correctly; `Allow:` header on 405; a denied request never spawns the handler (the gate is in the shim, proven with a marker-file handler) |
| **sse streaming** | `tests/sse_clock.py` emits 5 events at 0.5s intervals; asserts events arrive *and* that `time_total - time_starttransfer > 0.8s`, so a buffering regression fails instead of passing quietly |
| **helpers** | `jci-run` restores the shim inside a `#!/usr/bin/env bash` script (the SIP-stripped case); `jci-routes` lists routes; `jci-run` outside an env gives a real error |
| **ipv6 v4-mapped** | tiny C client connects via `AF_INET6` + `::ffff:127.0.0.X` (the Java HttpClient case) — compiled at test time |
| **auth proxy** | injects `Authorization` and `header_*` against httpbin.org; auto-skipped unless two probes return 200 (httpbin is flaky, and a network blip shouldn't redden the suite) |
| **mtls upstream** | local TLS server with `verify_mode=CERT_REQUIRED`; asserts all three client-cert config forms (path, `_cmd` printing a path, `_cmd` printing PEM), that a missing cert fails the handshake instead of being accepted, and that cert-without-key / missing file / unexpanded `${VAR}` each name the problem. Also re-asserts header injection locally, so `inject_headers` stays covered when the httpbin section skips |

## Requirements

- macOS: Homebrew `curl` (`brew install curl`) — system `/usr/bin/curl` is
  SIP-protected and won't load the shim.
- Any working `cc` (clang or gcc) — used to compile the v4-mapped probe.
- `python3` on PATH for the SSE clock handler and the mTLS server.
- `openssl` for the mTLS fixture's throwaway CA; the section skips without it.
- macOS: a Homebrew `zsh` or `bash` for the helper tests (SIP strips the
  preload var from `/bin/*` shells); skipped if absent.
- Optional: internet for the auth-proxy test (skipped if `httpbin.org` is
  unreachable).
