# just curl it

> Talk to Kafka, Postgres, Redis, Mongo, RabbitMQ, gRPC and SMTP with plain `curl`.
> No client library. No SDK. No server. **Just curl it.**

An `LD_PRELOAD` shim hijacks HTTP calls to a set of fake hostnames and pipes them
to small handler processes that speak each backend's real wire protocol. From
inside the container, every service looks like a plain HTTP endpoint.

## Start

```sh
docker compose up -d
docker compose run --rm shell
```

First run builds the shim image and pulls the backends (~1.5 GB).

Full guided tour once inside the shell:

```sh
demo
```

## Works with

Not just `curl` — anything in the container that resolves a name and opens a
socket through glibc, which covers most of the Linux ecosystem:

- **HTTP CLIs:** curl, wget, httpie, xh, hurl
- **Python:** `requests`, `urllib`, `httpx`, `aiohttp`
- **JVM:** `java.net.HttpClient`, OkHttp, Apache HttpClient, Ktor (Java/Kotlin/Scala/Clojure)
- **Node.js:** `fetch`, `http`, axios, undici
- **Ruby:** `Net::HTTP`, Faraday, HTTParty
- **PHP:** `file_get_contents`, Guzzle, php-curl
- **Perl:** LWP, `HTTP::Tiny`
- **Rust:** reqwest, hyper, ureq *(glibc target)*
- **C/C++:** libcurl — anything calling `getaddrinfo` + `connect`
- **Erlang / Elixir:** `:httpc`, HTTPoison, Finch

## Doesn't work

- **Go** — bypasses libc for both DNS (pure-Go resolver) and the `connect(2)`
  syscall, so `LD_PRELOAD` has nothing to hook.
- **musl-linked binaries** (Alpine, distroless-static) — the shim is a
  glibc-built `.so` and won't load in a musl process.
- **Statically-linked binaries** — nothing to preload into.
- **HTTPS** — handlers speak plain HTTP; no TLS termination.
- **macOS / Windows** — `LD_PRELOAD` is Linux-only.

## License

MIT
