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

## License

MIT
