# tests

```sh
make test           # builds the shim, runs the suite
./tests/run.sh      # equivalent
```

## What's covered

The suite is a single `run.sh` that exercises real shim behavior with a
real curl, plus subprocess-based unit tests for the Python gate logic.
Every test maps to a regression that actually bit during development.

| section | what it checks |
|---|---|
| **build** | shim compiles; `intercept.{so,dylib}` exists |
| **config parsing** | comments + blank lines ignored; methods detected; handler with spaces preserved verbatim |
| **basic routing** | `getaddrinfo` + `connect` interpose chain end-to-end via brew curl + `hello.sh` |
| **method permissions** | GET/POST/DELETE allow-listed correctly; `Allow:` header on 405 |
| **ipv6 v4-mapped** | tiny C client connects via `AF_INET6` + `::ffff:127.0.0.X` (the Java HttpClient case) — compiled at test time |
| **auth proxy** | injects `Authorization` and `header_*` against httpbin.org; auto-skipped if offline |
| **python unit tests** | `proxy.py` and `shim.py` `enforce_methods`: no-restriction / `*` / in-list / not-in-list → 405 |

## Requirements

- macOS: Homebrew `curl` (`brew install curl`) — system `/usr/bin/curl` is
  SIP-protected and won't load the shim.
- Any working `cc` (clang or gcc) — used to compile the v4-mapped probe.
- `python3` on PATH for the unit tests.
- Optional: internet for the auth-proxy test (skipped if `httpbin.org` is
  unreachable).
