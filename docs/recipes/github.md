# GitHub API

`gh` already holds a token; let the proxy borrow it.

## routes.conf

```ini
gh.api    GET    ${JCI_HANDLERS}/run proxy        # drop GET for write access
```

## backends.conf

```ini
[gh.api]
upstream                    = https://api.github.com
authorization_cmd           = printf 'Bearer %s' "$(gh auth token)"
header_accept               = application/vnd.github+json
header_x-github-api-version = 2022-11-28
```

Without `gh`: `authorization = Bearer ${GITHUB_TOKEN}`, or
`authorization_keychain = github-api-token` after
`security add-generic-password -a $USER -s github-api-token -w 'Bearer ghp_...'`.

## Try

```sh
curl -s http://gh.api/user | jq .login
curl -s 'http://gh.api/repos/99-not-out/just-curl-it/pulls?state=open' | jq '.[].title'
curl -s http://gh.api/repos/99-not-out/just-curl-it/issues -d '{"title":"from curl"}'   # 405 on the GET-only route
```

`curl -L` follows GitHub's redirects (e.g. renamed repos) through the proxy;
`Location` headers pointing at `api.github.com` are rewritten onto `gh.api`.

## Notes

- `gh auth token` prints the token for the current `gh` login; with
  multiple accounts, `gh auth token -u <user>`.
- GitHub's GraphQL endpoint is `POST /graphql` on the same host; it works
  through this route if you allow `POST` (every GraphQL call is a POST, so
  `GET`-only blocks it entirely — the `graphql` handler fixes that).
- Rate-limit headers come back unchanged: `curl -si ... | grep -i x-ratelimit`.

*Run live through the shim as written: `GET /user` → 200, `POST` → 405 from the gate.*
