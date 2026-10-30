#!/usr/bin/env python3
"""HTTP -> any GraphQL endpoint, with GET-able queries and schema discovery.

  GET  /gql?query=...&vars={json}&op=Name     run a query (queries only -- see below)
  POST /gql                                    body = {"query","variables","operationName"} JSON,
                                               or a raw GraphQL document; mutations allowed
  GET  /schema                                 root types + every type's name/kind/description
  GET  /types/{Name}                           one type: fields, args, enum values, interfaces

Linear, GitHub, Shopify, Hasura: every GraphQL API is `POST /graphql` with
a bearer token, which makes it invisible to a GET-only route. This handler
puts queries on GET and reads the document's own operation keyword to
refuse `mutation` / `subscription` there -- the protocol's declaration of
intent, not a guess about the body (ADR 0005). Auth is the proxy's:
`authorization*` and `header_*` keys in backends.conf.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import jci

ROUTES = [
    "GET  /_info",
    "GET  /gql?query=...&vars={json}&op=Name   (queries only; mutations are 405)",
    "POST /gql                                  (JSON {query,variables,operationName} or raw document)",
    "GET  /schema",
    "GET  /types/{Name}",
]

_OPS = ("query", "mutation", "subscription")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# operation type

def operation_types(doc: str) -> list[tuple[str, str | None]]:
    """[(type, name)] for each top-level operation definition, in order.
    Skips comments, strings (including block strings) and fragments, and
    tracks brace depth so selection sets never look like definitions. A
    bare `{ ... }` at the top level is the anonymous query shorthand."""
    ops: list[tuple[str, str | None]] = []
    depth = 0
    kw: str | None = None        # pending definition keyword at depth 0
    name: str | None = None
    skip_ident = False           # after '@' (directive) or 'on' (fragment type)
    i, n = 0, len(doc)
    while i < n:
        c = doc[i]
        if c == "#":
            while i < n and doc[i] != "\n":
                i += 1
            continue
        if c == '"':
            if doc.startswith('"""', i):
                j = doc.find('"""', i + 3)
                i = n if j < 0 else j + 3
            else:
                i += 1
                while i < n and doc[i] != '"':
                    i += 2 if doc[i] == "\\" else 1
                i += 1
            continue
        if c == "{":
            if depth == 0:
                if kw in _OPS:
                    ops.append((kw, name))
                elif kw is None:
                    ops.append(("query", None))
                kw, name, skip_ident = None, None, False
            depth += 1
            i += 1
            continue
        if c == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            if c == "@":
                skip_ident = True
                i += 1
                continue
            m = _IDENT.match(doc, i)
            if m:
                word = m.group(0)
                i = m.end()
                if skip_ident:
                    skip_ident = False
                elif kw is None:
                    kw = word if word in _OPS or word == "fragment" else kw
                elif name is None and word != "on":
                    name = word
                elif word == "on":
                    skip_ident = True
                continue
        i += 1
    return ops


def refuse_on_get(doc: str, op: str | None) -> str | None:
    """Reason to refuse this document on GET, or None if it's a query."""
    ops = operation_types(doc)
    if not ops:
        return "no operation found in the document"
    if op is not None:
        matching = [t for t, nm in ops if nm == op]
        if not matching:
            return f"no operation named {op!r} in the document"
        kind = matching[0]
    else:
        if len(ops) > 1:
            return "document has several operations; pass ?op=<name> to pick one"
        kind = ops[0][0]
    if kind != "query":
        return f"GET runs queries only; this document is a {kind}"
    return None


# ---------------------------------------------------------------------------
# upstream

def forward(cfg: dict, payload: dict):
    """POST the GraphQL payload upstream; returns the http.client response."""
    u = jci.upstream_target(cfg)
    default_port = 443 if u.scheme == "https" else 80
    authority = (u.hostname or "") + (f":{u.port}" if u.port and u.port != default_port else "")
    headers = dict(jci.inject_headers(cfg, {"content-type": "application/json",
                                            "accept": "application/json"}, authority))
    conn = jci.connect_upstream(cfg, u)
    body = json.dumps(payload).encode("utf-8")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    conn.request("POST", path, body=body, headers=headers)
    return conn, conn.getresponse()


def fetch(cfg: dict, query: str, variables: dict | None = None):
    conn, resp = forward(cfg, {"query": query, "variables": variables})
    try:
        raw = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"upstream HTTP {resp.status}: {raw[:300].decode('utf-8', 'replace')}")
    data = json.loads(raw)
    if data.get("errors") and not data.get("data"):
        raise RuntimeError("upstream errors: " + "; ".join(e.get("message", "?") for e in data["errors"]))
    return data.get("data") or {}


# ---------------------------------------------------------------------------
# schema

_TYPE_REF = ("kind name ofType { kind name ofType { kind name ofType { kind name "
             "ofType { kind name ofType { kind name } } } } }")
_SCHEMA_Q = ("query { __schema { queryType { name } mutationType { name } subscriptionType { name } "
             "types { kind name description } } }")
_TYPE_Q = ("query($n: String!) { __type(name: $n) { kind name description "
           "fields(includeDeprecated: true) { name description isDeprecated "
           f"args {{ name description defaultValue type {{ {_TYPE_REF} }} }} type {{ {_TYPE_REF} }} }} "
           f"inputFields {{ name description defaultValue type {{ {_TYPE_REF} }} }} "
           "enumValues(includeDeprecated: true) { name description isDeprecated } "
           "interfaces { name } possibleTypes { name } } }")


def render_type(ref) -> str:
    """{"kind":"NON_NULL","ofType":{"kind":"LIST",...}} -> "[String!]!"."""
    if not ref:
        return "?"
    kind = ref.get("kind")
    if kind == "NON_NULL":
        return render_type(ref.get("ofType")) + "!"
    if kind == "LIST":
        return "[" + render_type(ref.get("ofType")) + "]"
    return ref.get("name") or "?"


def _field(f: dict) -> dict:
    out = {"name": f["name"], "type": render_type(f.get("type"))}
    if f.get("description"):
        out["description"] = f["description"]
    if f.get("args"):
        out["args"] = [{"name": a["name"], "type": render_type(a.get("type")),
                        **({"default": a["defaultValue"]} if a.get("defaultValue") is not None else {})}
                       for a in f["args"]]
    if f.get("isDeprecated"):
        out["deprecated"] = True
    return out


def handle_schema(cfg: dict) -> None:
    s = fetch(cfg, _SCHEMA_Q).get("__schema") or {}
    root = {k: (s.get(k) or {}).get("name") for k in ("queryType", "mutationType", "subscriptionType")}
    types = [{"name": t["name"], "kind": t["kind"],
              **({"description": t["description"]} if t.get("description") else {})}
             for t in s.get("types", []) if not t["name"].startswith("__")]
    jci.send_json(200, {**root, "types": sorted(types, key=lambda t: t["name"]),
                        "hint": "GET /types/{Name} for a type's fields"})


def handle_type(cfg: dict, name: str) -> None:
    t = fetch(cfg, _TYPE_Q, {"n": name}).get("__type")
    if not t:
        return jci.send_error(404, f"no type named {name!r}", hint="GET /schema lists them")
    out = {"name": t["name"], "kind": t["kind"]}
    if t.get("description"):
        out["description"] = t["description"]
    if t.get("fields"):
        out["fields"] = [_field(f) for f in t["fields"]]
    if t.get("inputFields"):
        out["inputFields"] = [_field(f) for f in t["inputFields"]]
    if t.get("enumValues"):
        out["enumValues"] = [v["name"] for v in t["enumValues"]]
    if t.get("interfaces"):
        out["interfaces"] = [i["name"] for i in t["interfaces"]]
    if t.get("possibleTypes"):
        out["possibleTypes"] = [p["name"] for p in t["possibleTypes"]]
    jci.send_json(200, out)


# ---------------------------------------------------------------------------

def route(method: str, path: str, headers: dict, body: bytes) -> None:
    parsed = urlsplit(path)
    p = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)

    if method == "GET" and p == "/_info":
        return jci.info("graphql", ROUTES)

    cfg = jci.load_backend()

    if p == "/gql":
        if method == "GET":
            query = qs.get("query", [""])[0]
            if not query:
                return jci.send_error(400, "missing ?query=")
            op = qs.get("op", [None])[0]
            variables = None
            if qs.get("vars"):
                try:
                    variables = json.loads(qs["vars"][0])
                except ValueError as e:
                    return jci.send_error(400, f"vars is not JSON: {e}")
            why = refuse_on_get(query, op)
            if why:
                return jci.send_error(405, why, hint="POST /gql runs mutations", allow=["POST"])
            payload = {"query": query, "variables": variables, "operationName": op}
        elif method == "POST":
            if "json" in headers.get("content-type", "").lower():
                try:
                    payload = json.loads(body.decode("utf-8"))
                except ValueError as e:
                    return jci.send_error(400, f"body is not JSON: {e}")
                if not isinstance(payload, dict) or "query" not in payload:
                    return jci.send_error(400, 'JSON body needs a "query" field')
            else:
                payload = {"query": body.decode("utf-8")}
        else:
            return jci.send_error(405, "GET or POST", allow=["GET", "POST"])
        u = jci.upstream_target(cfg)
        conn, resp = forward(cfg, payload)
        try:
            jci.forward_response(resp, upstream_origin=f"{u.scheme}://{u.netloc}")
        finally:
            conn.close()
        return

    if method == "GET" and p == "/schema":
        return handle_schema(cfg)

    m = re.fullmatch(r"/types/([A-Za-z_][A-Za-z0-9_]*)", p)
    if m and method == "GET":
        return handle_type(cfg, m.group(1))

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
