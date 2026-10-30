#!/usr/bin/env python3
"""HTTP -> memcached, over the text protocol. Stdlib only. The `kv` shape:

  GET    /keys?limit=1000      key names via `lru_crawler metadump all` (memcached >= 1.5)
  GET    /kv/<k>               get; 404 if missing
  PUT    /kv/<k>?ex=<seconds>  set; body = value (ex=0 or absent: no expiry)
  POST   /kv/<k>               same as PUT
  DELETE /kv/<k>               delete -> {"deleted": 0|1}
  GET    /stats                `stats` as a dict
"""
from __future__ import annotations

import re
import socket
from urllib.parse import parse_qs, unquote, urlsplit

import jci

ROUTES = [
    "GET    /_info",
    "GET    /keys?limit=1000       (lru_crawler metadump)",
    "GET    /kv/{k}",
    "PUT    /kv/{k}?ex=<seconds>   (body = value)",
    "POST   /kv/{k}                (same as PUT)",
    "DELETE /kv/{k}",
    "GET    /stats",
]

_KEY_OK = re.compile(rb"^[^\s\x00-\x1f\x7f]{1,250}$")


class Client:
    def __init__(self, cfg: dict):
        self.sock = socket.create_connection((cfg.get("host", "localhost"), int(cfg.get("port", 11211))),
                                             timeout=jci.timeout(cfg))
        self.buf = b""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _line(self) -> bytes:
        # Protocol replies end in \r\n, but `lru_crawler metadump` lines end
        # in a bare \n -- split on \n and drop a trailing \r.
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("memcached closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.rstrip(b"\r")

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("memcached closed the connection mid-value")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def get(self, key: bytes):
        self.sock.sendall(b"get " + key + b"\r\n")
        line = self._line()
        if line == b"END":
            return None
        parts = line.split()
        if parts[0] != b"VALUE":
            raise RuntimeError(line.decode("utf-8", "replace"))
        n = int(parts[3])
        data = self._exact(n)
        self._exact(2)        # trailing \r\n
        end = self._line()
        if end != b"END":
            raise RuntimeError(end.decode("utf-8", "replace"))
        return data

    def set(self, key: bytes, value: bytes, ex: int) -> None:
        self.sock.sendall(b"set " + key + b" 0 " + str(ex).encode() + b" " + str(len(value)).encode()
                          + b"\r\n" + value + b"\r\n")
        line = self._line()
        if line != b"STORED":
            raise RuntimeError(line.decode("utf-8", "replace"))

    def delete(self, key: bytes) -> bool:
        self.sock.sendall(b"delete " + key + b"\r\n")
        line = self._line()
        if line in (b"DELETED", b"NOT_FOUND"):
            return line == b"DELETED"
        raise RuntimeError(line.decode("utf-8", "replace"))

    def stats(self) -> dict:
        self.sock.sendall(b"stats\r\n")
        out = {}
        while True:
            line = self._line()
            if line == b"END":
                return out
            parts = line.decode("utf-8", "replace").split(" ", 2)
            if len(parts) == 3 and parts[0] == "STAT":
                v = parts[2]
                out[parts[1]] = int(v) if v.lstrip("-").isdigit() else v

    def keys(self, limit: int) -> list[str]:
        self.sock.sendall(b"lru_crawler metadump all\r\n")
        keys: list[str] = []
        while True:
            line = self._line()
            if line == b"END":
                return keys
            if line.startswith(b"key="):
                if len(keys) < limit:
                    keys.append(unquote(line.split(b" ", 1)[0][4:].decode("utf-8", "replace")))
            elif line.startswith(b"BUSY") or line.startswith(b"ERROR") or line.startswith(b"CLIENT_ERROR"):
                raise RuntimeError(line.decode("utf-8", "replace"))


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("kv", ROUTES)

    cfg = jci.load_backend()
    try:
        c = Client(cfg)
    except OSError as e:
        return jci.send_error(502, f"{type(e).__name__}: {e} ({cfg.get('host', 'localhost')}:{cfg.get('port', 11211)})")
    try:
        if method == "GET" and p == "/keys":
            keys = c.keys(int(qs.get("limit", ["1000"])[0]))
            return jci.send_json(200, {"count": len(keys), "keys": sorted(keys)})
        if method == "GET" and p == "/stats":
            return jci.send_json(200, c.stats())

        m = re.fullmatch(r"/kv/(.+)", p)
        if m:
            key = unquote(m.group(1)).encode("utf-8")
            if not _KEY_OK.match(key):
                return jci.send_error(400, "key must be 1-250 bytes with no whitespace or control characters")
            if method == "GET":
                val = c.get(key)
                if val is None:
                    return jci.send_error(404, "not found", key=key.decode("utf-8", "replace"))
                try:
                    return jci.send(200, val.decode("utf-8"), "text/plain; charset=utf-8")
                except UnicodeDecodeError:
                    return jci.send(200, val, "application/octet-stream")
            if method in ("PUT", "POST"):
                ex = int(qs.get("ex", ["0"])[0])
                c.set(key, body, ex)
                return jci.send_json(200, {"key": key.decode("utf-8", "replace"), "set": True,
                                           "bytes": len(body), **({"ex": ex} if ex else {})})
            if method == "DELETE":
                return jci.send_json(200, {"key": key.decode("utf-8", "replace"), "deleted": int(c.delete(key))})
    except (ConnectionError, socket.timeout) as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")
    except RuntimeError as e:
        return jci.send_error(400, f"memcached: {e}")
    finally:
        c.close()

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
