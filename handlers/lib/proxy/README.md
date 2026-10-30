# proxy

Forward plain HTTP to an authenticated HTTP(S) upstream. The token (or
client certificate) lives in your shell, keychain or secret store — not in
your history — and every tool in the env that hits the hostname gets it
injected: `curl http://gh.api/user`, `gh`, `git` over HTTP, Python
`requests`. Stdlib only; no install step.

## Routes

```
<any method> <any path>     forwarded as-is to <upstream><path>
```

The upstream's `Location` headers that point back at the upstream are
rewritten onto the intercepted hostname, so `curl -L` keeps going through
the proxy; relative and third-party redirects pass through untouched.
Hop-by-hop headers are stripped in both directions.

## backends.conf

```ini
[gh.api]
upstream = https://api.github.com          # required; http:// or https://

# Authorization -- first one set wins:
authorization          = Bearer ${GITHUB_TOKEN}   # literal, ${VAR} expanded
authorization_keychain = github-api-token          # macOS: security find-generic-password -s
authorization_cmd      = pass show github/api      # any command printing the header value

# Extra headers: any key starting header_
header_accept               = application/vnd.github+json
header_x-github-api-version = 2022-11-28

# mTLS, for upstreams that want a client cert (Teleport app access, corp
# gateways). A path, or a *_cmd printing a path or the PEM itself.
client_cert     = ${TSH_APP_CERT}
client_key      = ${TSH_APP_KEY}
client_cert_cmd = tsh apps config --format=cert
client_key_cmd  = tsh apps config --format=key
ca_cert         = /path/ca.pem             # replaces the system trust store

timeout = 5                                # connect timeout, seconds
```

## Notes

- Errors name the cause: cert without key, a cert path that no longer
  exists (an expired login session is the usual reason), an unexpanded
  `${VAR}`, an expired client cert, and a chain lacking SKI/AKI extensions
  (which Python 3.13+ rejects by default).
- PEM printed by a `*_cmd` goes to a `0600` temp file that is unlinked as
  soon as the TLS context has loaded it.
- Scope: once the route is active, *everything* in that shell that hits
  the hostname goes through here. That's the point, but know it.
- Not an HTTPS listener: the client speaks plain `http://` to the fake
  hostname; TLS is only on the upstream leg.
