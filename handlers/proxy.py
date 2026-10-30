#!/usr/bin/env python3
"""HTTP -> HTTPS forward proxy with auth injection.

Reads an HTTP/1.x request from stdin, forwards it to a configured upstream
over TLS with an Authorization header (and optional extra headers) injected,
streams the response back to stdout.

Configured per-host via backends.conf:

    [api.github.com]
    upstream = https://api.github.com

    # Auth -- pick one source. Tried in this order if multiple are set:
    authorization          = Bearer ${GITHUB_TOKEN}     # literal + env expansion
    authorization_keychain = github-api-token            # macOS Keychain
    authorization_cmd      = pass show github/api        # arbitrary shell pipe

    # Optional extra headers (any key starting with "header_"):
    header_accept              = application/vnd.github+json
    header_x-github-api-version = 2022-11-28

    # Client certificates, for an upstream that wants mTLS (Teleport app
    # access, corp TLS gateways). Either a path or a command printing one --
    # a command may also print the PEM itself, which never touches disk
    # beyond a 0600 temp file unlinked once loaded.
    client_cert     = ${TSH_APP_CERT}
    client_key      = ${TSH_APP_KEY}
    client_cert_cmd = tsh apps config --format=cert
    client_key_cmd  = tsh apps config --format=key
    ca_cert         = /path/ca.pem      # replaces the system trust store

Stdlib only -- no venv needed. The helpers below (load_backend, inject_headers,
upstream_target, connect_upstream) are importable, so a protocol handler can
reach an authed upstream without reimplementing any of this.
"""
from __future__ import annotations

import configparser
import http.client
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.parse


HOP_BY_HOP_REQUEST = {
    "host", "connection", "proxy-connection", "keep-alive",
    "transfer-encoding", "upgrade", "te", "trailer",
}
HOP_BY_HOP_RESPONSE = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-connection",
}


def enforce_methods(method: str) -> None:
    """Honor the route's INTERCEPT_METHODS allow-list set by the shim."""
    allowed = os.environ.get("INTERCEPT_METHODS", "").strip()
    if not allowed or "*" in allowed:
        return
    methods = {m.strip().upper() for m in allowed.split(",") if m.strip()}
    if method.upper() in methods:
        return
    import json
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


def read_request(stdin):
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
    headers: list[tuple[str, str]] = []
    while True:
        line = readline().decode("iso-8859-1")
        if not line:
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers.append((k.strip(), v.strip()))
    cl = 0
    for k, v in headers:
        if k.lower() == "content-length":
            try:
                cl = int(v)
            except ValueError:
                cl = 0
            break
    body = stdin.read(cl) if cl else b""
    return method, path, headers, body


def write_error(status: int, msg: str) -> None:
    body = msg.encode("utf-8")
    out = sys.stdout.buffer
    out.write(f"HTTP/1.1 {status} jci-proxy-error\r\n".encode())
    out.write(b"Content-Type: text/plain; charset=utf-8\r\n")
    out.write(f"Content-Length: {len(body)}\r\n".encode())
    out.write(b"X-Handled-By: jci-proxy\r\n")
    out.write(b"Connection: close\r\n\r\n")
    out.write(body)
    out.flush()


def load_backend(host: str) -> dict:
    path = os.environ.get("INTERCEPT_BACKENDS_CONF",
                          "/etc/intercept/backends.conf")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path)
    if host not in cp:
        raise KeyError(f"no [{host}] section in {path}")
    # Expand ${VAR} from the environment so secrets can live in shell env
    # rather than on disk.
    return {k: os.path.expandvars(v) for k, v in cp[host].items()}


def resolve_auth(cfg: dict) -> str | None:
    if "authorization" in cfg:
        v = cfg["authorization"].strip()
        if "${" in v:
            raise RuntimeError(
                f"authorization has unexpanded ${{...}}: {v!r} "
                f"-- the referenced env var isn't set in this shell")
        return v
    if "authorization_keychain" in cfg:
        name = cfg["authorization_keychain"]
        try:
            out = subprocess.check_output(
                ["security", "find-generic-password", "-s", name, "-w"],
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("authorization_keychain requires macOS security(1)")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"keychain lookup failed for {name!r}: "
                f"{e.stderr.decode().strip() or e}")
        return out.decode().strip()
    if "authorization_cmd" in cfg:
        return run_cmd(cfg["authorization_cmd"], "authorization_cmd")
    return None


def run_cmd(cmd: str, what: str) -> str:
    try:
        out = subprocess.check_output(["sh", "-c", cmd], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{what} failed: {e.stderr.decode().strip() or e}")
    return out.decode().strip()


def resolve_pem(cfg: dict, key: str) -> tuple[str | None, bool]:
    """Path to the PEM configured under `key`, plus whether we created it.

    `<key>` is a path; `<key>_cmd` is a command printing either a path or the
    PEM itself. Returns (None, False) when neither is configured.
    """
    if key in cfg:
        v = cfg[key].strip()
        if "${" in v:
            raise RuntimeError(
                f"{key} has unexpanded ${{...}}: {v!r} "
                f"-- the referenced env var isn't set in this shell")
        return v, False
    cmd_key = f"{key}_cmd"
    if cmd_key in cfg:
        out = run_cmd(cfg[cmd_key], cmd_key)
        if out.startswith("-----BEGIN"):
            fd, path = tempfile.mkstemp(prefix="jci-", suffix=".pem")
            with os.fdopen(fd, "w") as f:
                f.write(out + "\n")
            return path, True
        return out, False
    return None, False


def tls_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=cfg.get("ca_cert") or None)
    cert, cert_tmp = resolve_pem(cfg, "client_cert")
    key, key_tmp = resolve_pem(cfg, "client_key")
    try:
        if bool(cert) != bool(key):
            raise RuntimeError(
                "client_cert and client_key must be set together "
                f"(got client_cert={cert!r}, client_key={key!r})")
        if cert and key:
            for path, what in ((cert, "client_cert"), (key, "client_key")):
                if not os.path.exists(path):
                    raise RuntimeError(
                        f"{what}: no such file: {path} "
                        f"-- an expired session leaves the config pointing at nothing")
            ctx.load_cert_chain(cert, key)
    finally:
        for path, is_tmp in ((cert, cert_tmp), (key, key_tmp)):
            if is_tmp and path:
                os.unlink(path)
    return ctx


def upstream_target(cfg: dict, host: str = "") -> urllib.parse.SplitResult:
    upstream = cfg.get("upstream")
    if not upstream:
        raise RuntimeError(
            f"missing 'upstream' in [{host}]" if host
            else "missing 'upstream' in backends.conf")
    u = urllib.parse.urlsplit(upstream)
    if u.scheme not in ("http", "https"):
        raise RuntimeError(f"unsupported upstream scheme: {u.scheme}")
    upstream_host(u)
    return u


def upstream_host(u: urllib.parse.SplitResult) -> str:
    if not u.hostname:
        raise RuntimeError(f"upstream has no host: {u.geturl()!r}")
    return u.hostname


def connect_upstream(cfg: dict,
                     u: urllib.parse.SplitResult) -> http.client.HTTPConnection:
    if u.scheme == "https":
        return http.client.HTTPSConnection(upstream_host(u), u.port or 443,
                                           context=tls_context(cfg))
    return http.client.HTTPConnection(upstream_host(u), u.port or 80)


def inject_headers(cfg: dict, headers: list, hostname: str) -> list:
    """Drop hop-by-hop headers, point Host at the upstream, add auth + header_*."""
    out = [(k, v) for (k, v) in headers
           if k.lower() not in HOP_BY_HOP_REQUEST]
    out.append(("Host", hostname))

    auth = resolve_auth(cfg)
    if auth:
        out = [(k, v) for (k, v) in out if k.lower() != "authorization"]
        out.append(("Authorization", auth))

    for k, v in cfg.items():
        if not k.lower().startswith("header_"):
            continue
        name = k[len("header_"):]
        out = [(hk, hv) for (hk, hv) in out if hk.lower() != name.lower()]
        out.append((name, v))
    return out


def proxy(host: str, method: str, path: str,
          headers: list, body: bytes) -> None:
    cfg = load_backend(host)
    u = upstream_target(cfg, host)
    out_headers = inject_headers(cfg, headers, upstream_host(u))
    conn = connect_upstream(cfg, u)

    upstream_path = u.path.rstrip("/") + path
    conn.request(method, upstream_path, body=body or None,
                 headers=dict(out_headers))
    resp = conn.getresponse()

    out = sys.stdout.buffer
    out.write(f"HTTP/1.1 {resp.status} {resp.reason}\r\n".encode())
    for k, v in resp.getheaders():
        if k.lower() in HOP_BY_HOP_RESPONSE:
            continue
        out.write(f"{k}: {v}\r\n".encode())
    out.write(b"X-Handled-By: jci-proxy\r\n")
    out.write(b"Connection: close\r\n\r\n")
    out.flush()

    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
    conn.close()


def main() -> None:
    host = os.environ.get("INTERCEPT_HOST", "")
    try:
        method, path, headers, body = read_request(sys.stdin.buffer)
    except Exception as e:
        return write_error(400, f"bad request: {e}")
    try:
        proxy(host, method, path, headers, body)
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
        sys.stderr.write(f"[jci-proxy] {detail}\n")
        write_error(502, detail)


if __name__ == "__main__":
    main()
