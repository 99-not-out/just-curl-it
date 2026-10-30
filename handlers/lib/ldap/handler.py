#!/usr/bin/env python3
"""HTTP -> LDAP / Active Directory, read-only. Needs ldap3.

  GET /search?filter=(uid=ada)&base=ou=people,...&scope=sub|one|base&attrs=cn,mail&limit=100
  GET /find/<term>          (|(uid=term*)(cn=*term*)(mail=term*)(sAMAccountName=term*)) -- the quick lookup
  GET /entry/<dn>           one entry, all attributes
  GET /whoami               the identity the handler binds as
  GET /_info

Nothing here writes: no add, modify, delete or password routes. A GET-only
route and this handler are the same thing.
"""
from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, unquote, urlsplit

import jci

ROUTES = [
    "GET  /_info",
    "GET  /search?filter=(uid=x)&base=&scope=sub|one|base&attrs=cn,mail&limit=100",
    "GET  /find/{term}          (uid / cn / mail / sAMAccountName quick lookup)",
    "GET  /entry/{dn}",
    "GET  /whoami",
]


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def connect(cfg: dict):
    import ldap3
    url = cfg.get("url", "").strip()
    if not url:
        raise jci.BackendConfigError("missing 'url' (ldap://host:389 or ldaps://host:636) in backends.conf")
    tls = None
    if url.startswith("ldaps://") or _truthy(cfg.get("tls")) or cfg.get("ca_cert"):
        import ssl
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if cfg.get("ca_cert") else ssl.CERT_NONE,
                        ca_certs_file=cfg.get("ca_cert") or None)
    # ldap3 struct-packs these into SO_RCVTIMEO: they must be ints.
    t = max(1, int(jci.timeout(cfg)))
    server = ldap3.Server(url, get_info=ldap3.NONE, connect_timeout=t, tls=tls)
    password = jci.resolve_secret(cfg, "password")
    conn = ldap3.Connection(server, user=cfg.get("bind_dn") or None, password=password or None,
                            auto_bind=False, receive_timeout=max(10, t * 2),
                            read_only=True, raise_exceptions=True)
    conn.open()
    if _truthy(cfg.get("tls")) and not url.startswith("ldaps://"):
        conn.start_tls()
    conn.bind()
    return conn


def _value(v):
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(v).decode()}
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def entries(conn) -> list[dict]:
    out = []
    for e in conn.response or []:
        if e.get("type") != "searchResEntry":
            continue
        attrs = {}
        for k, vals in (e.get("attributes") or {}).items():
            if isinstance(vals, list):
                conv = [_value(v) for v in vals]
                attrs[k] = conv[0] if len(conv) == 1 else conv
            else:
                attrs[k] = _value(vals)
        out.append({"dn": e.get("dn"), "attrs": attrs})
    return out


def search(conn, base: str, flt: str, scope: str, attrs, limit: int) -> list[dict]:
    import ldap3
    scopes = {"sub": ldap3.SUBTREE, "one": ldap3.LEVEL, "base": ldap3.BASE}
    conn.search(search_base=base, search_filter=flt, search_scope=scopes.get(scope, ldap3.SUBTREE),
                attributes=attrs or ldap3.ALL_ATTRIBUTES, size_limit=limit)
    return entries(conn)


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("directory", ROUTES)
    if method != "GET":
        return jci.send_error(405, "this handler is read-only", allow=["GET"])

    cfg = jci.load_backend()
    base_default = cfg.get("base", "")
    limit = int(qs.get("limit", [cfg.get("limit", "100")])[0])
    attrs = [a for a in qs.get("attrs", [""])[0].split(",") if a]

    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException, LDAPInvalidFilterError, LDAPNoSuchObjectResult, \
            LDAPInvalidCredentialsResult, LDAPSocketOpenError, LDAPBindError, LDAPInvalidDnError
        from ldap3.utils.conv import escape_filter_chars
    except ImportError as e:
        return jci.send_error(500, f"ldap3 not importable: {e}", hint="jci handler install ldap")

    try:
        conn = connect(cfg)
    except (LDAPSocketOpenError, OSError) as e:
        return jci.send_error(502, f"cannot reach {cfg.get('url')}: {e}")
    except (LDAPInvalidCredentialsResult, LDAPBindError) as e:
        return jci.send_error(502, f"bind as {cfg.get('bind_dn')!r} refused: {e}")
    except LDAPException as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")

    try:
        if p == "/whoami":
            who = conn.extend.standard.who_am_i()
            return jci.send_json(200, {"whoami": who, "bind_dn": cfg.get("bind_dn")})

        if p == "/search":
            flt = qs.get("filter", ["(objectClass=*)"])[0]
            base = qs.get("base", [base_default])[0]
            if not base:
                return jci.send_error(400, "missing ?base= (or a `base` default in backends.conf)")
            try:
                res = search(conn, base, flt, qs.get("scope", ["sub"])[0], attrs, limit)
            except LDAPInvalidFilterError as e:
                return jci.send_error(400, f"bad filter {flt!r}: {e}")
            except LDAPNoSuchObjectResult:
                return jci.send_error(404, f"no such base: {base}")
            return jci.send_json(200, {"base": base, "filter": flt, "count": len(res), "entries": res})

        m = re.fullmatch(r"/find/(.+)", p)
        if m:
            raw = unquote(m.group(1))
            trailing = raw.endswith("*")
            term = escape_filter_chars(raw.rstrip("*"))
            if not term:
                return jci.send_error(400, "empty search term")
            star = "*"   # prefix matches on identifiers, substring on cn
            flt = (f"(|(uid={term}{star})(sAMAccountName={term}{star})(mail={term}{star})"
                   f"(cn=*{term}{star})(displayName=*{term}{star}))")
            if not base_default:
                return jci.send_error(400, "/find needs a `base` in backends.conf")
            res = search(conn, base_default, flt, "sub", attrs, limit)
            return jci.send_json(200, {"term": raw, "trailingWildcard": trailing, "count": len(res), "entries": res})

        m = re.fullmatch(r"/entry/(.+)", p)
        if m:
            dn = unquote(m.group(1))
            try:
                res = search(conn, dn, "(objectClass=*)", "base", attrs, 1)
            except (LDAPNoSuchObjectResult, LDAPInvalidDnError):
                res = []
            if not res:
                return jci.send_error(404, f"no entry {dn}")
            return jci.send_json(200, res[0])

        return jci.send_error(404, f"no route for {method} {p}")
    except LDAPException as e:
        return jci.send_error(502, f"{type(e).__name__}: {e}")
    finally:
        try:
            conn.unbind()
        except Exception:
            pass


if __name__ == "__main__":
    jci.main(route)
