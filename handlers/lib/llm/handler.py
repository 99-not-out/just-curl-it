#!/usr/bin/env python3
"""HTTP -> an LLM, in curl-native shapes. Stdlib only.

  POST /chat            {"messages":[{role,content}], "system"?, "model"?, "max_tokens"?}
                        -> {"text", "provider", "model", "maxTokens", "usage": {input, output}, "stop"}
  POST /chat/stream     same body; Server-Sent Events: data: {"text": delta} ..., event: done
  POST /summarize       text in, text out: body is the text, response is the answer (text/plain)
  POST /explain | /fix | /translate?to=X | /complete (raw prompt) | /<verb> for any prompt_<verb> in config
  GET  /models          model ids the key can use
  GET  /_info

Providers: anthropic (Messages API), openai (chat completions), bedrock
(Converse API, SigV4 via jci_aws -- model-agnostic, so Claude or anything
else Bedrock serves). The key never reaches the caller's environment: it
is resolved here from api_key / api_key_keychain / api_key_cmd (or AWS
credentials), and `max_tokens` in the config is a hard cap a request can
only lower. For an agent running inside `jci use`, that makes this the one
chokepoint every model call goes through.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, urlsplit

import jci
import jci_aws

ROUTES = [
    "GET  /_info",
    "GET  /models",
    "POST /chat            ({messages, system?, model?, max_tokens?} -> {text, usage, ...})",
    "POST /chat/stream     (same; SSE: data: {text} ..., event: done)",
    "POST /summarize | /explain | /fix | /translate?to=X | /complete    (text in, text out)",
    "POST /<verb>          (any prompt_<verb> key in backends.conf)",
]

ANTHROPIC_VERSION = "2026-10-21"
DEFAULT_MAX_TOKENS = 1024

VERBS = {
    "summarize": "Summarize the following text. Keep every fact that matters and nothing else.\n\n",
    "explain": "Explain the following clearly, for someone who has not seen it before.\n\n",
    "fix": "Correct spelling, grammar and punctuation in the following text. Return only the corrected text.\n\n",
    "translate": "Translate the following text into {to}. Return only the translation.\n\n",
    "complete": "",
}


# ---------------------------------------------------------------------------
# provider adapters: pure functions, unit-tested

def effective_max_tokens(cfg: dict, requested) -> int:
    cap = int(cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
    if requested is None:
        return cap
    return max(1, min(int(requested), cap))


def anthropic_body(model: str, messages: list, system, max_tokens: int, stream: bool) -> dict:
    b = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        b["system"] = system
    if stream:
        b["stream"] = True
    return b


def anthropic_parse(resp: dict) -> dict:
    usage = resp.get("usage") or {}
    out = {"text": "".join(c.get("text", "") for c in resp.get("content") or [] if c.get("type") == "text"),
           "model": resp.get("model"), "stop": resp.get("stop_reason"),
           "usage": {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)}}
    if resp.get("stop_details"):
        out["stopDetails"] = resp["stop_details"]
    return out


def anthropic_delta(ev: dict):
    if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
        return ev["delta"].get("text", "")
    return None


def anthropic_done(ev: dict):
    if ev.get("type") == "message_delta":
        return {"stop": (ev.get("delta") or {}).get("stop_reason"),
                "usage": {"output": (ev.get("usage") or {}).get("output_tokens", 0)}}
    return None


def openai_body(model: str, messages: list, system, max_tokens: int, stream: bool) -> dict:
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    b = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens}
    if stream:
        b["stream"] = True
        b["stream_options"] = {"include_usage": True}
    return b


def openai_parse(resp: dict) -> dict:
    ch = (resp.get("choices") or [{}])[0]
    usage = resp.get("usage") or {}
    return {"text": (ch.get("message") or {}).get("content") or "", "model": resp.get("model"),
            "stop": ch.get("finish_reason"),
            "usage": {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}}


def openai_delta(ev: dict):
    ch = (ev.get("choices") or [{}])[0]
    return (ch.get("delta") or {}).get("content") or None


def bedrock_body(messages: list, system, max_tokens: int) -> dict:
    b = {"messages": [{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
         "inferenceConfig": {"maxTokens": max_tokens}}
    if system:
        b["system"] = [{"text": system}]
    return b


def bedrock_parse(resp: dict) -> dict:
    content = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    usage = resp.get("usage") or {}
    return {"text": "".join(c.get("text", "") for c in content), "stop": resp.get("stopReason"),
            "usage": {"input": usage.get("inputTokens", 0), "output": usage.get("outputTokens", 0)}}


def verb_messages(verb: str, text: str, qs: dict, cfg: dict):
    """The user message for a text-in/text-out verb, or None if unknown."""
    custom = cfg.get(f"prompt_{verb}")
    if custom is not None:
        instr = custom.rstrip() + "\n\n"
    elif verb in VERBS:
        instr = VERBS[verb]
        if verb == "translate":
            instr = instr.format(to=(qs.get("to") or ["English"])[0])
    else:
        return None
    return [{"role": "user", "content": instr + text}]


# ---------------------------------------------------------------------------
# transport

class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _provider(cfg: dict) -> str:
    p = cfg.get("provider", "").strip().lower()
    if p not in ("anthropic", "openai", "bedrock"):
        raise jci.BackendConfigError("provider must be anthropic, openai or bedrock")
    return p


def _model(cfg: dict, requested) -> str:
    if requested and not _truthy(cfg.get("allow_model_override", "true")):
        requested = None
    m = (requested or cfg.get("model", "")).strip()
    if not m:
        raise jci.BackendConfigError("missing 'model' in backends.conf")
    return m


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _api_key(cfg: dict) -> str:
    key = jci.resolve_secret(cfg, "api_key")
    if not key:
        raise jci.BackendConfigError("no api_key: set api_key, api_key_keychain or api_key_cmd")
    return key.strip()


def _open(cfg: dict, method: str, path: str, body: bytes | None, headers: dict, service: str | None = None):
    """Connect to the provider and return (conn, resp). Signs for bedrock."""
    p = _provider(cfg)
    default = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}.get(p)
    if p == "bedrock":
        region, _ = jci_aws.region_and_service(cfg, service or "bedrock-runtime")
        host = "bedrock" if service == "bedrock" else "bedrock-runtime"
        default = f"https://{host}.{region}.amazonaws.com"
    cfg = {**cfg, "upstream": cfg.get("upstream") or default,
           "timeout": cfg.get("read_timeout", "300")}   # generations take a while
    u = jci.upstream_target(cfg)
    default_port = 443 if u.scheme == "https" else 80
    authority = (u.hostname or "") + (f":{u.port}" if u.port and u.port != default_port else "")
    full = (u.path.rstrip("/") or "") + path
    headers = {"host": authority, "accept": "application/json", **headers}
    if p == "bedrock":
        headers.update(jci_aws.sign_request(cfg, method, full, "", headers, body or b"",
                                            default_service=service or "bedrock"))
    conn = jci.connect_upstream(cfg, u)
    conn.request(method, full, body=body, headers=headers)
    return conn, conn.getresponse()


def _auth_headers(cfg: dict, p: str) -> dict:
    if p == "anthropic":
        return {"x-api-key": _api_key(cfg), "anthropic-version": cfg.get("anthropic_version", ANTHROPIC_VERSION)}
    if p == "openai":
        h = {"authorization": f"Bearer {_api_key(cfg)}"}
        if cfg.get("organization"):
            h["openai-organization"] = cfg["organization"]
        return h
    return {}


def _raise_for_status(resp, raw: bytes) -> None:
    if resp.status < 400:
        return
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = {}
    err = doc.get("error") if isinstance(doc.get("error"), dict) else {}
    msg = err.get("message") or doc.get("message") or doc.get("Message") or raw[:300].decode("utf-8", "replace")
    raise ProviderError(resp.status, msg)


def _report(e: ProviderError, p: str) -> None:
    if e.status in (401, 403):
        return jci.send_error(502, f"{p} refused the credentials: {e}")
    if e.status == 429:
        return jci.send_error(503, f"{p} rate limit: {e}", hint="retry after a moment")
    if e.status == 400 or e.status == 404 or e.status == 422:
        return jci.send_error(400, f"{p}: {e}")
    jci.send_error(502, f"{p} HTTP {e.status}: {e}")


def complete(cfg: dict, messages: list, system, max_tokens: int, model: str) -> dict:
    p = _provider(cfg)
    if p == "anthropic":
        body = anthropic_body(model, messages, system, max_tokens, False)
        conn, resp = _open(cfg, "POST", "/v1/messages", json.dumps(body).encode(),
                           {"content-type": "application/json", **_auth_headers(cfg, p)})
        parse = anthropic_parse
    elif p == "openai":
        body = openai_body(model, messages, system, max_tokens, False)
        conn, resp = _open(cfg, "POST", "/v1/chat/completions", json.dumps(body).encode(),
                           {"content-type": "application/json", **_auth_headers(cfg, p)})
        parse = openai_parse
    else:
        body = bedrock_body(messages, system, max_tokens)
        conn, resp = _open(cfg, "POST", f"/model/{quote(model, safe='')}/converse", json.dumps(body).encode(),
                           {"content-type": "application/json"})
        parse = bedrock_parse
    try:
        raw = resp.read()
    finally:
        conn.close()
    _raise_for_status(resp, raw)
    out = parse(json.loads(raw))
    out.update({"provider": p, "model": out.get("model") or model, "maxTokens": max_tokens})
    return out


def _iter_sse(resp):
    """Yield (event, data) pairs from an SSE response."""
    event, data = None, []
    for line in resp:
        line = line.rstrip(b"\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(b"event:"):
            event = line[6:].strip().decode()
        elif line.startswith(b"data:"):
            data.append(line[5:].strip().decode("utf-8", "replace"))


def stream(cfg: dict, messages: list, system, max_tokens: int, model: str) -> None:
    p = _provider(cfg)
    if p == "bedrock":
        # Converse streaming is AWS event-stream binary framing; one chunk for now.
        out = complete(cfg, messages, system, max_tokens, model)
        jci.send_sse_headers()
        jci.sse_event({"text": out["text"]})
        jci.sse_event({"stop": out.get("stop"), "usage": out.get("usage"), "provider": p,
                       "model": out["model"], "note": "bedrock streaming is delivered as one chunk"}, event="done")
        return
    if p == "anthropic":
        body = anthropic_body(model, messages, system, max_tokens, True)
        conn, resp = _open(cfg, "POST", "/v1/messages", json.dumps(body).encode(),
                           {"content-type": "application/json", **_auth_headers(cfg, p)})
    else:
        body = openai_body(model, messages, system, max_tokens, True)
        conn, resp = _open(cfg, "POST", "/v1/chat/completions", json.dumps(body).encode(),
                           {"content-type": "application/json", **_auth_headers(cfg, p)})
    try:
        if resp.status >= 400:
            _raise_for_status(resp, resp.read())
        jci.send_sse_headers()
        done = {"stop": None, "usage": {}}
        for _ev, data in _iter_sse(resp):
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if p == "anthropic":
                delta = anthropic_delta(obj)
                fin = anthropic_done(obj)
                if obj.get("type") == "message_start":
                    done["usage"]["input"] = ((obj.get("message") or {}).get("usage") or {}).get("input_tokens", 0)
            else:
                delta = openai_delta(obj)
                ch = (obj.get("choices") or [{}])[0]
                fin = None
                if ch.get("finish_reason"):
                    done["stop"] = ch["finish_reason"]
                if obj.get("usage"):
                    done["usage"] = {"input": obj["usage"].get("prompt_tokens", 0),
                                     "output": obj["usage"].get("completion_tokens", 0)}
            if delta:
                jci.sse_event({"text": delta})
            if fin:
                done["stop"] = fin["stop"]
                done["usage"].update(fin["usage"])
        jci.sse_event({**done, "provider": p, "model": model}, event="done")
    except (BrokenPipeError, OSError):
        pass
    finally:
        conn.close()


def list_models(cfg: dict) -> list[str]:
    p = _provider(cfg)
    if p == "bedrock":
        conn, resp = _open(cfg, "GET", "/foundation-models", None, {}, service="bedrock")
        key, field = "modelSummaries", "modelId"
    else:
        conn, resp = _open(cfg, "GET", "/v1/models", None, _auth_headers(cfg, p))
        key, field = "data", "id"
    try:
        raw = resp.read()
    finally:
        conn.close()
    _raise_for_status(resp, raw)
    return sorted(m.get(field, "") for m in (json.loads(raw).get(key) or []))


# ---------------------------------------------------------------------------

def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("llm", ROUTES)

    cfg = jci.load_backend()
    provider = _provider(cfg)
    try:
        if method == "GET" and p == "/models":
            return jci.send_json(200, {"provider": provider, "models": list_models(cfg)})

        if method == "POST" and p in ("/chat", "/chat/stream"):
            try:
                data = json.loads(body.decode("utf-8")) if body.strip() else {}
            except ValueError as e:
                return jci.send_error(400, f"bad JSON body: {e}")
            msgs = data.get("messages") or []
            if not isinstance(msgs, list) or not msgs or not all(
                    isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
                    for m in msgs):
                return jci.send_error(400, 'body needs "messages": [{"role": "user"|"assistant", "content": "..."}]')
            system = data.get("system", cfg.get("system"))
            max_tokens = effective_max_tokens(cfg, data.get("max_tokens"))
            model = _model(cfg, data.get("model"))
            if p == "/chat/stream":
                return stream(cfg, msgs, system, max_tokens, model)
            return jci.send_json(200, complete(cfg, msgs, system, max_tokens, model))

        if method == "POST" and p.count("/") == 1 and p != "/":
            verb = p[1:]
            if "json" in headers.get("content-type", "").lower():
                try:
                    text = json.loads(body.decode("utf-8")).get("text", "")
                except (ValueError, AttributeError):
                    return jci.send_error(400, 'JSON body needs {"text": "..."}')
            else:
                text = body.decode("utf-8", "replace")
            msgs = verb_messages(verb, text, qs, cfg)
            if msgs is None:
                return jci.send_error(404, f"no verb {verb!r}",
                                      verbs=sorted(set(VERBS) | {k[len("prompt_"):] for k in cfg if k.startswith("prompt_")}))
            if not text.strip():
                return jci.send_error(400, "empty body; send the text to work on")
            out = complete(cfg, msgs, cfg.get("system"), effective_max_tokens(cfg, qs.get("max_tokens", [None])[0]),
                           _model(cfg, qs.get("model", [None])[0]))
            jci._write_head(200, [("Content-Type", "text/plain; charset=utf-8"),
                                  ("Content-Length", str(len(out["text"].encode("utf-8")))),
                                  ("X-LLM-Provider", provider), ("X-LLM-Model", str(out["model"])),
                                  ("X-LLM-Usage-Input", str(out["usage"].get("input", 0))),
                                  ("X-LLM-Usage-Output", str(out["usage"].get("output", 0))),
                                  ("X-LLM-Stop", str(out.get("stop")))])
            jci._out().write(out["text"].encode("utf-8"))
            jci._out().flush()
            return
    except ProviderError as e:
        return _report(e, provider)
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
