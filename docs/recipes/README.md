# Recipes

Services that are already HTTP need no handler of their own — the `proxy`,
`aws-sigv4` and (soon) `graphql` handlers cover them. What they need is the
right `backends.conf` block and three example curls. That's a recipe.

Each page is a complete env: copy the two config files into
`~/.just_curl_it/envs/<name>/`, run `jci use <name>`, curl.

```
~/.just_curl_it/envs/github/
    routes.conf      gh.api   GET   ${JCI_HANDLERS}/run proxy
    backends.conf    [gh.api] upstream = https://api.github.com ...
```

The `GET` in the route line is the point: the token in your keychain or
CLI session can read, and nothing in that shell — you, a script, an agent
— can write, whatever the token itself is allowed to do.

| recipe | handler | auth source |
|---|---|---|
| [GitHub](github.md) | proxy | `gh auth token` |
| [GCP](gcp.md) — any Google API, BigQuery raw, Vertex AI (Claude) | proxy | `gcloud auth print-access-token` |
| [Azure](azure.md) | proxy | `az account get-access-token` |
| [AWS Bedrock](aws-bedrock.md) — Claude and other models, SigV4 | aws-sigv4 | AWS credentials / profile / `aws configure export-credentials` |
| [Anthropic API](anthropic.md) | proxy | API key in the keychain, or `ant auth` OAuth |
| [OpenAI API](openai.md) | proxy | API key in the keychain |
| [Vault](vault.md) | proxy | `vault print token` |
| [Kubernetes](kubernetes.md) | proxy | `kubectl create token` or the kubeconfig |
| [Prometheus](prometheus.md) | proxy | none, or basic auth |
| [Linear](linear.md) (GraphQL) | proxy | API key in the keychain |
| [Slite](slite.md) | proxy | API key in the keychain |
| [Slack](slack.md) | proxy | bot/user token in the keychain |
| [Jira Cloud](jira.md) | proxy | email + API token, Basic |

## The three ways a secret gets in

```ini
authorization          = Bearer ${GITHUB_TOKEN}        # env var, expanded at request time
authorization_keychain = github-api-token              # macOS Keychain: security find-generic-password -s
authorization_cmd      = printf 'Bearer %s' "$(gh auth token)"   # any command printing the header value
```

The same three forms exist for any header: `header_x-api-key`,
`header_x-api-key_keychain`, `header_x-api-key_cmd`. A `_cmd` runs per
request, so short-lived CLI tokens (`gcloud`, `az`, `kubectl`, `ant`) are
always fresh and never written down.

## Verification status

Config shapes were checked against each API's documented auth; the
mechanics (`proxy` with `authorization_cmd`, `header_*`, mTLS,
`aws-sigv4` signing) are covered by the test suite. **GitHub** was run
live through the shim exactly as written (token from `gh`, `GET` → 200,
`POST` → 405 from the gate). Pages marked *not run live* were not
exercised against the real service from this repo — if one is wrong,
the fix is a config line; please send it.
