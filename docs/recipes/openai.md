# OpenAI API

Plain bearer auth; the proxy's native case.

## routes.conf

```ini
llm.openai        ${JCI_HANDLERS}/run proxy
```

## backends.conf

```ini
[llm.openai]
upstream               = https://api.openai.com
authorization_keychain = openai-api-key       # value stored as 'Bearer sk-...'
# or: authorization = Bearer ${OPENAI_API_KEY}
# header_openai-organization = org_...        # optional
```

## Try

```sh
curl -s http://llm.openai/v1/models | jq '.data[].id' | head
curl -s http://llm.openai/v1/chat/completions -d '{
  "model": "gpt-4o-mini",
  "messages": [{"role": "user", "content": "one line: why curl?"}]
}' | jq -r '.choices[0].message.content'
```

## Notes

- For text-in/text-out verbs and a normalised `/chat` with a token cap,
  use the **`llm` handler** with `provider = openai`; this recipe is the
  raw API.
- Azure OpenAI uses `api-key` as the header and a per-deployment path;
  `header_api-key_keychain = azure-openai-key` and
  `upstream = https://<resource>.openai.azure.com` handle it.

*Not run live from this repo.*
