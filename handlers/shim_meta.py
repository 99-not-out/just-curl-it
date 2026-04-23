#!/usr/bin/env python3
"""HTTP adapter that introspects the shim itself.

Routes:
  GET /         index: routes + backends
  GET /routes   parsed routes.conf
  GET /backends parsed backends.conf
  GET /_info
"""
from __future__ import annotations

import configparser
import os
import sys
import traceback
from urllib.parse import urlsplit

from shim import read_request, send_json

HOST = os.environ.get("INTERCEPT_HOST", "")
ROUTES_PATH = "/etc/intercept/routes.conf"
BACKENDS_PATH = "/etc/intercept/backends.conf"

_INFO = {
    "handler": "shim",
    "routes": [
        "GET /",
        "GET /routes",
        "GET /backends",
        "GET /_info",
    ],
}


def read_routes() -> list[dict]:
    out = []
    if not os.path.exists(ROUTES_PATH):
        return out
    with open(ROUTES_PATH) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(None, 1)
            if len(parts) == 2:
                out.append({"host": parts[0], "handler": parts[1]})
    return out


def read_backends() -> dict:
    cp = configparser.ConfigParser()
    cp.read(BACKENDS_PATH)
    return {s: dict(cp[s]) for s in cp.sections()}


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    p = urlsplit(path).path.rstrip("/") or "/"
    if method != "GET":
        return send_json(405, {"error": "only GET"}, handler="shim")
    if p == "/_info":
        return send_json(200, {**_INFO, "host": HOST}, handler="shim")
    if p == "/routes":
        return send_json(200, {"routes": read_routes()}, handler="shim")
    if p == "/backends":
        return send_json(200, {"backends": read_backends()}, handler="shim")
    if p == "/":
        return send_json(200, {
            "routes": read_routes(),
            "backends": read_backends(),
        }, handler="shim")
    send_json(404, {"error": f"no route for {method} {p}"}, handler="shim")


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"}, handler="shim")
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"}, handler="shim")


if __name__ == "__main__":
    main()
