#!/usr/bin/env python3
"""Plain HTTP in, authenticated HTTP(S) out.

Forwards the request to the `upstream` configured for this host, injecting
an Authorization header (from an env var, the macOS Keychain, or a command)
plus any `header_*` keys, optionally over mTLS. Response streams back with
upstream-origin Location headers rewritten onto the intercepted hostname,
so `curl -L` stays inside the shim. All the mechanics live in jci.py; this
file is the glue and the error wording.
"""
from __future__ import annotations

import ssl

import jci


ROUTES = [
    "GET  /_info",
    "<any method> <any path>     forwarded to <upstream><path> with auth injected",
]


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    # /_info is jci's, not the upstream's: with `openapi =` in the config it
    # becomes the upstream's operation menu. An upstream path literally
    # named /_info is unreachable through this handler; that's the trade.
    if method == "GET" and path.split("?", 1)[0] == "/_info":
        return jci.info("proxy", ROUTES)
    cfg = jci.load_backend()
    u = jci.upstream_target(cfg)
    # Host must carry a non-default port, or an upstream on :8443 builds its
    # own URLs (redirects, links) without it.
    default_port = 443 if u.scheme == "https" else 80
    authority = (u.hostname or "") + (f":{u.port}" if u.port and u.port != default_port else "")
    try:
        out_headers = jci.inject_headers(cfg, headers, authority)
        conn = jci.connect_upstream(cfg, u)
        conn.request(method, u.path.rstrip("/") + path,
                     body=body or None, headers=dict(out_headers))
        resp = conn.getresponse()
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        # Short-lived client certs (Teleport, SPIFFE, Vault) make expiry the
        # single most common mTLS failure, and the raw alert doesn't say whose
        # certificate it was talking about.
        if isinstance(e, ssl.SSLError) and "EXPIRED" in str(e).upper():
            detail += " -- renew the client certificate at its source"
        # Python >= 3.13 verifies strictly by default; a CA that issues leaves
        # without SKI/AKI extensions fails with this, not with anything that
        # names the real problem.
        elif isinstance(e, ssl.SSLError) and "KEY IDENTIFIER" in str(e).upper():
            detail += (" -- the upstream's certificate chain lacks SKI/AKI "
                       "extensions, which Python 3.13+ requires by default")
        return jci.send_error(502, detail)
    try:
        jci.forward_response(resp, upstream_origin=f"{u.scheme}://{authority}")
    finally:
        conn.close()


if __name__ == "__main__":
    jci.main(route)
