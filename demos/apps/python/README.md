# Python demo app

Single Python process that drives every backend in the `demos` env via
`urllib` — no client libraries, no SDKs. The shim intercepts each
`getaddrinfo`+`connect` and routes it to a protocol handler.

## Run

```sh
# 1) backends running, jci installed
docker compose -f ../../docker-compose.yml up -d
make demos-install        # from repo root

# 2) activate, then run
jci use demos
python3 demo.py
```

## macOS note

System `/usr/bin/python3` is hardened-runtime, so `DYLD_INSERT_LIBRARIES`
gets stripped and the shim won't load. Use Homebrew Python:

```sh
brew install python
which python3                        # /opt/homebrew/bin/python3
```
