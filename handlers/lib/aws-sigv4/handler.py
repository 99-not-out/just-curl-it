#!/usr/bin/env python3
"""AWS Signature Version 4 signing proxy. `curl http://mylocal.s3/bucket/key`
and the request reaches AWS signed; credentials live in your shell, your
~/.aws/credentials, or a command -- never in the curl line.

Stdlib only. Same shape as the `proxy` handler, with the Authorization
header computed instead of configured. Works for any SigV4 service
(`service = s3 | sqs | sts | execute-api | ...`); S3's distinct canonical-
URI rules (single encoding, no path normalisation) are handled.
"""
from __future__ import annotations

import configparser
import datetime
import hashlib
import hmac
import os
import re
import ssl
from collections import namedtuple
from urllib.parse import quote, unquote, unquote_plus

import jci

ROUTES = [
    "GET  /_info",
    "<any method> <any path>     signed and forwarded to <upstream><path>",
]

Credentials = namedtuple("Credentials", "access_key secret_key session_token")

ALGORITHM = "AWS4-HMAC-SHA256"
_UNRESERVED = "-_.~"


# ---------------------------------------------------------------------------
# canonicalisation

def _normalise_segments(path: str) -> list[str]:
    """RFC 3986 remove_dot_segments over '/'-split segments, dropping empties."""
    out: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return out


def canonical_uri(path: str, service: str) -> str:
    """S3: each segment encoded exactly once from its decoded form, no
    normalisation ('//' and '.' kept). Everything else: dot-segments and
    duplicate slashes removed, then each segment encoded as received, which
    double-encodes anything the client already encoded."""
    path = path or "/"
    if service == "s3":
        segs = [quote(unquote(seg), safe=_UNRESERVED) for seg in path.split("/")]
        uri = "/".join(segs)
        return uri if uri.startswith("/") else "/" + uri
    segs = [quote(seg, safe=_UNRESERVED) for seg in _normalise_segments(path)]
    uri = "/" + "/".join(segs)
    if path.endswith("/") and uri != "/":
        uri += "/"
    return uri


def canonical_query(query: str) -> str:
    """Form-decoded pairs ('+' is a space), re-encoded, sorted by name then
    value; a bare name becomes 'name='."""
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((quote(unquote_plus(k), safe=_UNRESERVED),
                      quote(unquote_plus(v), safe=_UNRESERVED)))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def canonical_headers(headers: dict) -> tuple[str, str]:
    """(canonical header block, signed-headers list) over every header given:
    names lowercased, values trimmed with inner whitespace collapsed."""
    items = sorted((k.lower().strip(), re.sub(r"\s+", " ", str(v)).strip())
                   for k, v in headers.items())
    block = "".join(f"{k}:{v}\n" for k, v in items)
    return block, ";".join(k for k, _ in items)


def canonical_request(method: str, path: str, query: str, headers: dict,
                      body: bytes, service: str,
                      payload_hash: str | None = None) -> tuple[str, str]:
    block, signed = canonical_headers(headers)
    payload_hash = payload_hash or hashlib.sha256(body or b"").hexdigest()
    creq = "\n".join([method.upper(), canonical_uri(path, service),
                      canonical_query(query), block, signed, payload_hash])
    return creq, signed


def _amz_date(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _scope(now: datetime.datetime, region: str, service: str) -> str:
    return f"{_amz_date(now)[:8]}/{region}/{service}/aws4_request"


def string_to_sign(now: datetime.datetime, region: str, service: str, creq: str) -> str:
    return "\n".join([ALGORITHM, _amz_date(now), _scope(now, region, service),
                      hashlib.sha256(creq.encode("utf-8")).hexdigest()])


def signing_key(secret: str, now: datetime.datetime, region: str, service: str) -> bytes:
    def h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    k = h(("AWS4" + secret).encode("utf-8"), _amz_date(now)[:8])
    k = h(k, region)
    k = h(k, service)
    return h(k, "aws4_request")


def authorization(creds: Credentials, now: datetime.datetime, region: str,
                  service: str, signed_headers: str, sts: str) -> str:
    sig = hmac.new(signing_key(creds.secret_key, now, region, service),
                   sts.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"{ALGORITHM} Credential={creds.access_key}/{_scope(now, region, service)}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")


def sign(method: str, path: str, query: str, headers: dict, body: bytes,
         creds: Credentials, region: str, service: str,
         now: datetime.datetime) -> dict[str, str]:
    """Headers to add to the outgoing request. Signs host, x-amz-*, and
    content-type if present, plus the date/payload/token headers it adds."""
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    added = {"x-amz-date": _amz_date(now), "x-amz-content-sha256": payload_hash}
    if creds.session_token:
        added["x-amz-security-token"] = creds.session_token
    to_sign = {k.lower(): v for k, v in headers.items()
               if k.lower() == "host" or k.lower() == "content-type"
               or k.lower().startswith("x-amz-")}
    to_sign.update(added)
    creq, signed = canonical_request(method, path, query, to_sign, body, service, payload_hash)
    sts = string_to_sign(now, region, service, creq)
    added["authorization"] = authorization(creds, now, region, service, signed, sts)
    return added


# ---------------------------------------------------------------------------
# credentials

def _from_env_lines(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip().strip("'\"")
    return out


def resolve_credentials(cfg: dict) -> Credentials:
    """Static keys, then a profile in the shared credentials file, then a
    command printing KEY=VALUE lines (`aws configure export-credentials
    --format env` fits)."""
    if cfg.get("access_key") and cfg.get("secret_key"):
        for k in ("access_key", "secret_key", "session_token"):
            if "${" in cfg.get(k, ""):
                raise jci.BackendConfigError(
                    f"{k} has unexpanded ${{...}}: {cfg[k]!r} -- env var not set in this shell")
        return Credentials(cfg["access_key"], cfg["secret_key"], cfg.get("session_token") or None)
    if cfg.get("profile"):
        path = os.environ.get("AWS_SHARED_CREDENTIALS_FILE",
                              os.path.expanduser("~/.aws/credentials"))
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        prof = cfg["profile"]
        if prof not in cp:
            raise jci.BackendConfigError(f"no [{prof}] profile in {path}")
        s = cp[prof]
        if not s.get("aws_access_key_id") or not s.get("aws_secret_access_key"):
            raise jci.BackendConfigError(f"profile [{prof}] in {path} lacks a key pair")
        return Credentials(s["aws_access_key_id"], s["aws_secret_access_key"],
                           s.get("aws_session_token") or None)
    if cfg.get("credentials_cmd"):
        env = _from_env_lines(jci.run_cmd(cfg["credentials_cmd"], "credentials_cmd"))
        ak, sk = env.get("AWS_ACCESS_KEY_ID"), env.get("AWS_SECRET_ACCESS_KEY")
        if not ak or not sk:
            raise jci.BackendConfigError(
                "credentials_cmd did not print AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
        return Credentials(ak, sk, env.get("AWS_SESSION_TOKEN") or None)
    raise jci.BackendConfigError(
        "no credentials: set access_key+secret_key, profile, or credentials_cmd")


# ---------------------------------------------------------------------------

def route(method: str, path: str, headers: dict, body: bytes) -> None:
    if method == "GET" and path.split("?", 1)[0].rstrip("/") == "/_info":
        return jci.info("proxy", ROUTES)

    cfg = jci.load_backend()
    u = jci.upstream_target(cfg)
    region, service = cfg.get("region", "").strip(), cfg.get("service", "").strip()
    if not region or not service:
        raise jci.BackendConfigError("region and service are required (e.g. eu-west-1, s3)")
    creds = resolve_credentials(cfg)

    default_port = 443 if u.scheme == "https" else 80
    authority = (u.hostname or "") + (f":{u.port}" if u.port and u.port != default_port else "")
    raw_path, _, query = path.partition("?")
    upstream_path = u.path.rstrip("/") + raw_path

    out = {k: v for k, v in headers.items()
           if k not in jci.HOP_BY_HOP_REQUEST and k != "authorization"}
    out["host"] = authority
    out.update(sign(method, upstream_path, query, out, body, creds, region, service,
                    datetime.datetime.now(datetime.timezone.utc)))

    try:
        conn = jci.connect_upstream(cfg, u)
        conn.request(method, upstream_path + (f"?{query}" if query else ""),
                     body=body or None, headers=out)
        resp = conn.getresponse()
    except ssl.SSLError as e:
        return jci.send_error(502, f"TLS to {authority}: {e}")
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e} ({authority})")
    try:
        jci.forward_response(resp, upstream_origin=f"{u.scheme}://{authority}")
    finally:
        conn.close()


if __name__ == "__main__":
    jci.main(route)
