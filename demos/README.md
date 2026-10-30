# jci demos

Optional add-on: a `demos` env that puts seven backend services behind
plain HTTP hostnames, plus a walkthrough script and client apps in Python
and Java that prove this works for any libc-resolving language, not just
curl. The handlers themselves live in the library (`jci handler ls`);
this directory is the backends and the examples of using them.

## What you get

| hostname           | handler     | backend          | typical port |
|---                 |---          |---               |---           |
| `mylocal.kafka`    | `kafka`     | Kafka            | 9092         |
| `mylocal.redis`    | `redis`     | Redis            | 6379         |
| `mylocal.postgres` | `postgres`  | PostgreSQL       | 5433 (host)  |
| `mylocal.mysql`    | `mysql`     | MySQL 8          | 3307 (host)  |
| `mylocal.clickhouse` | `clickhouse` | ClickHouse (HTTP) | 8123       |
| `mylocal.mqtt`     | `mqtt`      | Mosquitto        | 1883         |
| `mylocal.mongo`    | `mongo`     | MongoDB          | 27017        |
| `mylocal.rabbit`   | `rabbit`    | RabbitMQ + mgmt  | 5672 / 15672 |
| `mylocal.smtp`     | `smtp`      | SMTP (MailHog)   | 1025 / 8025  |
| `mylocal.grpc`     | `grpc`      | gRPC helloworld  | 50051        |
| `mylocal.shim`     | `shim-meta` | introspection    | (no backend) |
| `mylocal.hello`    | `hello`     | bash echo        | (no backend) |

Every handler answers `GET /_info` with the routes it serves; `jci-info`
inside the env prints them all.

## Setup

```sh
# from the repo root
make demos-install                     # make install + jci handler install kafka redis postgres mysql mongo rabbit grpc mqtt
cd demos && docker compose up -d       # bring up backends
jci use demos                          # activate the env
```

`demos-install` creates one venv per handler (network needed for pip).
`smtp`, `hello` and `shim-meta` are stdlib-only and need no install.

## Tour

```sh
demos/scripts/tour.sh                  # interactive walkthrough, inside `jci use demos`
```

## Demo apps

```sh
python3 demos/apps/python/demo.py      # urllib only — no client libs
java   demos/apps/java/Main.java       # java.net.http.HttpClient — no client libs
```

Both reach every backend with no SDK imports: each connect is intercepted
and routed through the handlers.

**macOS:** these client apps run *inside* the env, so they need a
non-SIP interpreter — Homebrew `python3` and `openjdk`, not the system
ones, which strip `DYLD_INSERT_LIBRARIES`. (Handlers are unaffected; the
shim is unset before they're exec'd.)

## Tests

```sh
cd demos && docker compose up -d
make test-handlers                     # from the repo root; sections skip for services that aren't up
```

## Files

```
demos/
    docker-compose.yml          # backends, all ports mapped to localhost
    grpcdemo/                   # the gRPC helloworld service (built into compose)
    env/                        # the demos jci env
        routes.conf             # hostname -> ${JCI_HANDLERS}/run <handler>
        backends.conf           # hostname -> service address
    apps/
        python/demo.py
        java/Main.java
    scripts/tour.sh
```
