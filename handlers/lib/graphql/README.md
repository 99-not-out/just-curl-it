# graphql

Any GraphQL API — Linear, GitHub, Shopify, Hasura, your own — with
queries on `GET`, schema discovery for humans and agents, and the proxy's
auth. Stdlib only, nothing to install.

```sh
curl -sG http://linear.api/gql --data-urlencode 'query={ viewer { name } }' | jq .data
curl -s http://linear.api/schema | jq '.types[].name' | head
curl -s http://linear.api/types/Issue | jq '.fields[] | .name + ": " + .type'
```

## Routes

```
GET  /gql?query=...&vars={json}&op=Name    run a query; mutation/subscription documents are 405
POST /gql                                  {"query","variables","operationName"} JSON, or a raw
                                           GraphQL document (any other content-type); mutations ok
GET  /schema                               {"queryType","mutationType","subscriptionType","types":[{name,kind,description}]}
GET  /types/{Name}                         {"fields":[{name,type,args}], "inputFields", "enumValues", "interfaces", ...}
GET  /_info
```

The upstream's response passes through unchanged (`data`, `errors`,
`extensions`, status code). GraphQL reports errors with HTTP 200, so check
`.errors`, not the status.

## backends.conf

```ini
[linear.api]
upstream               = https://api.linear.app/graphql     # the full endpoint URL
authorization_keychain = linear-api-key                     # or authorization = Bearer ${TOKEN}
# header_*, header_*_cmd, client_cert, ca_cert, timeout: same as the proxy handler
```

## Notes

- **`GET /gql` runs queries only.** The handler reads the document's own
  top-level operation keyword (skipping comments, strings and fragments)
  and answers `405` for `mutation` or `subscription`, whatever the route
  allows — GET doesn't mutate (ADR 0005). This is the protocol's
  declaration, not a guess about what the query does; a `query` that
  mutates server-side is the server's bug, not something any client can
  see. A document with several operations needs `?op=` on GET.
- A `GET`-only route plus this handler is "read anything, change nothing"
  for the whole API. Put mutations behind a second hostname with `POST`.
- `/schema` and `/types/{T}` use standard introspection; APIs that
  disable it return a `502` with the server's message.
- `vars` and `op` map to `variables` and `operationName`.
