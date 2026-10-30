# ldap

Look people and groups up in LDAP or Active Directory with curl.
Read-only by construction — there is no route that writes — so the
hostname is safe to hand to anything. Needs `jci handler install ldap`.

```sh
curl -s http://corp.ldap/find/ada | jq '.entries[] | .attrs | {cn, mail, title}'
curl -sG http://corp.ldap/search --data-urlencode 'filter=(memberOf=cn=sre,ou=groups,dc=corp,dc=example)' --data-urlencode 'attrs=mail' | jq -r '.entries[].attrs.mail'
curl -s 'http://corp.ldap/entry/uid=ada,ou=people,dc=corp,dc=example' | jq .attrs
```

## Routes

```
GET /search?filter=(uid=x)&base=&scope=sub|one|base&attrs=cn,mail&limit=100
                           {"base","filter","count","entries":[{"dn","attrs":{...}}]}
GET /find/{term}           quick lookup: uid / sAMAccountName / mail prefix, cn / displayName substring
                           (term is escaped; a trailing * is allowed)
GET /entry/{dn}            one entry, all attributes; 404 if absent
GET /whoami                the bound identity (RFC 4532)
GET /_info
```

Multi-valued attributes come back as lists, single values as scalars,
binary values as `{"base64": ...}`.

## backends.conf

```ini
[corp.ldap]
url      = ldaps://ldap.corp.example:636        # ldap:// + tls = true for StartTLS
base     = dc=corp,dc=example                   # default search base; /find needs it
bind_dn  = cn=readonly,ou=svc,dc=corp,dc=example
password_keychain = corp-ldap-readonly          # or password = ${LDAP_PASSWORD} / password_cmd = ...
ca_cert  = /etc/ssl/corp-ca.pem                 # verify the server; without it ldaps is unverified
limit    = 100
timeout  = 5
```

Active Directory: `bind_dn` can be `user@corp.example`; `/find` already
tries `sAMAccountName`.

## Notes

- Everything is `GET` and nothing mutates (ADR 0005). Bind with a
  read-only service account anyway — least privilege is still the
  directory's job.
- `/find` escapes the term with RFC 4515 rules, so `*`, `(`, `)` and `\`
  in a name are matched literally; only a *trailing* `*` is kept as a
  wildcard.
- `ldaps://` without `ca_cert` does not verify the server certificate
  (CERT_NONE). Set `ca_cert` for anything you care about.
- Server-side size limits still apply; `limit` is the client's request,
  not an override.
