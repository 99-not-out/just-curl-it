# 0001. Route on sentinel loopback IPs, not ports

- Status: Accepted
- Date: 2026-10-30 (records a decision made at project start)
- Governs: `lib/intercept.c` (`do_getaddrinfo`, `do_connect`, `load_config`)

## Context

The shim has to carry "which route is this" from `getaddrinfo()` to
`connect()`, which are separate libc calls with nothing in common but what
the client chooses to pass between them. The obvious encoding is the port:
return `127.0.0.1:<route-specific port>` from `getaddrinfo` and dispatch on
it in `connect`.

curl breaks that. It trusts the IP from `getaddrinfo` but substitutes the
URL's port at connect time, so every intercepted request arrives on port 80
and the route is lost. Other clients behave the same way to varying
degrees. The IP, by contrast, survives unchanged in every client we tested
(curl, Python urllib, Java HttpClient including its v4-mapped IPv6 path).

Binding real loopback aliases (`ifconfig lo0 alias 127.0.0.X`) would need
root on macOS and leaves state behind, both against the project's grain.

## Decision

Each route gets a sentinel address `127.0.0.<2+index>`. `getaddrinfo` for a
configured host returns that address with the caller's port untouched.
`connect()` to a sentinel address never reaches the kernel: the interposer
binds an ephemeral real loopback socket, forks the handler onto it, and
redirects the connect there. `127.0.0.1` is skipped so real local services
are never shadowed. `MAX_ROUTES` is 64.

## Consequences

- Works on macOS and Linux with no root, no aliases, no `/etc/hosts`.
- Survives every client that preserves the resolved IP, which is all of
  them we know of; the port is irrelevant, so `http://host:9000/` and
  `http://host/` hit the same handler.
- Only `getaddrinfo` is hooked; `gethostbyname` callers bypass the shim.
- Sentinel addresses are a real range. A client that resolves some other
  name to `127.0.0.2`–`127.0.0.65` (systemd-resolved lives at `127.0.0.53`)
  would be intercepted if a route of that index exists. `do_connect` should
  restrict itself to `SOCK_STREAM` to keep UDP out of the blast radius.
- Hostname matching is exact and case-insensitive; no wildcards yet.
