# HashiCorp Vault

Vault wants `X-Vault-Token`; the `vault` CLI already has one.

## routes.conf

```ini
vault.api    GET    ${JCI_HANDLERS}/run proxy      # reads only; drop GET to write secrets
```

## backends.conf

```ini
[vault.api]
upstream                 = https://vault.example.com:8200
header_x-vault-token_cmd = vault print token
ca_cert                  = /etc/ssl/corp-ca.pem      # if Vault has a private CA
# header_x-vault-namespace = team-a                   # Enterprise namespaces
```

## Try

```sh
curl -s http://vault.api/v1/sys/health | jq .
curl -s http://vault.api/v1/secret/data/app/prod | jq .data.data        # KV v2
curl -s -X LIST http://vault.api/v1/secret/metadata/app | jq .data.keys   # LIST is a method -- blocked by GET-only
```

## Notes

- `vault print token` reads `VAULT_TOKEN` or `~/.vault-token`; it needs
  `vault login` to have happened in this user's session.
- Vault's `LIST` is a real HTTP method. A route with `GET,LIST` allows
  browsing without writing.
- Secrets read through this route land in whatever is reading them; the
  point of the `GET`-only route is that nothing in the shell can *change*
  Vault, not that the values are hidden.

*Not run live from this repo.*
