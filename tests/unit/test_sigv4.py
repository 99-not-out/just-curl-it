"""SigV4 signer tests against handlers/lib/jci_aws.py: the AWS test-suite
vectors (vendored under tests/fixtures/sigv4/) plus the S3-specific path
rules the suite doesn't cover.
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import datetime
import os
import unittest
from unittest import mock

import jci_aws as sigv4

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIXTURES = os.path.join(REPO, "tests", "fixtures", "sigv4")

CREDS = sigv4.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", None)
NOW = datetime.datetime(2015, 8, 30, 12, 36, 0, tzinfo=datetime.timezone.utc)
CASES = sorted(d for d in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, d)))


def _read(case, ext):
    with open(os.path.join(FIXTURES, case, f"{case}.{ext}"), "rb") as f:
        return f.read().decode("utf-8")


def _parse_req(text):
    """Request line + headers + optional body, as the .req files are laid out.
    Repeated headers are joined with ', ' the way jci.read_request does."""
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    method, rest = lines[0].split(" ", 1)
    target = rest.rsplit(" HTTP/", 1)[0]
    path, _, query = target.partition("?")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        k = k.strip().lower()
        v = v.strip()
        headers[k] = f"{headers[k]},{v}" if k in headers else v
    return method, path, query, headers, body.encode("utf-8")


class Vectors(unittest.TestCase):
    def test_fixtures_present(self):
        self.assertGreaterEqual(len(CASES), 10, CASES)

    def test_all_vectors(self):
        for case in CASES:
            with self.subTest(case=case):
                method, path, query, headers, body = _parse_req(_read(case, "req"))
                creq, signed = sigv4.canonical_request(
                    method, path, query, headers, body, service="service")
                self.assertEqual(creq, _read(case, "creq"))
                sts = sigv4.string_to_sign(NOW, "us-east-1", "service", creq)
                self.assertEqual(sts, _read(case, "sts"))
                authz = sigv4.authorization(CREDS, NOW, "us-east-1", "service", signed, sts)
                self.assertEqual(authz, _read(case, "authz"))


class CanonicalUri(unittest.TestCase):
    def test_s3_path_single_encoded(self):
        # The handler sees the wire path (already percent-encoded by the
        # client). S3 signs the key encoded exactly once, so a well-encoded
        # path is unchanged and a literal space/plus gets encoded once.
        self.assertEqual(sigv4.canonical_uri("/b/my%20key%2B%25.txt", "s3"),
                         "/b/my%20key%2B%25.txt")
        self.assertEqual(sigv4.canonical_uri("/b/my key+.txt", "s3"),
                         "/b/my%20key%2B.txt")
        # ...whereas a generic service would encode that wire path again.
        self.assertEqual(sigv4.canonical_uri("/b/my%20key%2B%25.txt", "execute-api"),
                         "/b/my%2520key%252B%2525.txt")

    def test_s3_path_not_normalised(self):
        self.assertEqual(sigv4.canonical_uri("/a//b/./c", "s3"), "/a//b/./c")

    def test_generic_path_double_encoded(self):
        # A client sends "/a%20b" on the wire; generic services sign the
        # encoded form encoded again. (The suite's get-space case feeds a
        # literal space and expects %20 -- same rule, one level down.)
        self.assertEqual(sigv4.canonical_uri("/a%20b", "execute-api"), "/a%2520b")
        self.assertEqual(sigv4.canonical_uri("/a b", "execute-api"), "/a%20b")

    def test_generic_path_normalised(self):
        self.assertEqual(sigv4.canonical_uri("/a//b/./c/", "execute-api"), "/a/b/c/")


class CanonicalQuery(unittest.TestCase):
    def test_sorted_and_encoded(self):
        self.assertEqual(sigv4.canonical_query("b=2&a=1&a=0&c"), "a=0&a=1&b=2&c=")

    def test_plus_decodes_as_space(self):
        # Form-decoding rules, as botocore and S3 both apply them: '+' and
        # '%20' are the same value and canonicalise to %20; a literal plus
        # must arrive as %2B.
        self.assertEqual(sigv4.canonical_query("k=a+b&s=a%20b&p=a%2Bb"),
                         "k=a%20b&p=a%2Bb&s=a%20b")


class Sign(unittest.TestCase):
    def test_sign_adds_required_headers(self):
        out = sigv4.sign("PUT", "/bucket/key", "", {"host": "s3.example"}, b"hello",
                         CREDS, "us-east-1", "s3", NOW)
        self.assertEqual(out["x-amz-date"], "20150830T123600Z")
        self.assertEqual(out["x-amz-content-sha256"],
                         "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        self.assertTrue(out["authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="))
        self.assertNotIn("x-amz-security-token", out)

    def test_session_token_is_signed(self):
        creds = sigv4.Credentials("AK", "SK", "TOKEN")
        out = sigv4.sign("GET", "/", "", {"host": "h"}, b"", creds, "eu-west-1", "sts", NOW)
        self.assertEqual(out["x-amz-security-token"], "TOKEN")
        self.assertIn("x-amz-security-token", out["authorization"])


class ResolveCredentials(unittest.TestCase):
    def test_static_keys(self):
        c = sigv4.resolve_credentials({"access_key": "a", "secret_key": "b", "session_token": "t"})
        self.assertEqual(c, sigv4.Credentials("a", "b", "t"))

    def test_credentials_cmd(self):
        c = sigv4.resolve_credentials({
            "credentials_cmd": "printf 'AWS_ACCESS_KEY_ID=a\\nAWS_SECRET_ACCESS_KEY=b\\n'"})
        self.assertEqual(c, sigv4.Credentials("a", "b", None))

    def test_profile_from_shared_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("[work]\naws_access_key_id = pa\naws_secret_access_key = ps\n"
                    "aws_session_token = pt\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        with mock.patch.dict(os.environ, {"AWS_SHARED_CREDENTIALS_FILE": path}):
            c = sigv4.resolve_credentials({"profile": "work"})
        self.assertEqual(c, sigv4.Credentials("pa", "ps", "pt"))

    def test_none_configured_is_config_error(self):
        import jci
        with self.assertRaises(jci.BackendConfigError):
            sigv4.resolve_credentials({})


if __name__ == "__main__":
    unittest.main()
