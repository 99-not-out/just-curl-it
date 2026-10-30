# hello

Echo the routing context as JSON. A bash handler with no dependencies —
the thing to point a route at first, to prove the shim is wired up before
adding real protocol handlers. Also the smallest possible example of what
a handler is: read the request on stdin, write a response on stdout.

## Routes

```
GET|POST|... /anything    {"handler","env","host","request","pid"}
```

Every method and path answers the same way; the request line is echoed in
`request`.

## backends.conf

None. This handler reads nothing from `backends.conf`.

## Notes

- Methods are not checked here. The shim enforces the route's allow-list
  before any handler is spawned (ADR 0004), so `hello` is gated exactly as
  well as a Python handler when its route says `GET`.
- The whole handler is `handler.sh`; read it to see the contract.
