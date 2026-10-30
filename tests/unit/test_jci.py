"""Unit tests for handlers/lib/jci.py -- the stdlib plumbing every Python
handler shares. Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import jci


def _feed(raw: bytes):
    """Replace sys.stdin with a buffer holding `raw`; return a stdout capture."""
    stdin = io.TextIOWrapper(io.BytesIO(raw))
    out = io.BytesIO()
    stdout = io.TextIOWrapper(out)
    return mock.patch.multiple(sys, stdin=stdin, stdout=stdout), out


def _capture():
    out = io.BytesIO()
    return mock.patch.object(sys, "stdout", io.TextIOWrapper(out)), out


class ReadRequest(unittest.TestCase):
    def test_content_length_body(self):
        patch, _ = _feed(b"POST /x HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc")
        with patch:
            method, path, headers, body = jci.read_request()
        self.assertEqual((method, path, body), ("POST", "/x", b"abc"))
        self.assertEqual(headers["content-length"], "3")

    def test_header_keys_lowercased_and_joined(self):
        patch, _ = _feed(b"GET / HTTP/1.1\r\nX-Tag: a\r\nx-tag: b\r\n\r\n")
        with patch:
            _, _, headers, _ = jci.read_request()
        self.assertEqual(headers["x-tag"], "a, b")

    def test_chunked_body_decoded(self):
        raw = (b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
        patch, _ = _feed(raw)
        with patch:
            _, _, _, body = jci.read_request()
        self.assertEqual(body, b"abcde")

    def test_chunked_bad_size_raises(self):
        raw = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\nabc\r\n0\r\n\r\n"
        patch, _ = _feed(raw)
        with patch, self.assertRaises(ValueError):
            jci.read_request()

    def test_chunked_truncated_raises(self):
        raw = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab"
        patch, _ = _feed(raw)
        with patch, self.assertRaises(ValueError):
            jci.read_request()

    def test_expect_100_continue_written_before_body(self):
        raw = b"POST / HTTP/1.1\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\nhi"
        patch, out = _feed(raw)
        with patch:
            _, _, _, body = jci.read_request()
        self.assertEqual(body, b"hi")
        self.assertTrue(out.getvalue().startswith(b"HTTP/1.1 100 Continue\r\n\r\n"),
                        out.getvalue())

    def test_body_over_max_raises_413(self):
        raw = b"POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello"
        patch, _ = _feed(raw)
        with patch, mock.patch.dict(os.environ, {"JCI_MAX_BODY": "4"}):
            with self.assertRaises(jci.TooLarge):
                jci.read_request()
        # and main() turns it into a 413
        patch, out = _feed(raw)
        with patch, mock.patch.dict(os.environ, {"JCI_MAX_BODY": "4"}):
            jci.main(lambda *a: self.fail("route must not run"))
        self.assertTrue(out.getvalue().startswith(b"HTTP/1.1 413 "), out.getvalue())

    def test_bad_request_line_is_400_via_main(self):
        patch, out = _feed(b"garbage\r\n\r\n")
        with patch:
            jci.main(lambda *a: self.fail("route must not run"))
        head = out.getvalue()
        self.assertTrue(head.startswith(b"HTTP/1.1 400 "), head)
        self.assertIn(b'"handler"', head)


class LoadBackend(unittest.TestCase):
    def _conf(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".conf")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_expands_env_and_no_interpolation(self):
        path = self._conf("[h]\ndsn = postgresql://u:%s${P}@h\n")
        with mock.patch.dict(os.environ, {"INTERCEPT_BACKENDS_CONF": path, "P": "xyz"}):
            cfg = jci.load_backend("h")
        self.assertEqual(cfg["dsn"], "postgresql://u:%sxyz@h")

    def test_missing_section_names_file(self):
        path = self._conf("[other]\nx = 1\n")
        with mock.patch.dict(os.environ, {"INTERCEPT_BACKENDS_CONF": path}):
            with self.assertRaises(jci.BackendConfigError) as cm:
                jci.load_backend("h")
        self.assertIn("[h]", str(cm.exception))
        self.assertIn(path, str(cm.exception))

    def test_missing_file_names_file(self):
        missing = "/nonexistent/jci-backends.conf"
        with mock.patch.dict(os.environ, {"INTERCEPT_BACKENDS_CONF": missing}):
            with self.assertRaises(jci.BackendConfigError) as cm:
                jci.load_backend("h")
        self.assertIn(missing, str(cm.exception))

    def test_timeout_default_5(self):
        self.assertEqual(jci.timeout({}), 5.0)
        self.assertEqual(jci.timeout({"timeout": "2.5"}), 2.5)


class Redact(unittest.TestCase):
    def test_key_patterns(self):
        cfg = {"password": "p", "api_token": "t", "Authorization": "Bearer x",
               "ssl_key": "/k.pem", "host": "db.local"}
        r = jci.redact(cfg)
        for k in ("password", "api_token", "Authorization", "ssl_key"):
            self.assertEqual(r[k], "***", k)
        self.assertEqual(r["host"], "db.local")

    def test_url_userinfo(self):
        r = jci.redact({"url": "amqp://guest:guest@h:5672/", "plain": "http://h/"})
        self.assertEqual(r["url"], "amqp://***@h:5672/")
        self.assertEqual(r["plain"], "http://h/")


class _FakeResp:
    def __init__(self, status, headers, body=b""):
        self.status, self.reason = status, "X"
        self._headers, self._body = headers, io.BytesIO(body)

    def getheaders(self):
        return self._headers

    def read(self, n=-1):
        return self._body.read(n)


class ForwardResponse(unittest.TestCase):
    def _run(self, resp, origin=None):
        patch, out = _capture()
        with patch, mock.patch.object(jci, "HOST", "gh.api"):
            jci.forward_response(resp, upstream_origin=origin)
        return out.getvalue().decode("iso-8859-1")

    def test_location_rewritten_for_upstream_origin(self):
        out = self._run(_FakeResp(302, [("Location", "https://api.example.com/a?b=1")]),
                        origin="https://api.example.com")
        self.assertIn("Location: http://gh.api/a?b=1\r\n", out)

    def test_relative_location_untouched(self):
        out = self._run(_FakeResp(302, [("Location", "/next")]), origin="https://api.example.com")
        self.assertIn("Location: /next\r\n", out)

    def test_other_origin_untouched(self):
        out = self._run(_FakeResp(302, [("Location", "https://elsewhere.example/x")]),
                        origin="https://api.example.com")
        self.assertIn("Location: https://elsewhere.example/x\r\n", out)

    def test_hop_by_hop_stripped(self):
        out = self._run(_FakeResp(200, [("Transfer-Encoding", "chunked"),
                                        ("Connection", "keep-alive"),
                                        ("Content-Type", "text/plain")], b"ok"))
        self.assertNotIn("Transfer-Encoding", out)
        self.assertNotIn("keep-alive", out)
        self.assertIn("Content-Type: text/plain\r\n", out)
        self.assertIn("Connection: close\r\n", out)
        self.assertTrue(out.endswith("\r\n\r\nok"), out)


class SendError(unittest.TestCase):
    def test_body_shape_and_allow_header(self):
        patch, out = _capture()
        with patch, mock.patch.object(jci, "HANDLER", "demo"):
            jci.send_error(405, "nope", allow=["POST"], hint="use POST")
        head, _, body = out.getvalue().partition(b"\r\n\r\n")
        self.assertIn(b"HTTP/1.1 405 ", head)
        self.assertIn(b"Allow: POST\r\n", head)
        self.assertIn(b"X-Handled-By: jci-demo\r\n", head)
        self.assertEqual(json.loads(body), {"error": "nope", "handler": "demo",
                                            "allow": ["POST"], "hint": "use POST"})


if __name__ == "__main__":
    unittest.main()
