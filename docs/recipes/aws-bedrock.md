# AWS Bedrock (Claude and other models)

Bedrock wants SigV4, which is what the `aws-sigv4` handler does. Set
`service = bedrock` and curl the runtime API.

## routes.conf

```ini
aws.bedrock        ${JCI_HANDLERS}/run aws-sigv4     # invoking a model is POST
```

## backends.conf

```ini
[aws.bedrock]
upstream = https://bedrock-runtime.eu-west-1.amazonaws.com
region   = eu-west-1
service  = bedrock
profile  = default                                        # ~/.aws/credentials
# or: credentials_cmd = aws configure export-credentials --format env   (SSO / assumed roles)
# or: access_key = ${AWS_ACCESS_KEY_ID} / secret_key = ${AWS_SECRET_ACCESS_KEY} / session_token = ${AWS_SESSION_TOKEN}
```

## Try

```sh
# Claude via InvokeModel: the body is the Messages API with anthropic_version set
curl -s -X POST http://aws.bedrock/model/anthropic.claude-opus-5-5/invoke \
     -H 'content-type: application/json' \
     -d '{"anthropic_version":"bedrock-2023-05-31","max_tokens":1024,
          "messages":[{"role":"user","content":"one line: why curl?"}]}' | jq -r '.content[0].text'

# Streaming: the response is AWS event-stream framing, not SSE -- pipe through `strings` to eyeball it
curl -sN -X POST http://aws.bedrock/model/anthropic.claude-opus-5-5/invoke-with-response-stream \
     -d '{"anthropic_version":"bedrock-2023-05-31","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}' | strings | grep -o '"text":"[^"]*"'

# The Converse API is model-agnostic
curl -s -X POST http://aws.bedrock/model/anthropic.claude-opus-5-5/converse \
     -d '{"messages":[{"role":"user","content":[{"text":"hi"}]}]}' | jq -r '.output.message.content[0].text'
```

## Notes

- Model IDs on Bedrock carry the `anthropic.` prefix; the exact IDs
  available in your region are in the Bedrock console or
  `aws bedrock list-foundation-models`. Some regions need an inference
  profile ID (`eu.anthropic....`) instead.
- Listing models is a different service and host:
  `bedrock.<region>.amazonaws.com` with `service = bedrock` — give it its
  own route if you want it.
- The `llm` handler on the roadmap normalises Anthropic / OpenAI / Bedrock
  behind one text-in, text-out shape; this recipe is the raw API.

*Not run live from this repo; the SigV4 signer is covered by the AWS test vectors.*
