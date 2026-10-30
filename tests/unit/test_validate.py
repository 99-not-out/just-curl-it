"""AsyncAPI payload validation for stream handlers: jci.payload_error()
matches the channel (templates included) and validates the body against
the message schema. Needs jsonschema; skipped without it.
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import json
import os
import tempfile
import unittest

import jci

try:
    import jsonschema  # noqa: F401
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False

SPEC = {
    "asyncapi": "2.6.0", "info": {"title": "t", "version": "1"},
    "channels": {
        "home/{room}/temperature": {"publish": {"message": {"name": "Reading", "payload": {
            "type": "object", "required": ["celsius"],
            "properties": {"celsius": {"type": "number"}}}}}},
        "home/alerts": {"publish": {"message": {"payload": {"type": "string"}}}},
    },
}


class ChannelMatch(unittest.TestCase):
    def test_template_matches_one_segment(self):
        self.assertEqual(jci.match_channel(SPEC, "home/kitchen/temperature"), "home/{room}/temperature")
        self.assertIsNone(jci.match_channel(SPEC, "home/kitchen/extra/temperature"))

    def test_exact_match(self):
        self.assertEqual(jci.match_channel(SPEC, "home/alerts"), "home/alerts")

    def test_unknown(self):
        self.assertIsNone(jci.match_channel(SPEC, "jci/test/x"))


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed in this interpreter")
class PayloadError(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(SPEC, f)
        self.addCleanup(os.unlink, self.path)
        self.cfg = {"asyncapi": self.path, "validate": "true"}

    def test_valid_object(self):
        self.assertIsNone(jci.payload_error(self.cfg, "home/kitchen/temperature", b'{"celsius": 21.5}'))

    def test_invalid_object_names_the_problem(self):
        err = jci.payload_error(self.cfg, "home/kitchen/temperature", b'{"celsius": "warm"}')
        self.assertIn("celsius", err)
        self.assertIn("number", err)

    def test_missing_required(self):
        self.assertIn("celsius", jci.payload_error(self.cfg, "home/kitchen/temperature", b'{}'))

    def test_body_not_json_for_object_schema(self):
        self.assertIn("JSON", jci.payload_error(self.cfg, "home/kitchen/temperature", b"not json"))

    def test_string_schema_accepts_raw_text(self):
        self.assertIsNone(jci.payload_error(self.cfg, "home/alerts", b"smoke in the kitchen"))

    def test_unknown_channel_passes_unless_strict(self):
        self.assertIsNone(jci.payload_error(self.cfg, "jci/test/x", b"anything"))
        self.assertIn("not in", jci.payload_error({**self.cfg, "validate": "strict"}, "jci/test/x", b"x"))

    def test_validation_off(self):
        self.assertIsNone(jci.payload_error({"asyncapi": self.path}, "home/kitchen/temperature", b"garbage"))
        self.assertIsNone(jci.payload_error({"validate": "true"}, "home/kitchen/temperature", b"garbage"))


if __name__ == "__main__":
    unittest.main()
