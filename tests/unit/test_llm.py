"""The llm handler's provider adapters: request bodies, response parsing and
SSE delta extraction for Anthropic, OpenAI and Bedrock (Converse).
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "llm", os.path.join(REPO, "handlers", "lib", "llm", "handler.py"))
llm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm)

MSGS = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"}]


class Anthropic(unittest.TestCase):
    def test_body(self):
        b = llm.anthropic_body("claude-opus-5-5", MSGS, "be brief", 256, stream=False)
        self.assertEqual(b["model"], "claude-opus-5-5")
        self.assertEqual(b["max_tokens"], 256)
        self.assertEqual(b["system"], "be brief")
        self.assertEqual(b["messages"], MSGS)
        self.assertNotIn("stream", b)
        self.assertTrue(llm.anthropic_body("m", MSGS, None, 1, stream=True)["stream"])

    def test_parse(self):
        resp = {"content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
                "model": "claude-opus-5-5", "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2}}
        out = llm.anthropic_parse(resp)
        self.assertEqual(out["text"], "AB")
        self.assertEqual(out["usage"], {"input": 10, "output": 2})
        self.assertEqual(out["stop"], "end_turn")

    def test_refusal_is_surfaced(self):
        resp = {"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}, "usage": {}}
        out = llm.anthropic_parse(resp)
        self.assertEqual(out["stop"], "refusal")
        self.assertEqual(out["stopDetails"], {"category": "cyber"})

    def test_sse_delta(self):
        ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}
        self.assertEqual(llm.anthropic_delta(ev), "Hel")
        self.assertIsNone(llm.anthropic_delta({"type": "message_start"}))
        done = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}
        self.assertEqual(llm.anthropic_done(done), {"stop": "end_turn", "usage": {"output": 5}})


class OpenAI(unittest.TestCase):
    def test_body(self):
        b = llm.openai_body("gpt-x", MSGS, "be brief", 256, stream=False)
        self.assertEqual(b["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(b["messages"][1:], MSGS)
        self.assertEqual(b["max_completion_tokens"], 256)
        self.assertTrue(llm.openai_body("m", MSGS, None, 1, stream=True)["stream"])

    def test_parse(self):
        resp = {"choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}], "model": "gpt-x",
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
        out = llm.openai_parse(resp)
        self.assertEqual(out["text"], "yo")
        self.assertEqual(out["usage"], {"input": 3, "output": 1})
        self.assertEqual(out["stop"], "stop")

    def test_sse_delta(self):
        self.assertEqual(llm.openai_delta({"choices": [{"delta": {"content": "Hi"}}]}), "Hi")
        self.assertIsNone(llm.openai_delta({"choices": [{"delta": {}}]}))


class Bedrock(unittest.TestCase):
    def test_converse_body(self):
        b = llm.bedrock_body(MSGS, "be brief", 256)
        self.assertEqual(b["messages"][0], {"role": "user", "content": [{"text": "hi"}]})
        self.assertEqual(b["system"], [{"text": "be brief"}])
        self.assertEqual(b["inferenceConfig"], {"maxTokens": 256})

    def test_parse(self):
        resp = {"output": {"message": {"role": "assistant", "content": [{"text": "br"}]}},
                "stopReason": "end_turn", "usage": {"inputTokens": 4, "outputTokens": 1}}
        out = llm.bedrock_parse(resp)
        self.assertEqual(out["text"], "br")
        self.assertEqual(out["usage"], {"input": 4, "output": 1})


class Verbs(unittest.TestCase):
    def test_builtin_verb_prompt(self):
        msgs = llm.verb_messages("summarize", "long text", {}, {})
        self.assertEqual(msgs[0]["role"], "user")
        self.assertIn("long text", msgs[0]["content"])
        self.assertIn("ummar", msgs[0]["content"])

    def test_translate_uses_to(self):
        msgs = llm.verb_messages("translate", "bonjour", {"to": ["German"]}, {})
        self.assertIn("German", msgs[0]["content"])

    def test_custom_verb_from_config(self):
        msgs = llm.verb_messages("tldr", "stuff", {}, {"prompt_tldr": "Three bullets, then stop:"})
        self.assertTrue(msgs[0]["content"].startswith("Three bullets, then stop:"))

    def test_complete_is_raw(self):
        self.assertEqual(llm.verb_messages("complete", "raw prompt", {}, {})[0]["content"], "raw prompt")

    def test_unknown_verb(self):
        self.assertIsNone(llm.verb_messages("nope", "x", {}, {}))


class Cap(unittest.TestCase):
    def test_request_can_only_lower_the_cap(self):
        self.assertEqual(llm.effective_max_tokens({"max_tokens": "512"}, None), 512)
        self.assertEqual(llm.effective_max_tokens({"max_tokens": "512"}, 100), 100)
        self.assertEqual(llm.effective_max_tokens({"max_tokens": "512"}, 99999), 512)
        self.assertEqual(llm.effective_max_tokens({}, None), 1024)


if __name__ == "__main__":
    unittest.main()
