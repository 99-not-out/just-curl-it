# Anthropic API

The Messages API authenticates with an `x-api-key` header, so the key goes
in as a `header_*` with a `_keychain` or `_cmd` source — not `authorization`.
The required `anthropic-version` header is injected too, so a curl is just
the body.

## routes.conf

```ini
llm.anthropic        ${JCI_HANDLERS}/run proxy
```

## backends.conf — API key

```ini
[llm.anthropic]
upstream                  = https://api.anthropic.com
header_x-api-key_keychain = anthropic-api-key      # security add-generic-password -a $USER -s anthropic-api-key -w 'sk-ant-...'
header_anthropic-version  = 2026-10-21
```

Or `header_x-api-key = ${ANTHROPIC_API_KEY}` from the environment, or
`header_x-api-key_cmd = pass show anthropic/api`.

## backends.conf — `ant auth login` (OAuth, no key on disk)

```ini
[llm.anthropic]
upstream                 = https://api.anthropic.com
authorization_cmd        = printf 'Bearer %s' "$(ant auth print-credentials --access-token)"
header_anthropic-beta    = oauth-2025-04-20
header_anthropic-version = 2026-10-21
```

## Try

```sh
curl -s http://llm.anthropic/v1/models | jq '.data[].id'

curl -s http://llm.anthropic/v1/messages -d '{
  "model": "claude-opus-5-5", "max_tokens": 1024,
  "messages": [{"role": "user", "content": "one line: why curl?"}]
}' | jq -r '.content[] | select(.type == "text") | .text'

# streaming (SSE) -- -N so curl doesn't buffer
curl -sN http://llm.anthropic/v1/messages -d '{
  "model": "claude-opus-5-5", "max_tokens": 1024, "stream": true,
  "messages": [{"role": "user", "content": "write a haiku about sockets"}]
}' | grep '^data:' | jq -r 'select(.type=="content_block_delta") | .delta.text // empty' 2>/dev/null | tr -d '\n'; echo
```

`claude-opus-5-5` is the current default model; `GET /v1/models` lists
what your key can use.

## Notes

- Everything in the shell that talks to `llm.anthropic` gets the key: an
  agent you start inside `jci use` never sees `ANTHROPIC_API_KEY`, only the
  hostname. That's the sandbox story applied to the model call itself.
- This recipe is the raw Messages API. For the curl-shaped version —
  `cat notes.md | curl --data-binary @- http://mylocal.llm/summarize`, a
  normalised `/chat`, a `max_tokens` cap — use the **`llm` handler** with
  `provider = anthropic` (`jci handler info llm`).
- Parse responses with `jq`, never `grep` — content is JSON with escapes.

*Not run live from this repo.*
