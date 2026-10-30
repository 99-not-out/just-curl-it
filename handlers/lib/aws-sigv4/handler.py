#!/usr/bin/env python3
"""AWS Signature Version 4 signing proxy. `curl http://mylocal.s3/bucket/key`
and the request reaches AWS signed; credentials live in your shell, your
~/.aws/credentials, or a command -- never in the curl line.

Stdlib only. Same shape as the `proxy` handler, with the Authorization
header computed instead of configured. The signer itself is
handlers/lib/jci_aws.py, shared with the Athena and Bedrock paths.
"""
from __future__ import annotations

import ssl

import jci
import jci_aws

ROUTES = [
    "GET  /_info",
    "<any method> <any path>     signed and forwarded to <upstream><path>",
]


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    if method == "GET" and path.split("?", 1)[0].rstrip("/") == "/_info":
        return jci.info("proxy", ROUTES)

    cfg = jci.load_backend()
    u = jci.upstream_target(cfg)
    default_port = 443 if u.scheme == "https" else 80
    authority = (u.hostname or "") + (f":{u.port}" if u.port and u.port != default_port else "")
    raw_path, _, query = path.partition("?")
    upstream_path = u.path.rstrip("/") + raw_path

    out = {k: v for k, v in headers.items()
           if k not in jci.HOP_BY_HOP_REQUEST and k != "authorization"}
    out["host"] = authority
    out.update(jci_aws.sign_request(cfg, method, upstream_path, query, out, body))

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
