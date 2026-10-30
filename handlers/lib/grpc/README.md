# grpc

Call gRPC methods with curl and JSON, no stubs and no `.proto` files — the
handler asks the server for its schema over reflection, builds the request
message from your JSON, and returns the reply as JSON. Needs
`jci handler install grpc`.

```sh
curl http://mylocal.grpc/_services
curl -X POST http://mylocal.grpc/helloworld.Greeter/SayHello -d '{"name":"ada"}'
```

## Routes

```
GET  /_services                   {"services": ["helloworld.Greeter", ...]}
POST /{pkg.Service}/{Method}      body = JSON request message -> JSON reply
GET  /_info
```

## backends.conf

```ini
[mylocal.grpc]
address = api.internal:443        # required; host:port
tls     = false                   # true -> TLS; system roots unless ca_cert is set
ca_cert = /path/ca.pem
timeout = 5                       # reflection and call deadline, seconds
```

## Notes

- **The server must enable reflection.** Most frameworks have a one-liner
  for it; without it you get `404 service not found via reflection`.
- Unary calls only. Streaming methods answer `501`.
- gRPC status codes come back as `502` with the code name and details in
  the body (`NOT_FOUND`, `PERMISSION_DENIED`, ...).
- JSON ↔ protobuf uses the canonical proto3 JSON mapping; field names in
  the reply are the proto names, not camelCase.
- No per-call metadata/auth headers yet. A `GET`-only route makes this
  handler read-only in the trivial sense — nothing but `/_services`.
