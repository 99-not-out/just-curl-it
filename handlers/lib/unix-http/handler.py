#!/usr/bin/env python3
"""HTTP over a Unix domain socket: the Docker, Podman and systemd socket
APIs, or anything else that speaks HTTP on a filesystem path.

The request is forwarded unchanged -- method, path and query, body, and
every non-hop-by-hop header -- and the response streams back. That is the
whole handler. A GET-only route gives a read-only view of the daemon.
"""
from __future__ import annotations

import errno as _errno
import http.client
import os
import socket

import jci


def errno_name(code: int) -> str:
    return _errno.errorcode.get(code, str(code))

ROUTES = [
    "GET  /_info",
    "<any method> <any path>     forwarded as-is over the socket",
]


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    if method == "GET" and path.split("?", 1)[0].rstrip("/") == "/_info":
        return jci.info("proxy", ROUTES)

    cfg = jci.load_backend()
    sock_path = os.path.expanduser(cfg.get("socket", "").strip())
    if not sock_path:
        raise jci.BackendConfigError("missing 'socket' in backends.conf")

    out_headers = {k: v for k, v in headers.items() if k not in jci.HOP_BY_HOP_REQUEST}
    out_headers.setdefault("host", "localhost")

    conn = UnixHTTPConnection(sock_path, jci.timeout(cfg))
    try:
        conn.request(method, path, body=body or None, headers=out_headers)
        resp = conn.getresponse()
    except OSError as e:
        # Missing, refused, not-a-socket, path too long, timeout: every one
        # is "couldn't talk to the socket", so every one is a 502 that names it.
        errno = getattr(e, "errno", None)
        reason = os.strerror(errno) if errno else (str(e) or type(e).__name__)
        return jci.send_error(502, f"{reason}: {sock_path}",
                              errno=errno_name(errno) if errno else None)
    try:
        jci.forward_response(resp)
    finally:
        conn.close()


if __name__ == "__main__":
    jci.main(route)
