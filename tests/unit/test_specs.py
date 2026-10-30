"""Spec-aware /_info: OpenAPI and AsyncAPI documents rendered into the route
menu that jci.info() returns, so `jci-info` tells an agent what an
unfamiliar upstream actually offers.
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import jci

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.2"},
    "servers": [{"url": "https://api.example/v1"}],
    "paths": {
        "/pets": {
            "get": {"summary": "List pets", "parameters": [
                {"name": "limit", "in": "query", "schema": {"type": "integer"}}]},
            "post": {"summary": "Create a pet", "requestBody": {"content": {"application/json": {
                "schema": {"type": "object", "properties": {"name": {"type": "string"},
                                                            "age": {"type": "integer"}}}}}}},
        },
        "/pets/{id}": {
            "get": {"summary": "One pet", "parameters": [{"name": "id", "in": "path"}]},
            "delete": {"operationId": "deletePet"},
        },
    },
}

ASYNCAPI_V2 = {
    "asyncapi": "2.6.0",
    "info": {"title": "Sensors", "version": "3"},
    "servers": {"prod": {"url": "mqtt://broker:1883", "protocol": "mqtt"}},
    "channels": {
        "home/{room}/temperature": {
            "description": "Room temperature readings",
            "publish": {"message": {"name": "Reading", "payload": {
                "type": "object", "properties": {"celsius": {"type": "number"},
                                                 "at": {"type": "string", "format": "date-time"}}}}},
        },
        "home/alerts": {"subscribe": {"message": {"name": "Alert", "payload": {"type": "string"}}}},
    },
}

ASYNCAPI_V3 = {
    "asyncapi": "3.0.0",
    "info": {"title": "Orders", "version": "1"},
    "channels": {
        "orders": {"address": "shop.orders", "description": "Order events",
                   "messages": {"OrderPlaced": {"payload": {"type": "object", "properties": {
                       "id": {"type": "string"}, "total": {"type": "number"}}}}}},
    },
    "operations": {
        "onOrder": {"action": "receive", "channel": {"$ref": "#/channels/orders"}},
    },
}


class OpenAPIMenu(unittest.TestCase):
    def test_operations_listed_with_summaries(self):
        api = jci.openapi_menu(OPENAPI, host="pets.api")
        self.assertEqual(api["title"], "Pets")
        self.assertEqual(api["version"], "1.2")
        ops = {(o["method"], o["path"]): o for o in api["operations"]}
        self.assertEqual(ops[("GET", "/pets")]["summary"], "List pets")
        self.assertEqual(ops[("DELETE", "/pets/{id}")]["summary"], "deletePet")   # falls back to operationId
        self.assertEqual(ops[("GET", "/pets")]["query"], ["limit"])

    def test_example_curls(self):
        api = jci.openapi_menu(OPENAPI, host="pets.api")
        ops = {(o["method"], o["path"]): o for o in api["operations"]}
        self.assertEqual(ops[("GET", "/pets/{id}")]["curl"], "curl http://pets.api/pets/{id}")
        self.assertIn("-X POST http://pets.api/pets", ops[("POST", "/pets")]["curl"])
        self.assertIn('"name": "string"', ops[("POST", "/pets")]["curl"])   # body example from schema

    def test_sorted_by_path_then_method(self):
        api = jci.openapi_menu(OPENAPI, host="h")
        self.assertEqual([(o["method"], o["path"]) for o in api["operations"]],
                         [("GET", "/pets"), ("POST", "/pets"), ("DELETE", "/pets/{id}"), ("GET", "/pets/{id}")])


class AsyncAPIMenu(unittest.TestCase):
    def test_v2_channels_with_examples(self):
        api = jci.asyncapi_menu(ASYNCAPI_V2, host="mylocal.mqtt")
        self.assertEqual(api["title"], "Sensors")
        chans = {c["channel"]: c for c in api["channels"]}
        t = chans["home/{room}/temperature"]
        self.assertEqual(t["description"], "Room temperature readings")
        self.assertEqual(t["message"], "Reading")
        self.assertEqual(t["payload"], {"celsius": "number", "at": "string (date-time)"})
        self.assertIn("curl -N http://mylocal.mqtt/stream/home/{room}/temperature/tail", t["curl"])
        self.assertTrue(any('"celsius": 0' in c for c in t["curl"]), t["curl"])
        self.assertEqual(chans["home/alerts"]["payload"], "string")

    def test_v3_channels_use_address(self):
        api = jci.asyncapi_menu(ASYNCAPI_V3, host="mylocal.kafka")
        chans = {c["channel"]: c for c in api["channels"]}
        self.assertIn("shop.orders", chans)
        self.assertEqual(chans["shop.orders"]["message"], "OrderPlaced")
        self.assertEqual(chans["shop.orders"]["payload"], {"id": "string", "total": "number"})
        self.assertEqual(chans["shop.orders"]["operations"], ["receive"])


class LoadSpec(unittest.TestCase):
    def test_json_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(OPENAPI, f)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(jci.load_spec(f.name)["info"]["title"], "Pets")

    def test_yaml_path_without_pyyaml_names_the_fix(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("openapi: 3.0.3\ninfo:\n  title: Y\n")
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict("sys.modules", {"yaml": None}):   # simulate PyYAML absent
            with self.assertRaises(jci.BackendConfigError) as cm:
                jci.load_spec(f.name)
        self.assertIn("PyYAML", str(cm.exception))

    def test_missing_path(self):
        with self.assertRaises(jci.BackendConfigError):
            jci.load_spec("/nonexistent/spec.json")


class InfoWithSpec(unittest.TestCase):
    def test_info_attaches_api_section(self):
        import io, sys
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(OPENAPI, f)
        self.addCleanup(os.unlink, f.name)
        fd, conf = tempfile.mkstemp(suffix=".conf")
        with os.fdopen(fd, "w") as c:
            c.write(f"[h]\nupstream = https://api.example\nopenapi = {f.name}\n")
        self.addCleanup(os.unlink, conf)
        out = io.BytesIO()
        wrapper = io.TextIOWrapper(out)   # keep a ref: GC of the wrapper closes `out`
        with mock.patch.dict(os.environ, {"INTERCEPT_BACKENDS_CONF": conf, "INTERCEPT_HOST": "h"}), \
             mock.patch.object(jci, "HOST", "h"), \
             mock.patch.object(sys, "stdout", wrapper):
            jci.info("proxy", ["GET /_info"])
            raw = out.getvalue()
        body = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(body["api"]["title"], "Pets")
        self.assertEqual(len(body["api"]["operations"]), 4)


if __name__ == "__main__":
    unittest.main()
