# Java demo app

Single Java process that drives every backend via `java.net.http.HttpClient`.
No client libraries — every backend is reached over plain HTTP because the
shim intercepts the JVM's libc calls.

## Run

```sh
# 1) backends running, jci installed
docker compose -f ../../docker-compose.yml up -d
make demos-install     # from repo root

# 2) activate, then run via single-file source-launch (Java 11+)
jci use demos
java Main.java
```

## macOS note

System `/usr/bin/java` (if present) is hardened-runtime;
`DYLD_INSERT_LIBRARIES` gets stripped. Install via Homebrew, which is
keg-only:

```sh
brew install openjdk
/opt/homebrew/opt/openjdk/bin/java Main.java
```

Or symlink/PATH it however you prefer.
