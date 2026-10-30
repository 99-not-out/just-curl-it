"""Shared plumbing for jci handlers. Stdlib only; Python 3.9+.

A handler runs as a fresh process per request with stdin = the raw HTTP/1.x
request and stdout = the raw response (ADR 0002). These helpers hide the
line noise so a handler is just a `route(method, path, headers, body)`:

    import jci

    def route(method, path, headers, body):
        if path == "/_info":
            return jci.info("kv", ["GET /_info", "GET /kv/{k}"])
        ...

    if __name__ == "__main__":
        jci.main(route)

Method allow-lists are enforced by the shim before we are spawned (ADR
0004); nothing here checks them. `GET` handlers must not mutate (ADR 0005).
"""
from __future__ import annotations

import configparser
import http.client
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import traceback
import urllib.parse

HANDLER: str = os.environ.get("JCI_HANDLER", "handler")
HOST: str = os.environ.get("INTERCEPT_HOST", "")

_REASONS = {
    100: "Continue", 200: "OK", 201: "Created", 204: "No Content",
    302: "Found", 400: "Bad Request", 404: "Not Found",
    405: "Method Not Allowed", 413: "Content Too Large",
    500: "Internal Server Error", 501: "Not Implemented",
    502: "Bad Gateway", 503: "Service Unavailable",
}

HOP_BY_HOP_REQUEST = {
    "host", "connection", "proxy-connection", "keep-alive",
    "transfer-encoding", "upgrade", "te", "trailer", "expect",
}
HOP_BY_HOP_RESPONSE = {
    "connection", "keep-alive", "transfer-encoding",
    "upgrade", "trailer", "proxy-authenticate",
}


class BackendConfigError(RuntimeError):
    """backends.conf is missing, lacks our section, or a value is unusable."""


class TooLarge(ValueError):
    """Request body exceeds JCI_MAX_BODY."""


# ---------------------------------------------------------------------------
# request side

def _out():
    return sys.stdout.buffer


def _max_body() -> int:
    return int(os.environ.get("JCI_MAX_BODY", str(64 * 1024 * 1024)))


def _read_exact(stdin, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stdin.read(n - len(buf))
        if not chunk:
            raise ValueError(f"body truncated: wanted {n} bytes, got {len(buf)}")
        buf += chunk
    return bytes(buf)


def _read_chunked(stdin, limit: int) -> bytes:
    """RFC 9112 §7.1 chunked decoding; chunk extensions and trailers ignored."""
    body = bytearray()
    while True:
        line = stdin.readline()
        if not line:
            raise ValueError("chunked body truncated before a chunk-size line")
        size_tok = line.split(b";", 1)[0].strip()
        try:
            size = int(size_tok, 16)
        except ValueError:
            raise ValueError(f"bad chunk size: {size_tok!r}") from None
        if size == 0:
            # trailers until blank line
            while True:
                t = stdin.readline()
                if not t or t in (b"\r\n", b"\n"):
                    break
            return bytes(body)
        if len(body) + size > limit:
            raise TooLarge(f"body exceeds JCI_MAX_BODY={limit}")
        body += _read_exact(stdin, size)
        crlf = stdin.readline()
        if crlf not in (b"\r\n", b"\n"):
            raise ValueError("chunk not terminated by CRLF")


def read_request() -> tuple[str, str, dict[str, str], bytes]:
    """Parse the HTTP/1.x request on stdin.

    Header keys come back lowercased; repeated headers are joined with ", ".
    Honours Content-Length and Transfer-Encoding: chunked; answers
    `Expect: 100-continue` before reading the body. Raises ValueError on a
    malformed request, TooLarge over JCI_MAX_BODY.
    """
    stdin = sys.stdin.buffer
    req_line = stdin.readline()
    if not req_line:
        raise ValueError("empty request")
    parts = req_line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
    if len(parts) < 3 or not parts[2].startswith("HTTP/"):
        raise ValueError(f"bad request line: {req_line!r}")
    method, path, _ = parts

    headers: dict[str, str] = {}
    while True:
        line = stdin.readline()
        if not line:
            raise ValueError("headers truncated")
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("iso-8859-1").rstrip("\r\n")
        if ":" not in text:
            continue
        k, _, v = text.partition(":")
        k, v = k.strip().lower(), v.strip()
        headers[k] = f"{headers[k]}, {v}" if k in headers else v

    limit = _max_body()
    if headers.get("expect", "").lower() == "100-continue":
        _out().write(b"HTTP/1.1 100 Continue\r\n\r\n")
        _out().flush()

    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _read_chunked(stdin, limit)
    else:
        try:
            cl = int(headers.get("content-length", "0") or 0)
        except ValueError:
            raise ValueError(f"bad Content-Length: {headers['content-length']!r}") from None
        if cl > limit:
            raise TooLarge(f"body of {cl} bytes exceeds JCI_MAX_BODY={limit}")
        body = _read_exact(stdin, cl) if cl else b""
    return method, path, headers, body


# ---------------------------------------------------------------------------
# response side

def _sigpipe_default() -> None:
    """Restore default SIGPIPE so a client disconnect kills us cleanly."""
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, OSError, ValueError):
        pass


def _write_head(status: int, headers: list[tuple[str, str]]) -> None:
    out = _out()
    out.write(f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n".encode())
    for k, v in headers:
        out.write(f"{k}: {v}\r\n".encode("iso-8859-1"))
    out.write(f"X-Handled-By: jci-{HANDLER}\r\n".encode())
    out.write(b"Connection: close\r\n\r\n")


def send(status: int, body, content_type: str = "application/json") -> None:
    if isinstance(body, str):
        body = body.encode("utf-8")
    _write_head(status, [("Content-Type", content_type),
                         ("Content-Length", str(len(body)))])
    out = _out()
    out.write(body)
    out.flush()


def send_json(status: int, obj) -> None:
    send(status, json.dumps(obj, default=str), "application/json")


def send_error(status: int, msg: str, **extra) -> None:
    """Standard error body: {"error": msg, "handler": HANDLER, **extra}.
    An `allow` list also becomes an Allow: header."""
    body = json.dumps({"error": msg, "handler": HANDLER, **extra}, default=str).encode()
    headers = [("Content-Type", "application/json"),
               ("Content-Length", str(len(body)))]
    if extra.get("allow"):
        headers.append(("Allow", ", ".join(extra["allow"])))
    _write_head(status, headers)
    out = _out()
    out.write(body)
    out.flush()


def send_sse_headers() -> None:
    """Open a streaming Server-Sent Events response. No Content-Length;
    events are flushed until the client goes away (SIGPIPE kills us)."""
    _sigpipe_default()
    _write_head(200, [("Content-Type", "text/event-stream; charset=utf-8"),
                      ("Cache-Control", "no-cache")])
    _out().flush()


def sse_event(obj, event: str | None = None) -> None:
    out = _out()
    if event:
        out.write(f"event: {event}\n".encode())
    out.write(b"data: " + json.dumps(obj, default=str).encode("utf-8") + b"\n\n")
    out.flush()


def sse_comment(text: str) -> None:
    """SSE keepalive / status line. Clients ignore these."""
    out = _out()
    out.write(f": {text}\n\n".encode())
    out.flush()


# ---------------------------------------------------------------------------
# config

def load_backend(host: str | None = None) -> dict:
    """The [host] section of backends.conf as a dict, ${VAR}-expanded.
    No '%' interpolation, so passwords with '%' survive. A '#' preceded by
    whitespace starts a comment (`timeout = 5   # seconds`), as every
    handler README writes it; `a#b` is data, `a #b` is not."""
    host = host or HOST
    path = os.environ.get("INTERCEPT_BACKENDS_CONF", "/etc/intercept/backends.conf")
    if not os.path.exists(path):
        raise BackendConfigError(f"backends file not found: {path}")
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
    cp.read(path)
    if host not in cp:
        raise BackendConfigError(f"no [{host}] section in {path}")
    return {k: os.path.expandvars(v) for k, v in cp[host].items()}


def timeout(cfg: dict) -> float:
    """Connect timeout in seconds; every handler passes this to its client."""
    return float(cfg.get("timeout", 5))


_SECRET_KEY = re.compile(r"pass|secret|token|authorization|key|cookie|session", re.I)
_URL_USERINFO = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@]+@", re.I)
_KW_PASSWORD = re.compile(r"\b(password|passwd|pwd)=\S+", re.I)   # libpq keyword DSNs


def redact(cfg: dict) -> dict:
    """Copy of cfg safe to show: secret-looking keys become '***', URL
    userinfo becomes 'scheme://***@host', `password=x` inside a value
    becomes `password=***`."""
    out = {}
    for k, v in cfg.items():
        if _SECRET_KEY.search(k):
            out[k] = "***"
        elif isinstance(v, str) and _URL_USERINFO.match(v):
            out[k] = _URL_USERINFO.sub(r"\1***@", v)
        elif isinstance(v, str) and _KW_PASSWORD.search(v):
            out[k] = _KW_PASSWORD.sub(r"\1=***", v)
        else:
            out[k] = v
    return out


def info(shape, routes: list[str]) -> None:
    """The standard GET /_info body."""
    try:
        backend = redact(load_backend())
    except Exception as e:  # /_info must always answer
        backend = {"error": str(e)}
    send_json(200, {"handler": HANDLER, "shape": shape, "routes": routes,
                    "host": HOST, "backend": backend})


# ---------------------------------------------------------------------------
# upstream HTTP(S) -- shared by proxy-style handlers

def run_cmd(cmd: str, what: str) -> str:
    try:
        out = subprocess.check_output(["sh", "-c", cmd], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{what} failed: {e.stderr.decode().strip() or e}")
    return out.decode().strip()


def keychain_lookup(name: str, what: str) -> str:
    """macOS Keychain generic password with service `name`."""
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", name, "-w"],
            stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(f"{what} requires macOS security(1)")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"{what}: keychain lookup failed for {name!r}: {e.stderr.decode().strip() or e}")
    return out.decode().strip()


def resolve_secret(cfg: dict, key: str) -> str | None:
    """Value for `key` from `<key>` (literal, ${VAR}-expanded), `<key>_keychain`
    or `<key>_cmd`, in that order. None if none is configured."""
    if key in cfg:
        v = cfg[key].strip()
        if "${" in v:
            raise RuntimeError(
                f"{key} has unexpanded ${{...}}: {v!r} "
                f"-- the referenced env var isn't set in this shell")
        return v
    if f"{key}_keychain" in cfg:
        return keychain_lookup(cfg[f"{key}_keychain"], f"{key}_keychain")
    if f"{key}_cmd" in cfg:
        return run_cmd(cfg[f"{key}_cmd"], f"{key}_cmd")
    return None


def resolve_auth(cfg: dict) -> str | None:
    """Authorization header value from `authorization`, `authorization_keychain`
    (macOS) or `authorization_cmd`, in that order."""
    return resolve_secret(cfg, "authorization")


def _header_names(cfg: dict) -> list[str]:
    """Header names configured as header_<name>[_keychain|_cmd], in config order."""
    names: list[str] = []
    for k in cfg:
        lk = k.lower()
        if not lk.startswith("header_"):
            continue
        name = lk[len("header_"):]
        for suffix in ("_keychain", "_cmd"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        if name and name not in names:
            names.append(name)
    return names


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


def upstream_target(cfg: dict) -> urllib.parse.SplitResult:
    upstream = cfg.get("upstream")
    if not upstream:
        raise BackendConfigError(f"missing 'upstream' in [{HOST}]")
    u = urllib.parse.urlsplit(upstream)
    if u.scheme not in ("http", "https"):
        raise BackendConfigError(f"unsupported upstream scheme: {u.scheme}")
    if not u.hostname:
        raise BackendConfigError(f"upstream has no host: {upstream!r}")
    return u


def connect_upstream(cfg: dict, u: urllib.parse.SplitResult) -> http.client.HTTPConnection:
    host = u.hostname or ""  # upstream_target() already refused an empty host
    if u.scheme == "https":
        return http.client.HTTPSConnection(host, u.port or 443,
                                           context=tls_context(cfg),
                                           timeout=timeout(cfg))
    return http.client.HTTPConnection(host, u.port or 80, timeout=timeout(cfg))


def inject_headers(cfg: dict, headers: dict[str, str], hostname: str) -> list[tuple[str, str]]:
    """Drop hop-by-hop headers, point Host at the upstream, add auth + header_*.

    `header_<name>` is a literal (${VAR}-expanded); `header_<name>_keychain`
    and `header_<name>_cmd` resolve like `authorization_*` do, so an API that
    wants its key in `x-api-key` or `X-Vault-Token` gets the same secret
    sources. Configured headers replace any the client sent."""
    out = [(k, v) for k, v in headers.items() if k not in HOP_BY_HOP_REQUEST]
    out.append(("Host", hostname))
    auth = resolve_auth(cfg)
    if auth:
        out = [(k, v) for k, v in out if k.lower() != "authorization"]
        out.append(("Authorization", auth))
    lower_cfg = {k.lower(): v for k, v in cfg.items()}
    for name in _header_names(cfg):
        value = resolve_secret(lower_cfg, f"header_{name}")
        if value is None:
            continue
        out = [(hk, hv) for hk, hv in out if hk.lower() != name]
        out.append((name, value))
    return out


def forward_response(resp, upstream_origin: str | None = None) -> None:
    """Stream an http.client response to the client, minus hop-by-hop headers.
    With `upstream_origin` (e.g. "https://api.github.com"), a Location header
    pointing there is rewritten to http://<INTERCEPT_HOST> so `curl -L` stays
    inside the shim."""
    headers = []
    for k, v in resp.getheaders():
        if k.lower() in HOP_BY_HOP_RESPONSE:
            continue
        if (k.lower() == "location" and upstream_origin
                and v.startswith(upstream_origin)):
            v = f"http://{HOST}" + v[len(upstream_origin):]
        headers.append((k, v))
    _write_head(resp.status, headers)
    out = _out()
    out.flush()
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        out.write(chunk)
        out.flush()


# ---------------------------------------------------------------------------

def main(route) -> None:
    """Read the request, call route(method, path, headers, body), map errors."""
    try:
        method, path, headers, body = read_request()
    except TooLarge as e:
        return send_error(413, str(e))
    except ValueError as e:
        return send_error(400, f"bad request: {e}")
    try:
        route(method, path, headers, body)
    except BackendConfigError as e:
        send_error(500, f"config: {e}")
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_error(500, f"{type(e).__name__}: {e}")
