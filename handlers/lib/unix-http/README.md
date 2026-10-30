# unix-http

Talk to an HTTP API that lives on a Unix socket — Docker, Podman, the
systemd socket APIs — as if it were a hostname. Stdlib only. The request
goes over the socket unchanged and the response streams back; with a
`GET`-only route you get a read-only view of the daemon.

```sh
curl http://mylocal.docker/containers/json | jq '.[].Names'
curl http://mylocal.docker/images/json | jq length
curl -X POST http://mylocal.docker/containers/abc123/stop     # if the route allows POST
```

## Routes

```
<any method> <any path>     forwarded as-is over the socket
GET /_info
```

Path, query string, body and headers pass through; hop-by-hop headers are
dropped and `Host: localhost` is set if the client sent none.

## backends.conf

```ini
[mylocal.docker]
socket  = /var/run/docker.sock       # required; ~ expanded
timeout = 5                          # connect timeout, seconds
```

Docker Desktop on macOS: `~/.docker/run/docker.sock`. Podman:
`$XDG_RUNTIME_DIR/podman/podman.sock`.

## Notes

- jci adds no privilege. If your user can't open the socket, the handler
  answers `502` with the errno — fix the group or the path.
- A socket path that doesn't exist is also a `502`, naming the path.
- The Docker API is versioned under `/v1.xx/...`; unversioned paths work
  against the daemon's default. Nothing here rewrites them.
- `curl --unix-socket` does this too, for one command. The point of a route
  is that *every* tool in the env, and every curl in a script, gets it.
