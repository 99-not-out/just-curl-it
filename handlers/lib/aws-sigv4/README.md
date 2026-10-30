# aws-sigv4

Curl an S3 bucket. Or SQS, STS, API Gateway — anything that wants an AWS
Signature Version 4. The request is signed on the way through and your
credentials never appear in the command line, the shell history, or the
agent's context. Stdlib only.

```sh
curl http://mylocal.s3/my-bucket/reports/2026-10.json | jq .
curl -X PUT http://mylocal.s3/my-bucket/reports/2026-10.json --data-binary @report.json
curl 'http://mylocal.s3/my-bucket?list-type=2&prefix=reports/' | xmllint --format -
curl -X DELETE http://mylocal.s3/my-bucket/scratch/tmp.txt
```

With a `GET`-only route, an agent can read the bucket and provably not
write to it.

## Routes

```
<any method> <any path>     signed and forwarded to <upstream><path>
GET /_info
```

The path and query string reach AWS exactly as sent; the handler adds
`x-amz-date`, `x-amz-content-sha256` (hash of the body), `x-amz-security-token`
when there is one, and `Authorization`. An incoming `Authorization` header
is discarded. Upstream `Location` headers pointing back at the upstream are
rewritten onto the intercepted hostname.

## backends.conf

```ini
[mylocal.s3]
upstream = https://s3.eu-west-1.amazonaws.com    # path-style: bucket is the first path segment
region   = eu-west-1
service  = s3

# Credentials -- first one configured wins:
access_key    = ${AWS_ACCESS_KEY_ID}            # 1. static keys, ${VAR}-expanded
secret_key    = ${AWS_SECRET_ACCESS_KEY}
session_token = ${AWS_SESSION_TOKEN}            #    optional
profile       = default                          # 2. a profile in ~/.aws/credentials
                                                 #    (AWS_SHARED_CREDENTIALS_FILE honoured)
credentials_cmd = aws configure export-credentials --format env
                                                 # 3. a command printing KEY=VALUE lines
                                                 #    (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
                                                 #     AWS_SESSION_TOKEN); SSO and assumed
                                                 #     roles arrive this way
timeout  = 5
```

A non-S3 service is the same shape:

```ini
[mylocal.sqs]
upstream = https://sqs.eu-west-1.amazonaws.com
region   = eu-west-1
service  = sqs
profile  = default
```

```sh
curl -X POST http://mylocal.sqs/ -d 'Action=ListQueues&Version=2012-11-05'
```

## Notes

- **Path-style vs virtual-host style.** `upstream = https://s3.<region>.amazonaws.com`
  and the bucket as the first path segment works for every bucket. For
  virtual-host style set `upstream = https://<bucket>.s3.<region>.amazonaws.com`
  and leave the bucket out of the path; the handler signs whatever `Host`
  it connects to.
- **S3 signs paths differently** from every other service (single-encoded,
  not normalised; everything else is double-encoded and normalised). The
  handler switches on `service = s3`, and both rule sets are pinned by the
  AWS test-suite vectors in `tests/fixtures/sigv4/`.
- **Encode your keys.** A key with a space is `my%20key.txt` in the URL;
  curl rejects a raw space. `+` in a key must be `%2B`. In a *query string*
  a `+` is treated as a space (form decoding), so a literal plus there is
  `%2B` too; the AWS test vectors never put a raw `+` in a query, so this
  follows what S3 does rather than a published rule.
- **Body is held in memory** to hash it (`x-amz-content-sha256`). Fine for
  objects up to `JCI_MAX_BODY` (64 MiB default); multipart upload is not
  implemented.
- S3 errors come back as AWS's XML; `| xmllint --format -` or
  `| python3 -c 'import sys,xml.dom.minidom;print(xml.dom.minidom.parse(sys.stdin).toprettyxml())'`.
- Clock skew over 15 minutes is rejected by AWS (`RequestTimeTooSkewed`);
  the handler uses the system clock.
