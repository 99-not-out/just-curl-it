# llm

Talk to a model with curl, in shapes that fit a pipeline: text in, text
out; or a normalised chat body that's the same for Anthropic, OpenAI and
Bedrock. Stdlib only. The API key stays in your keychain or shell; a
`max_tokens` cap in the config is enforced on every call.

```sh
cat report.md | curl -s --data-binary @- http://mylocal.llm/summarize
git diff | curl -s --data-binary @- http://mylocal.llm/explain
curl -s --data-binary 'teh quick brwon fox' http://mylocal.llm/fix
curl -s 'http://mylocal.llm/translate?to=German' --data-binary 'good morning'
curl -sN http://mylocal.llm/chat/stream -d '{"messages":[{"role":"user","content":"why curl?"}]}'
```

## Routes

```
POST /chat                 {"messages":[{role,content}], "system"?, "model"?, "max_tokens"?}
                           -> {"text","provider","model","maxTokens","usage":{"input","output"},"stop"}
POST /chat/stream          same body; Server-Sent Events: data: {"text": delta} ... event: done {stop, usage}
POST /summarize            body = the text (any content-type, or {"text": ...} as JSON); reply is text/plain
POST /explain
POST /fix                  spelling, grammar, punctuation; returns only the corrected text
POST /translate?to=X       default English
POST /complete             the body is the prompt, verbatim
POST /<verb>               any `prompt_<verb>` in backends.conf
GET  /models               model ids the credentials can use
GET  /_info
```

Verb responses carry `X-LLM-Model`, `X-LLM-Usage-Input`, `X-LLM-Usage-Output`
and `X-LLM-Stop` headers; the body is just the answer, ready for the next
pipe. `?max_tokens=` and `?model=` work on verbs too (within the cap).

## backends.conf

```ini
[mylocal.llm]
provider        = anthropic                      # anthropic | openai | bedrock
model           = claude-opus-5-5
api_key_keychain = anthropic-api-key             # or api_key = ${ANTHROPIC_API_KEY} / api_key_cmd = ...
max_tokens      = 2048                           # hard cap; a request can only lower it
system          = Answer in plain prose, no preamble.
prompt_tldr     = Give a three-bullet TL;DR of the following, then stop:
read_timeout    = 300                            # seconds to wait for a generation
# anthropic_version = 2026-10-21
# allow_model_override = false                   # ignore "model" in requests

[mylocal.gpt]
provider        = openai
model           = gpt-4o-mini
api_key_keychain = openai-api-key
# organization  = org_...

[mylocal.bedrock]
provider        = bedrock                        # Converse API: Claude or any model Bedrock serves
model           = anthropic.claude-opus-5-5
region          = eu-west-1
profile         = default                        # or credentials_cmd / access_key+secret_key, as aws-sigv4
```

## Notes

- **The sandbox angle.** An agent started inside `jci use` sees a hostname,
  never a key, and every model call it makes passes through this handler:
  the cap applies, the provider is whatever the config says, and the
  request lands in the shell's audit trail like any other curl.
- `usage` is normalised to `{"input", "output"}` tokens across providers;
  `stop` is the provider's own stop reason. An Anthropic safety refusal
  comes back as `"stop": "refusal"` with `stopDetails` — check it before
  using `text`.
- Streaming: Anthropic and OpenAI stream real deltas. Bedrock's streaming
  API uses AWS event-stream binary framing, so `/chat/stream` on Bedrock
  delivers the whole answer as one `data:` event (the `done` event says so).
- Thinking, tools, images and structured output aren't exposed; this is
  the curl-shaped 80%. Use the provider's API through `proxy` (see
  `docs/recipes/anthropic.md`) for the rest.
- Not run against a live provider from this repo; the suite drives fake
  Anthropic, OpenAI and Bedrock endpoints, with the Bedrock one verifying
  the SigV4 signature.
