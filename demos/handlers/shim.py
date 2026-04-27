"""Shared helpers for intercept handlers.

Each handler runs as a fresh process with stdin = raw HTTP request
and stdout = raw HTTP response. These helpers hide the line noise.
"""
from __future__ import annotations

import configparser
import json
import os
import signal
import sys

_REASONS = {
    200: "OK", 201: "Created", 204: "No Content",
    400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
    500: "Internal Server Error",
}


def _sigpipe_default() -> None:
    """Restore default SIGPIPE so a client disconnect kills us cleanly."""
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, OSError, ValueError):
        pass


def enforce_methods(method: str) -> None:
    """If the route restricts HTTP methods (set by the shim via INTERCEPT_METHODS),
    write a 405 and exit when the request's method isn't on the allow-list.
    Empty / unset / "*" means "no restriction"."""
    allowed = os.environ.get("INTERCEPT_METHODS", "").strip()
    if not allowed or "*" in allowed:
        return
    methods = {m.strip().upper() for m in allowed.split(",") if m.strip()}
    if method.upper() in methods:
        return
    out = sys.stdout.buffer
    body = json.dumps({
        "error": f"method {method} not allowed for this route",
        "allow": sorted(methods),
    }).encode()
    out.write(b"HTTP/1.1 405 Method Not Allowed\r\n")
    out.write(b"Content-Type: application/json\r\n")
    out.write(f"Allow: {', '.join(sorted(methods))}\r\n".encode())
    out.write(f"Content-Length: {len(body)}\r\n".encode())
    out.write(b"X-Handled-By: jci-gate\r\n")
    out.write(b"Connection: close\r\n\r\n")
    out.write(body)
    out.flush()
    sys.exit(0)


def read_request():
    stdin = sys.stdin.buffer

    def readline() -> bytes:
        buf = bytearray()
        while True:
            b = stdin.read(1)
            if not b:
                return bytes(buf)
            buf += b
            if buf.endswith(b"\r\n"):
                return bytes(buf[:-2])

    req_line = readline().decode("iso-8859-1")
    parts = req_line.split(" ", 2)
    if len(parts) < 3:
        raise ValueError(f"bad request line: {req_line!r}")
    method, path, _ = parts
    enforce_methods(method)
    headers: dict[str, str] = {}
    while True:
        line = readline().decode("iso-8859-1")
        if not line:
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    body = b""
    cl = int(headers.get("content-length", "0") or 0)
    if cl:
        body = stdin.read(cl)
    return method, path, headers, body


def send(status: int, body, content_type: str = "application/json",
         handler: str = "intercept") -> None:
    if isinstance(body, str):
        body = body.encode("utf-8")
    out = sys.stdout.buffer
    out.write(f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n".encode())
    out.write(f"Content-Type: {content_type}\r\n".encode())
    out.write(f"Content-Length: {len(body)}\r\n".encode())
    out.write(f"X-Handled-By: intercept-{handler}\r\n".encode())
    out.write(b"Connection: close\r\n\r\n")
    out.write(body)
    out.flush()


def send_json(status: int, obj, handler: str = "intercept") -> None:
    send(status, json.dumps(obj, default=str), "application/json", handler=handler)


def send_sse_headers(handler: str = "intercept") -> None:
    """Open a streaming Server-Sent Events response.

    No Content-Length; we flush events until the client goes away.
    Defaults SIGPIPE so a dropped client just kills us.
    """
    _sigpipe_default()
    out = sys.stdout.buffer
    out.write(b"HTTP/1.1 200 OK\r\n")
    out.write(b"Content-Type: text/event-stream; charset=utf-8\r\n")
    out.write(b"Cache-Control: no-cache\r\n")
    out.write(f"X-Handled-By: intercept-{handler}\r\n".encode())
    out.write(b"Connection: close\r\n\r\n")
    out.flush()


def sse_event(obj, event: str | None = None) -> None:
    out = sys.stdout.buffer
    if event:
        out.write(f"event: {event}\n".encode())
    out.write(b"data: ")
    out.write(json.dumps(obj, default=str).encode("utf-8"))
    out.write(b"\n\n")
    out.flush()


def sse_comment(text: str) -> None:
    """SSE keepalive / status line. Clients ignore these."""
    out = sys.stdout.buffer
    out.write(f": {text}\n\n".encode())
    out.flush()


def load_backend(host: str, path: str | None = None) -> dict:
    path = path or os.environ.get("INTERCEPT_BACKENDS_CONF",
                                  "/etc/intercept/backends.conf")
    cp = configparser.ConfigParser()
    cp.read(path)
    if host not in cp:
        raise KeyError(f"no config section for host {host!r} in {path}")
    return dict(cp[host])
