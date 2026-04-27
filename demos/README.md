# jci demos

Optional add-on: a `demos` env that turns 7 backend services into plain HTTP
endpoints, plus walkthrough scripts and demo apps in Python and Java to
prove this works for any libc-resolving language, not just curl.

## What you get

| hostname           | backend          | typical port |
|---                 |---               |---           |
| `mylocal.kafka`    | Kafka            | 9092         |
| `mylocal.redis`    | Redis            | 6379         |
| `mylocal.postgres` | PostgreSQL       | 5432         |
| `mylocal.mongo`    | MongoDB          | 27017        |
| `mylocal.rabbit`   | RabbitMQ + mgmt  | 5672 / 15672 |
| `mylocal.smtp`     | SMTP (MailHog)   | 1025 / 8025  |
| `mylocal.grpc`     | gRPC helloworld  | 50051        |
| `mylocal.shim`     | introspection    | (no backend) |
| `mylocal.hello`    | bash echo        | (no backend) |

Plus `GET /_info` on every endpoint that describes the routes it serves.

## Setup

```sh
# from the repo root
make demos-install                     # builds the shim, sets up venv, copies handlers
cd demos && docker compose up -d       # bring up backends
jci use demos                          # activate the env
```

`demos-install` uses Homebrew Python by default on macOS (since system
`/usr/bin/python3` is hardened-runtime and won't load the shim). Override
with `make demos-install PYTHON=/path/to/python3` if needed.

## Tour

```sh
demos/scripts/tour.sh                  # interactive walkthrough
```

## Demo apps

```sh
python3 demos/apps/python/demo.py      # urllib only — no client libs
java   demos/apps/java/Main.java       # java.net.http.HttpClient — no client libs
```

Both reach all 7 backends with no SDK imports — every connect is intercepted
and routed through the protocol handlers.

## Files

```
demos/
    docker-compose.yml          # backends, all ports mapped to localhost
    grpcdemo/                   # the gRPC helloworld service (built into compose)
    handlers/                   # protocol adapters (HTTP -> wire protocol)
        kafka.py redis_kv.py postgres.py mongo.py rabbit.py smtp.py
        grpc_reflect.py shim.py shim_meta.py
        run-handler             # bash wrapper: execs handler under managed venv
    env/                        # the demos jci env
        routes.conf             # hostname -> handler
        backends.conf           # hostname -> service address
    requirements.txt            # Python deps for the venv
    apps/
        python/demo.py
        java/Main.java
    scripts/tour.sh
```
