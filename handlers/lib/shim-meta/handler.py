#!/usr/bin/env python3
"""Introspect the active jci env: the route table and (redacted) backends.

Routes:
  GET /          routes + backends
  GET /routes    parsed routes.conf: host, methods, handler
  GET /backends  parsed backends.conf, secrets redacted
  GET /_info
"""
from __future__ import annotations

import configparser
import os
import re
from urllib.parse import urlsplit

import jci

ROUTES_PATH = os.environ.get("INTERCEPT_CONFIG", "/etc/intercept/routes.conf")
BACKENDS_PATH = os.environ.get("INTERCEPT_BACKENDS_CONF", "/etc/intercept/backends.conf")

ROUTES = ["GET /", "GET /routes", "GET /backends", "GET /_info"]

# Same rule as the shim's is_method_token(): the optional second column is
# all uppercase letters, commas or '*'.
_METHODS = re.compile(r"^[A-Z,*]+$")


def read_routes() -> list[dict]:
    out = []
    if not os.path.exists(ROUTES_PATH):
        return out
    with open(ROUTES_PATH) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(None, 2)
            if len(parts) < 2:
                continue
            if len(parts) == 3 and _METHODS.match(parts[1]):
                host, methods, handler = parts
            else:
                host, methods, handler = parts[0], "*", " ".join(parts[1:])
            out.append({"host": host, "methods": methods, "handler": handler})
    return out


def read_backends() -> dict:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(BACKENDS_PATH)
    return {s: jci.redact(dict(cp[s])) for s in cp.sections()}


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    p = urlsplit(path).path.rstrip("/") or "/"
    if method != "GET":
        return jci.send_error(405, "only GET", allow=["GET"])
    if p == "/_info":
        return jci.info("meta", ROUTES)
    if p == "/routes":
        return jci.send_json(200, {"routes": read_routes()})
    if p == "/backends":
        return jci.send_json(200, {"backends": read_backends()})
    if p == "/":
        return jci.send_json(200, {"routes": read_routes(), "backends": read_backends()})
    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
