# Slite

REST with an `x-slite-api-key` header.

## routes.conf

```ini
slite.api    GET    ${JCI_HANDLERS}/run proxy      # read notes; drop GET to create/edit
```

## backends.conf

```ini
[slite.api]
upstream                      = https://api.slite.com
header_x-slite-api-key_keychain = slite-api-key
# or: header_x-slite-api-key = ${SLITE_API_KEY}
```

## Try

```sh
curl -s http://slite.api/v1/me | jq .
curl -sG http://slite.api/v1/search-notes --data-urlencode 'query=onboarding' | jq '.hits[] | .title'
curl -s http://slite.api/v1/notes/$NOTE_ID?format=md | jq -r .content
```

## Notes

- Slite's "Ask" endpoint and note creation are `POST`; keep the route
  `GET`-only for a read-only knowledge base an agent can search but not edit.
- Endpoint paths follow Slite's public API reference; check it for the
  current `v1` surface.

*Not run live from this repo.*
