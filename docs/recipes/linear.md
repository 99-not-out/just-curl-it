# Linear

Linear's API is GraphQL only, so it goes through the `graphql` handler:
queries on `GET` (a `GET`-only route is read-only across the whole API),
mutations on `POST`, and the schema a curl away.

## routes.conf

```ini
linear.api    GET    ${JCI_HANDLERS}/run graphql      # drop GET to allow mutations
```

## backends.conf

```ini
[linear.api]
upstream               = https://api.linear.app/graphql
authorization_keychain = linear-api-key       # personal API keys go in Authorization as-is, no "Bearer"
# or: authorization = ${LINEAR_API_KEY}
```

## Try

```sh
curl -sG http://linear.api/gql --data-urlencode 'query={ viewer { id name } }' | jq .data.viewer

curl -sG http://linear.api/gql --data-urlencode 'query={ issues(first: 10, filter: {state: {type: {eq: "started"}}}) { nodes { identifier title assignee { name } } } }' \
  | jq -r '.data.issues.nodes[] | "\(.identifier)  \(.title)  [\(.assignee.name // "-")]"'

# variables and a named operation
curl -sG http://linear.api/gql \
  --data-urlencode 'query=query Team($key: String!) { team(id: $key) { issues(first: 5) { nodes { identifier title } } } }' \
  --data-urlencode 'vars={"key":"ENG"}' --data-urlencode 'op=Team' | jq .

# what's in the schema?
curl -s http://linear.api/schema | jq '.types[] | select(.kind == "OBJECT") | .name' | head -20
curl -s http://linear.api/types/Issue | jq '.fields[] | .name + ": " + .type'

# a mutation: 405 on the GET-only route; allow POST on a second hostname if you want it
curl -s -X POST http://linear.api/gql -d '{"query":"mutation { issueCreate(input: {teamId: \"...\", title: \"from curl\"}) { success } }"}'
```

## Notes

- Personal API keys are sent as the raw `Authorization` value; OAuth
  tokens use `Bearer `. The config above is for personal keys.
- Linear's own docs are the schema reference; `/types/{Name}` is quicker
  when you just need a field name.
- GitHub GraphQL (`https://api.github.com/graphql`), Shopify Admin and
  Hasura work the same way with their own `upstream` and auth.

*Not run live from this repo; the handler is covered end to end by the test suite against a fake GraphQL server.*
