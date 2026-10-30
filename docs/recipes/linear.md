# Linear

Linear's API is GraphQL only: everything is `POST /graphql`, so a
`GET`-only route blocks it entirely. Until the `graphql` handler lands
(roadmap #4 — it exposes queries on `GET` and refuses mutations), allow
`POST` and rely on the token's own permissions.

## routes.conf

```ini
linear.api    POST    ${JCI_HANDLERS}/run proxy
```

## backends.conf

```ini
[linear.api]
upstream               = https://api.linear.app
authorization_keychain = linear-api-key       # personal API keys go in Authorization as-is, no "Bearer"
# or: authorization = ${LINEAR_API_KEY}
```

## Try

```sh
curl -s http://linear.api/graphql -d '{"query":"{ viewer { id name } }"}' | jq .data.viewer

curl -s http://linear.api/graphql -d '{"query":"{ issues(first: 10, filter: {state: {type: {eq: \"started\"}}}) { nodes { identifier title assignee { name } } } }"}' \
  | jq -r '.data.issues.nodes[] | "\(.identifier)  \(.title)  [\(.assignee.name // "-")]"'

# a saved query from a file, with variables
curl -s http://linear.api/graphql -d @- <<'EOF' | jq .
{"query":"query($team: String!) { team(id: $team) { issues(first: 5) { nodes { identifier title } } } }",
 "variables":{"team":"ENG"}}
EOF
```

## Notes

- Personal API keys are sent as the raw `Authorization` value; OAuth
  tokens use `Bearer `. The config above is for personal keys.
- Slite, GitHub and Shopify GraphQL work the same way; the `graphql`
  handler will give all of them `GET /gql?query=`, `/schema` and
  `/types/{T}` so an agent can discover the schema.

*Not run live from this repo.*
