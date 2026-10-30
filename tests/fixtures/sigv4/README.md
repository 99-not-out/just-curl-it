# AWS SigV4 test-suite vectors

Vendored from botocore (Apache-2.0, see `LICENSE`), path
`tests/unit/auth/aws4_testsuite/` and its `normalize-path/` subdirectory,
at commit `7f517a30f80daa50e2ab84c9b1cfa073c4d8cf58` of the `develop`
branch, fetched 2026-10-30. AWS removed the original "Signature Version 4
Test Suite" page from its documentation; botocore's copy is the surviving
public one.

Each case directory holds:

| file | contents |
|---|---|
| `<case>.req`   | the raw HTTP/1.1 request to sign |
| `<case>.creq`  | the expected canonical request |
| `<case>.sts`   | the expected string to sign |
| `<case>.authz` | the expected `Authorization` header value |

All cases use access key `AKIDEXAMPLE`, secret
`wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY`, region `us-east-1`, service
`service`, timestamp `20150830T123600Z`. They exercise the *generic*
canonical-URI rules (path normalisation, double encoding); the S3 rules
(single encoding, no normalisation) are covered by explicit assertions in
`tests/unit/test_sigv4.py`.

Used by `tests/unit/test_sigv4.py` against `handlers/lib/aws-sigv4/handler.py`.
