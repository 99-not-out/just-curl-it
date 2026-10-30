#!/usr/bin/env bash
# jci test suite. Covers the regressions we hit during development:
#   - build sanity
#   - routes.conf parsing (comments, methods detection, handler with spaces)
#   - end-to-end interception (curl -> sentinel IP -> spawned handler)
#   - method permissions, enforced in the shim (GET-only denies POST/DELETE
#     with an Allow header, and the handler is never spawned)
#   - SSE responses stream progressively instead of buffering to handler exit
#   - jci-run restores the shim in scripts whose shebang lost it to SIP
#   - auth proxy header injection (against httpbin.org if reachable)
#   - AF_INET6 with v4-mapped destinations (the Java HttpClient case)
#
# Usage: ./tests/run.sh
#        make test
set -u

# shellcheck source=tests/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# ---------------------------------------------------------------------------
section "build"
make -C "$REPO" build >/dev/null 2>&1 && ok "shim builds" || { bad "shim builds"; exit 1; }
[ -f "$DYLIB" ] && ok "$(basename "$DYLIB") exists" || { bad "$(basename "$DYLIB") exists"; exit 1; }

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/jci-tests.XXXXXX")
trap 'rm -rf "$TMPDIR"' EXIT

# ---------------------------------------------------------------------------
section "config parsing"

cat > "$TMPDIR/parse.conf" <<EOF
# this is a comment

test.plain        $REPO/handlers/run hello
test.gated  GET   $REPO/handlers/run hello
test.spaces       $REPO/handlers/run hello extra args here
test.long         /a/very/long/handler/path/that/goes/well/past/sixty/three/characters/in/total/run hello
test.longgated GET /a/very/long/handler/path/that/goes/well/past/sixty/three/characters/in/total/run hello
EOF

DEBUG=$(env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/parse.conf" \
        INTERCEPT_DEBUG=1 "$CURL" --max-time 1 http://nope.invalid/ 2>&1 || true)

assert_match "loads 5 routes (comment + blank line ignored)" "loaded 5 route" "$DEBUG"
assert_match "no methods -> [*]" "test.plain [*]" "$DEBUG"
assert_match "GET methods detected"        "test.gated [GET]" "$DEBUG"
assert_match "handler with spaces preserved" "extra args here" "$DEBUG"
# A handler path longer than 63 chars used to be split by sscanf's %63s and
# re-joined with a space -- the first token of the path became its own word.
assert_match "long handler path kept intact (no methods)" \
    "test.long [*] -> 127.0.0.5 via /a/very/long/handler/path/that/goes/well/past/sixty/three/characters/in/total/run hello" "$DEBUG"
assert_match "long handler path kept intact (with methods)" \
    "test.longgated [GET] -> 127.0.0.6 via /a/very/long/handler/path/that/goes/well/past/sixty/three/characters/in/total/run hello" "$DEBUG"

# ---------------------------------------------------------------------------
section "basic routing"

cat > "$TMPDIR/routes.conf" <<EOF
test.hello  $REPO/handlers/run hello
EOF

shim_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/routes.conf" \
        INTERCEPT_BACKENDS_CONF=/dev/null JCI_HANDLERS="$REPO/handlers" \
        "$CURL" "$@"
}

RESP=$(shim_curl -s http://test.hello/ping)
assert_match "handler responds"          '"handler":"hello.sh"'        "$RESP"
assert_match "INTERCEPT_HOST in response" '"host":"test.hello"'         "$RESP"
assert_match "request line preserved"     '"request":"GET /ping HTTP/1.1"' "$RESP"

# ---------------------------------------------------------------------------
section "run dispatcher"

# A copy of handlers/ so we can add a fixture handler that declares a
# dependency without a venv -- the 503 path.
HCOPY="$TMPDIR/handlers"
cp -R "$REPO/handlers" "$HCOPY"
mkdir -p "$HCOPY/lib/needsdeps"
printf 'import somepkg\n' > "$HCOPY/lib/needsdeps/handler.py"
printf 'somepkg>=1\n'      > "$HCOPY/lib/needsdeps/requirements.txt"

cat > "$TMPDIR/run.conf" <<EOF
test.hello   $HCOPY/run hello
test.meta    $HCOPY/run shim-meta
test.nope    $HCOPY/run no-such-handler
test.evil    $HCOPY/run ../lib/hello
test.needs   $HCOPY/run needsdeps
EOF
run_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/run.conf" \
        INTERCEPT_BACKENDS_CONF=/dev/null JCI_HANDLERS="$HCOPY" \
        "$CURL" -s "$@"
}
RESP=$(run_curl http://test.hello/ping)
assert_match "run hello -> bash handler ran"      '"handler":"hello.sh"' "$RESP"
RESP=$(run_curl -w '\n%{http_code}' http://test.meta/routes)
assert_eq    "run shim-meta /routes -> 200"       "200" "${RESP##*$'\n'}"
assert_match "shim-meta /routes shows methods"    '"methods"' "${RESP%$'\n'*}"
RESP=$(run_curl -w '\n%{http_code}' http://test.nope/)
assert_eq    "unknown handler -> 404"             "404" "${RESP##*$'\n'}"
assert_match "404 lists available handlers"       '"available"' "${RESP%$'\n'*}"
assert_match "404 list includes hello"            'hello' "${RESP%$'\n'*}"
mkdir -p "$HCOPY/lib/__pycache__" "$HCOPY/lib/notahandler"   # dirs with no entrypoint
RESP=$(run_curl http://test.nope/)
assert_no_match "404 list skips dirs without an entrypoint" 'notahandler' "$RESP"
assert_no_match "404 list skips __pycache__"                '__pycache__' "$RESP"
RESP=$(run_curl -w '\n%{http_code}' http://test.evil/)
assert_eq    "path-traversal name -> 404"         "404" "${RESP##*$'\n'}"
assert_match "traversal names the problem"        'invalid handler name' "${RESP%$'\n'*}"
RESP=$(run_curl -i http://test.needs/)
assert_match "deps without venv -> 503"           'HTTP/1.1 503' "$RESP"
assert_match "503 names the install command"      'jci handler install needsdeps' "$RESP"
assert_match "503 is from jci-run"                'X-Handled-By: jci-run' "$RESP"

# ---------------------------------------------------------------------------
section "jci handler"

# A throwaway JCI_HOME with the library copied in, plus a fixture handler
# that depends on a tiny pure-Python package we build as a wheel right here,
# so `jci handler install` is exercised with no network (PIP_NO_INDEX).
JH="$TMPDIR/home"
mkdir -p "$JH/handlers" "$TMPDIR/wheels"
cp -R "$REPO/handlers/lib" "$JH/handlers/lib"
cp "$REPO/handlers/run" "$JH/handlers/run"
mkdir -p "$JH/handlers/lib/withdeps"
cat > "$JH/handlers/lib/withdeps/handler.py" <<'PYEOF'
import jcitestpkg
import jci
def route(method, path, headers, body):
    jci.send_json(200, {"imported": jcitestpkg.__name__})
if __name__ == "__main__":
    jci.main(route)
PYEOF
printf 'jcitestpkg\n' > "$JH/handlers/lib/withdeps/requirements.txt"
printf '# withdeps\n\nFixture handler that needs a package.\n' > "$JH/handlers/lib/withdeps/README.md"
"${PY:-python3}" - "$TMPDIR/wheels" <<'PYEOF'
import sys, zipfile, hashlib, base64, os
out = os.path.join(sys.argv[1], "jcitestpkg-0.1-py3-none-any.whl")
files = {
    "jcitestpkg/__init__.py": b"__version__ = '0.1'\n",
    "jcitestpkg-0.1.dist-info/METADATA": b"Metadata-Version: 2.1\nName: jcitestpkg\nVersion: 0.1\n",
    "jcitestpkg-0.1.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: jci-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
}
def rec(name, data):
    h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={h},{len(data)}\n"
record = "".join(rec(n, d) for n, d in files.items()) + "jcitestpkg-0.1.dist-info/RECORD,,\n"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for n, d in files.items():
        z.writestr(n, d)
    z.writestr("jcitestpkg-0.1.dist-info/RECORD", record)
PYEOF

mkdir -p "$JH/handlers/lib/__pycache__" "$JH/handlers/lib/notahandler"   # not handlers
jh() { JCI_HOME="$JH" "$REPO/bin/jci" "$@"; }
LS=$(jh handler ls 2>&1)
assert_no_match "ls skips dirs without an entrypoint" "notahandler" "$LS"
assert_no_match "ls skips __pycache__"                "__pycache__" "$LS"
assert_match "ls shows hello ready"            "hello        ready"          "$LS"
assert_match "ls shows withdeps not installed" "withdeps     not installed" "$LS"
assert_match "ls shows deps column"            "jcitestpkg"                 "$LS"
assert_match "info prints the README"          "## backends.conf"           "$(jh handler info proxy 2>&1)"

INST=$(PIP_NO_INDEX=1 PIP_FIND_LINKS="$TMPDIR/wheels" jh handler install withdeps 2>&1); RC=$?
[ $RC -eq 0 ] && ok "install withdeps exits 0" || bad "install withdeps exits 0" "$INST"
[ -x "$JH/handlers/venv/withdeps/bin/python" ] && ok "install created the venv" || bad "install created the venv"
assert_match "ls shows withdeps installed"     "withdeps     installed"     "$(jh handler ls 2>&1)"

cat > "$TMPDIR/wd.conf" <<EOF
test.wd   $JH/handlers/run withdeps
EOF
wd_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/wd.conf" \
        INTERCEPT_BACKENDS_CONF=/dev/null JCI_HANDLERS="$JH/handlers" \
        "$CURL" -s -w '\n%{http_code}' http://test.wd/
}
RESP=$(wd_curl)
assert_eq    "installed handler runs under its venv -> 200" "200" "${RESP##*$'\n'}"
assert_match "handler imported the package"    '"imported": "jcitestpkg"' "${RESP%$'\n'*}"

OUT=$(jh handler install hello 2>&1)
assert_match "install of stdlib handler says ready" "ready (no dependencies)" "$OUT"
[ ! -e "$JH/handlers/venv/hello" ] && ok "no venv created for stdlib handler" || bad "venv wrongly created for hello"

jh handler uninstall withdeps >/dev/null 2>&1
[ ! -e "$JH/handlers/venv/withdeps" ] && ok "uninstall removed the venv" || bad "uninstall left the venv"
assert_match "ls shows withdeps not installed again" "withdeps     not installed" "$(jh handler ls 2>&1)"
RESP=$(wd_curl)
assert_eq    "uninstalled handler -> 503"      "503" "${RESP##*$'\n'}"

OUT=$(jh handler install nosuch 2>&1); RC=$?
[ $RC -ne 0 ] && ok "install nosuch exits non-zero" || bad "install nosuch exits non-zero"
assert_match "install nosuch names the problem" "no such handler" "$OUT"
# Names are validated like `run` does, so a typo can't become `rm -rf lib`.
OUT=$(jh handler uninstall ../lib 2>&1); RC=$?
[ $RC -ne 0 ] && ok "uninstall ../lib exits non-zero" || bad "uninstall ../lib exits non-zero" "$OUT"
[ -f "$JH/handlers/lib/jci.py" ] && ok "uninstall ../lib left the library intact" \
    || bad "uninstall ../lib deleted the library"
OUT=$(jh handler install ../lib 2>&1); RC=$?
[ $RC -ne 0 ] && ok "install ../lib exits non-zero" || bad "install ../lib exits non-zero" "$OUT"
assert_match "doctor lists venv handlers"      "handler withdeps:" "$(jh doctor 2>&1)"

# ---------------------------------------------------------------------------
section "handler: sqlite"

"${PY:-python3}" -c "import sqlite3; c=sqlite3.connect('$TMPDIR/t.db'); c.execute('create table t(x int)'); c.commit()"
cat > "$TMPDIR/sq.conf" <<EOF
test.sq      $REPO/handlers/run sqlite
test.sqro    $REPO/handlers/run sqlite
test.sqnone  $REPO/handlers/run sqlite
EOF
cat > "$TMPDIR/sq-backends.conf" <<EOF
[test.sq]
path = $TMPDIR/t.db
[test.sqro]
path = $TMPDIR/t.db
readonly = true
[test.sqnone]
path = $TMPDIR/missing.db
EOF
sq_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/sq.conf" \
        INTERCEPT_BACKENDS_CONF="$TMPDIR/sq-backends.conf" JCI_HANDLERS="$REPO/handlers" \
        "$CURL" -s -w '\n%{http_code}' "$@"
}
RESP=$(sq_curl http://test.sq/tables)
assert_eq    "sqlite /tables -> 200"                 "200" "${RESP##*$'\n'}"
assert_match "sqlite /tables lists t"                '"t"' "${RESP%$'\n'*}"
RESP=$(sq_curl -G http://test.sq/query --data-urlencode 'sql=select count(*) from t')
assert_match "sqlite GET select -> 0 rows"           '"rows": [[0]]' "${RESP%$'\n'*}"
RESP=$(sq_curl -G http://test.sq/query --data-urlencode 'sql=insert into t values (1)')
assert_eq    "sqlite GET insert -> 405"              "405" "${RESP##*$'\n'}"
assert_match "sqlite GET insert points at POST"      '"allow": ["POST"]' "${RESP%$'\n'*}"
RESP=$(sq_curl -X POST http://test.sq/query -d 'insert into t values (1)')
assert_eq    "sqlite POST insert -> 200"             "200" "${RESP##*$'\n'}"
assert_match "sqlite POST insert rowCount 1"         '"rowCount": 1' "${RESP%$'\n'*}"
RESP=$(sq_curl -G http://test.sq/query --data-urlencode 'sql=select count(*) from t')
assert_match "sqlite GET select sees the row"        '"rows": [[1]]' "${RESP%$'\n'*}"
RESP=$(sq_curl -X POST http://test.sqro/query -d 'insert into t values (2)')
assert_eq    "sqlite readonly=true POST -> 405"      "405" "${RESP##*$'\n'}"
assert_match "sqlite readonly=true names the key"    'readonly' "${RESP%$'\n'*}"
# Hostile GETs (ADR 0005): statements that write to the *filesystem* even
# when the database itself is opened read-only.
RESP=$(sq_curl -G http://test.sq/query --data-urlencode "sql=VACUUM INTO '$TMPDIR/vac.db'")
[ "${RESP##*$'\n'}" != "200" ] && ok "sqlite GET VACUUM INTO refused (${RESP##*$'\n'})" \
    || bad "sqlite GET VACUUM INTO -> 200" "${RESP%$'\n'*}"
[ ! -e "$TMPDIR/vac.db" ] && ok "sqlite GET VACUUM INTO wrote nothing" || bad "sqlite GET VACUUM INTO created $TMPDIR/vac.db"
RESP=$(sq_curl -G http://test.sq/query --data-urlencode "sql=ATTACH '$TMPDIR/att.db' AS n")
[ "${RESP##*$'\n'}" != "200" ] && ok "sqlite GET ATTACH refused (${RESP##*$'\n'})" \
    || bad "sqlite GET ATTACH -> 200" "${RESP%$'\n'*}"
[ ! -e "$TMPDIR/att.db" ] && ok "sqlite GET ATTACH wrote nothing" || bad "sqlite GET ATTACH created $TMPDIR/att.db"
# A write with RETURNING has a result set; it must still be committed.
RESP=$(sq_curl -X POST http://test.sq/query -d 'insert into t values (9) returning x')
assert_match "sqlite POST insert..returning -> rows"  '"rows": [[9]]' "${RESP%$'\n'*}"
RESP=$(sq_curl -G http://test.sq/query --data-urlencode 'sql=select count(*) from t where x = 9')
assert_match "sqlite RETURNING write was committed"   '"rows": [[1]]' "${RESP%$'\n'*}"
RESP=$(sq_curl http://test.sqnone/tables)
assert_eq    "sqlite missing file -> 502"            "502" "${RESP##*$'\n'}"
assert_match "sqlite missing file named"             'missing.db' "${RESP%$'\n'*}"
[ ! -e "$TMPDIR/missing.db" ] && ok "sqlite never creates a missing database" \
    || bad "sqlite created $TMPDIR/missing.db"

# ---------------------------------------------------------------------------
section "handler: unix-http"

# An HTTP server on a Unix socket that echoes what it received -- the shape
# of the Docker / Podman / systemd socket APIs.
cat > "$TMPDIR/uxserver.py" <<'PYEOF'
import http.server, json, os, socketserver, sys

class H(http.server.BaseHTTPRequestHandler):
    def _echo(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        out = json.dumps({"method": self.command, "path": self.path, "body": body,
                          "x-tag": self.headers.get("x-tag"),
                          "host": self.headers.get("host")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = do_PUT = do_DELETE = _echo
    def address_string(self): return "unix"
    def log_message(self, *a): pass

class S(socketserver.UnixStreamServer):
    allow_reuse_address = True

path = sys.argv[1]
if os.path.exists(path): os.unlink(path)
S(path, H).serve_forever()
PYEOF
"${PY:-python3}" "$TMPDIR/uxserver.py" "$TMPDIR/s.sock" &
UX_PID=$!
for _ in $(seq 1 50); do [ -S "$TMPDIR/s.sock" ] && break; sleep 0.1; done

cat > "$TMPDIR/ux.conf" <<EOF
test.ux      $REPO/handlers/run unix-http
test.uxnone  $REPO/handlers/run unix-http
test.uxfile  $REPO/handlers/run unix-http
EOF
printf 'not a socket\n' > "$TMPDIR/plain.file"
cat > "$TMPDIR/ux-backends.conf" <<EOF
[test.ux]
socket = $TMPDIR/s.sock
[test.uxnone]
socket = $TMPDIR/none.sock
[test.uxfile]
socket = $TMPDIR/plain.file
EOF
ux_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/ux.conf" \
        INTERCEPT_BACKENDS_CONF="$TMPDIR/ux-backends.conf" JCI_HANDLERS="$REPO/handlers" \
        "$CURL" -s -w '\n%{http_code}' "$@"
}
RESP=$(ux_curl -X POST 'http://test.ux/a/b?c=1' -H 'X-Tag: t' -d hi)
assert_eq    "unix-http POST -> 200"                  "200" "${RESP##*$'\n'}"
assert_match "unix-http method forwarded"             '"method": "POST"' "${RESP%$'\n'*}"
assert_match "unix-http path+query forwarded"         '"path": "/a/b?c=1"' "${RESP%$'\n'*}"
assert_match "unix-http body forwarded"               '"body": "hi"' "${RESP%$'\n'*}"
assert_match "unix-http header forwarded"             '"x-tag": "t"' "${RESP%$'\n'*}"
RESP=$(ux_curl http://test.uxnone/)
assert_eq    "unix-http missing socket -> 502"        "502" "${RESP##*$'\n'}"
assert_match "unix-http missing socket named"         'none.sock' "${RESP%$'\n'*}"
RESP=$(ux_curl http://test.uxfile/)
assert_eq    "unix-http non-socket path -> 502"       "502" "${RESP##*$'\n'}"
assert_match "unix-http non-socket path named"        'plain.file' "${RESP%$'\n'*}"
kill "$UX_PID" 2>/dev/null; wait "$UX_PID" 2>/dev/null

# ---------------------------------------------------------------------------
section "handler: graphql"

# A fake GraphQL endpoint: answers introspection with a two-type schema,
# `hello` and `setHello`, and echoes what it received under extensions.
cat > "$TMPDIR/gqlserver.py" <<'PYEOF'
import http.server, json, sys
SCHEMA = {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "subscriptionType": None,
  "types": [
    {"kind": "OBJECT", "name": "Query", "description": "root", "fields": [
        {"name": "hello", "description": "greets", "args": [
            {"name": "name", "type": {"kind": "SCALAR", "name": "String", "ofType": None}}],
         "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "String", "ofType": None}}}],
     "inputFields": None, "enumValues": None, "interfaces": []},
    {"kind": "OBJECT", "name": "Mutation", "description": None, "fields": [
        {"name": "setHello", "description": None, "args": [
            {"name": "v", "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "String", "ofType": None}}}],
         "type": {"kind": "SCALAR", "name": "String", "ofType": None}}],
     "inputFields": None, "enumValues": None, "interfaces": []},
    {"kind": "SCALAR", "name": "String", "description": "text", "fields": None, "inputFields": None, "enumValues": None, "interfaces": None},
    {"kind": "OBJECT", "name": "__Schema", "description": "meta", "fields": [], "inputFields": None, "enumValues": None, "interfaces": []},
  ]}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        ct = self.headers.get("content-type", "")
        if "json" in ct:
            req = json.loads(raw)
        else:
            req = {"query": raw.decode(), "variables": None, "operationName": None, "raw_content_type": ct}
        q = req.get("query") or ""
        if "__schema" in q:
            data = {"__schema": SCHEMA}
        elif "__type" in q:
            name = (req.get("variables") or {}).get("n")
            data = {"__type": next((t for t in SCHEMA["types"] if t["name"] == name), None)}
        elif "setHello" in q:
            data = {"setHello": "ok"}
        else:
            data = {"hello": "world"}
        body = json.dumps({"data": data, "extensions": {"received": {
            "query": q, "variables": req.get("variables"), "operationName": req.get("operationName"),
            "headers": {k.lower(): v for k, v in self.headers.items()}}}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_port, flush=True)
srv.serve_forever()
PYEOF
mkfifo "$TMPDIR/gqlport"
"${PY:-python3}" "$TMPDIR/gqlserver.py" > "$TMPDIR/gqlport" 2>"$TMPDIR/gqlserver.err" &
GQL_PID=$!
read -r GQL_PORT < "$TMPDIR/gqlport"

cat > "$TMPDIR/gql.conf" <<EOF
test.gql      $REPO/handlers/run graphql
test.gqlro    GET   $REPO/handlers/run graphql
EOF
cat > "$TMPDIR/gql-backends.conf" <<EOF
[test.gql]
upstream       = http://127.0.0.1:$GQL_PORT/graphql
header_x-token = t1
[test.gqlro]
upstream       = http://127.0.0.1:$GQL_PORT/graphql
header_x-token = t1
EOF
gql_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/gql.conf" \
        INTERCEPT_BACKENDS_CONF="$TMPDIR/gql-backends.conf" JCI_HANDLERS="$REPO/handlers" \
        "$CURL" -s --max-time 10 -w '\n%{http_code}' "$@"
}
RESP=$(gql_curl -G http://test.gql/gql --data-urlencode 'query={ hello }')
assert_eq    "graphql GET /gql -> 200"                       "200" "${RESP##*$'\n'}"
assert_match "graphql GET /gql returns data"                 '"hello": "world"' "${RESP%$'\n'*}"
assert_match "graphql configured header reached upstream"    '"x-token": "t1"' "${RESP%$'\n'*}"
RESP=$(gql_curl -G http://test.gql/gql --data-urlencode 'query=query A($n: String) { hello(name: $n) } mutation B { setHello(v: "x") }' \
       --data-urlencode 'vars={"n":"ada"}' --data-urlencode 'op=A')
assert_eq    "graphql GET with vars+op -> 200"               "200" "${RESP##*$'\n'}"
assert_match "graphql variables forwarded"                   '"variables": {"n": "ada"}' "${RESP%$'\n'*}"
assert_match "graphql operationName forwarded"               '"operationName": "A"' "${RESP%$'\n'*}"
RESP=$(gql_curl -G http://test.gql/gql --data-urlencode 'query=mutation { setHello(v: "x") }')
assert_eq    "graphql GET mutation -> 405 (even on an open route)" "405" "${RESP##*$'\n'}"
assert_match "graphql GET mutation points at POST"           '"allow": ["POST"]' "${RESP%$'\n'*}"
RESP=$(gql_curl -X POST http://test.gql/gql -H 'content-type: application/json' \
       -d '{"query":"mutation { setHello(v: \"x\") }"}')
assert_eq    "graphql POST mutation -> 200"                  "200" "${RESP##*$'\n'}"
assert_match "graphql POST mutation ran"                     '"setHello": "ok"' "${RESP%$'\n'*}"
RESP=$(gql_curl -X POST http://test.gql/gql -H 'content-type: application/graphql' -d '{ hello }')
assert_match "graphql POST raw document is wrapped as JSON"  '"query": "{ hello }"' "${RESP%$'\n'*}"
RESP=$(gql_curl -X POST http://test.gqlro/gql -d '{"query":"mutation { setHello(v: \"x\") }"}')
assert_eq    "graphql POST on GET-only route -> 405 from the shim" "405" "${RESP##*$'\n'}"
RESP=$(gql_curl http://test.gql/schema)
assert_eq    "graphql GET /schema -> 200"                    "200" "${RESP##*$'\n'}"
assert_match "graphql /schema names the query type"          '"queryType": "Query"' "${RESP%$'\n'*}"
assert_match "graphql /schema lists Mutation"                '"Mutation"' "${RESP%$'\n'*}"
assert_no_match "graphql /schema hides __ types"             '__Schema' "${RESP%$'\n'*}"
RESP=$(gql_curl http://test.gql/types/Query)
assert_match "graphql /types/Query renders field type"       '"type": "String!"' "${RESP%$'\n'*}"
assert_match "graphql /types/Query renders args"             '"name": "name"' "${RESP%$'\n'*}"
RESP=$(gql_curl http://test.gql/types/Nope)
assert_eq    "graphql /types/Nope -> 404"                    "404" "${RESP##*$'\n'}"
RESP=$(gql_curl http://test.gql/gql)
assert_eq    "graphql GET /gql without query -> 400"         "400" "${RESP##*$'\n'}"
kill "$GQL_PID" 2>/dev/null; wait "$GQL_PID" 2>/dev/null

# ---------------------------------------------------------------------------
section "spec-aware /_info"

# `openapi =` on a proxy route turns /_info into the upstream's operation
# menu; nothing contacts the upstream for that.
cat > "$TMPDIR/pets.json" <<'EOF'
{"openapi":"3.0.3","info":{"title":"Pets","version":"1"},
 "paths":{"/pets":{"get":{"summary":"List pets"},"post":{"summary":"Create a pet"}},
          "/pets/{id}":{"get":{"summary":"One pet"}}}}
EOF
cat > "$TMPDIR/spec.conf" <<EOF
test.pets      $REPO/handlers/run proxy
test.badspec   $REPO/handlers/run proxy
EOF
cat > "$TMPDIR/spec-backends.conf" <<EOF
[test.pets]
upstream = http://127.0.0.1:1
openapi  = $TMPDIR/pets.json
[test.badspec]
upstream = http://127.0.0.1:1
openapi  = $TMPDIR/does-not-exist.json
EOF
spec_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/spec.conf" \
        INTERCEPT_BACKENDS_CONF="$TMPDIR/spec-backends.conf" JCI_HANDLERS="$REPO/handlers" \
        "$CURL" -s --max-time 10 -w '\n%{http_code}' "$@"
}
RESP=$(spec_curl http://test.pets/_info)
assert_eq    "openapi /_info -> 200"                         "200" "${RESP##*$'\n'}"
assert_match "openapi /_info carries the spec title"         '"title": "Pets"' "${RESP%$'\n'*}"
assert_match "openapi /_info lists an operation"             '"summary": "Create a pet"' "${RESP%$'\n'*}"
assert_match "openapi /_info gives an example curl on our host" 'curl http://test.pets/pets/{id}' "${RESP%$'\n'*}"
RESP=$(spec_curl http://test.badspec/_info)
assert_eq    "missing spec: /_info still answers 200"        "200" "${RESP##*$'\n'}"
assert_match "missing spec is reported in the api section"   'does-not-exist.json' "${RESP%$'\n'*}"

# ---------------------------------------------------------------------------
section "handler: bigquery"

# A fake BigQuery REST API: requires Bearer tok-1, classifies dry runs by
# the statement's first word, pages a 'page' query, and makes a 'slow'
# query incomplete on the first call so the handler has to poll.
cat > "$TMPDIR/bqserver.py" <<'PYEOF'
import http.server, json, re
STATE = {"slow_calls": 0}
def classify(q):
    w = (q.strip().split() or ["?"])[0].upper()
    return {"SELECT": "SELECT", "WITH": "SELECT", "CREATE": "CREATE_TABLE", "INSERT": "INSERT",
            "DELETE": "DELETE", "DROP": "DROP_TABLE", "MERGE": "MERGE"}.get(w, "SCRIPT")
INT = {"name": "n", "type": "INTEGER"}
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _auth(self):
        if self.headers.get("authorization") != "Bearer tok-1":
            self._send(401, {"error": {"code": 401, "message": "Request had invalid authentication credentials."}})
            return False
        return True
    def do_GET(self):
        if not self._auth(): return
        if re.fullmatch(r"/bigquery/v2/projects/p1/datasets(\?.*)?", self.path):
            return self._send(200, {"datasets": [{"datasetReference": {"datasetId": "d1"}}, {"datasetReference": {"datasetId": "d2"}}]})
        if re.fullmatch(r"/bigquery/v2/projects/p1/datasets/d1/tables(\?.*)?", self.path):
            return self._send(200, {"tables": [{"tableReference": {"tableId": "t1"}, "type": "TABLE"}]})
        m = re.fullmatch(r"/bigquery/v2/projects/p1/queries/(job-[a-z]+)(\?.*)?", self.path)
        if m:
            job = m.group(1)
            if job == "job-page" and "pageToken=pt1" in self.path:
                return self._send(200, {"jobComplete": True, "schema": {"fields": [INT]}, "rows": [{"f": [{"v": "3"}]}]})
            if job == "job-slow":
                return self._send(200, {"jobComplete": True, "schema": {"fields": [INT]}, "rows": [{"f": [{"v": "7"}]}], "totalRows": "1"})
        return self._send(404, {"error": {"message": "not found " + self.path}})
    def do_POST(self):
        if not self._auth(): return
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/bigquery/v2/projects/p1/jobs"):
            q = req.get("configuration", {}).get("query", {}).get("query", "")
            if req.get("configuration", {}).get("dryRun"):
                return self._send(200, {"jobReference": {"jobId": "dry"}, "statistics": {
                    "query": {"statementType": classify(q)}, "totalBytesProcessed": "4096"}})
            return self._send(400, {"error": {"message": "fixture only supports dry runs on jobs.insert"}})
        if self.path.startswith("/bigquery/v2/projects/p1/queries"):
            q = req.get("query", "")
            if "page" in q:
                return self._send(200, {"jobComplete": True, "jobReference": {"jobId": "job-page"},
                                        "schema": {"fields": [INT]}, "rows": [{"f": [{"v": "1"}]}, {"f": [{"v": "2"}]}],
                                        "pageToken": "pt1", "totalRows": "3"})
            if "slow" in q:
                return self._send(200, {"jobComplete": False, "jobReference": {"jobId": "job-slow", "location": "EU"}})
            if "boom" in q:
                return self._send(400, {"error": {"code": 400, "message": "Syntax error: Unexpected keyword BOOM at [1:8]"}})
            if classify(q) != "SELECT":
                return self._send(200, {"jobComplete": True, "jobReference": {"jobId": "job-ddl"},
                                        "numDmlAffectedRows": "0", "totalRows": "0"})
            return self._send(200, {"jobComplete": True, "jobReference": {"jobId": "job-sel"},
                                    "schema": {"fields": [INT, {"name": "s", "type": "STRING"}]},
                                    "rows": [{"f": [{"v": "1"}, {"v": "one"}]}], "totalRows": "1",
                                    "totalBytesProcessed": "0"})
        return self._send(404, {"error": {"message": "not found " + self.path}})
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_port, flush=True)
srv.serve_forever()
PYEOF
mkfifo "$TMPDIR/bqport"
"${PY:-python3}" "$TMPDIR/bqserver.py" > "$TMPDIR/bqport" 2>"$TMPDIR/bqserver.err" &
BQ_PID=$!
read -r BQ_PORT < "$TMPDIR/bqport"
cat > "$TMPDIR/bq.conf" <<EOF
test.bq     $REPO/handlers/run bigquery
test.bqbad  $REPO/handlers/run bigquery
EOF
cat > "$TMPDIR/bq-backends.conf" <<EOF
[test.bq]
upstream  = http://127.0.0.1:$BQ_PORT
project   = p1
token_cmd = printf tok-1
[test.bqbad]
upstream  = http://127.0.0.1:$BQ_PORT
project   = p1
token     = wrong
EOF
bq_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/bq.conf" \
        INTERCEPT_BACKENDS_CONF="$TMPDIR/bq-backends.conf" JCI_HANDLERS="$REPO/handlers" \
        "$CURL" -s --max-time 20 -w '\n%{http_code}' "$@"
}
RESP=$(bq_curl -G http://test.bq/query --data-urlencode 'sql=select 1 as n, "one" as s')
assert_eq    "bigquery GET select -> 200"                    "200" "${RESP##*$'\n'}"
assert_match "bigquery GET select typed rows"                '"rows": [[1, "one"]]' "${RESP%$'\n'*}"
assert_match "bigquery GET reports the statement type"       '"statementType": "SELECT"' "${RESP%$'\n'*}"
assert_match "bigquery GET reports the dry-run estimate"     '"estimatedBytes": 4096' "${RESP%$'\n'*}"
RESP=$(bq_curl -G http://test.bq/query --data-urlencode 'sql=create table d1.t2 (x int64)')
assert_eq    "bigquery GET create -> 405"                    "405" "${RESP##*$'\n'}"
assert_match "bigquery GET create names the statement type" 'CREATE_TABLE' "${RESP%$'\n'*}"
RESP=$(bq_curl -G http://test.bq/query --data-urlencode 'sql=insert into d1.t1 values (1)')
assert_eq    "bigquery GET insert -> 405"                    "405" "${RESP##*$'\n'}"
RESP=$(bq_curl -X POST http://test.bq/query -d 'create table d1.t2 (x int64)')
assert_eq    "bigquery POST create -> 200"                   "200" "${RESP##*$'\n'}"
assert_match "bigquery POST create reports the job"          '"jobId": "job-ddl"' "${RESP%$'\n'*}"
RESP=$(bq_curl -G http://test.bq/query --data-urlencode 'sql=select page')
assert_match "bigquery GET follows pageToken"                '"rows": [[1], [2], [3]]' "${RESP%$'\n'*}"
RESP=$(bq_curl -G http://test.bq/query --data-urlencode 'sql=select slow')
assert_match "bigquery GET polls an incomplete job"          '"rows": [[7]]' "${RESP%$'\n'*}"
RESP=$(bq_curl -G http://test.bq/query --data-urlencode 'sql=select boom')
assert_eq    "bigquery GET syntax error -> 400"              "400" "${RESP##*$'\n'}"
assert_match "bigquery GET syntax error carries the message" 'Unexpected keyword BOOM' "${RESP%$'\n'*}"
RESP=$(bq_curl http://test.bq/datasets)
assert_match "bigquery GET /datasets"                        '"datasets": ["d1", "d2"]' "${RESP%$'\n'*}"
RESP=$(bq_curl 'http://test.bq/tables?dataset=d1')
assert_match "bigquery GET /tables?dataset="                 '"t1"' "${RESP%$'\n'*}"
RESP=$(bq_curl http://test.bqbad/datasets)
assert_eq    "bigquery bad token -> 502"                     "502" "${RESP##*$'\n'}"
assert_match "bigquery bad token carries Google's message"   'invalid authentication' "${RESP%$'\n'*}"
kill "$BQ_PID" 2>/dev/null; wait "$BQ_PID" 2>/dev/null

# ---------------------------------------------------------------------------
section "handler: athena"

# A fake Athena JSON API that *recomputes the SigV4 signature* of every
# request with jci_aws (so the signer is verified end to end, not just
# shape-checked), then plays the StartQueryExecution / GetQueryExecution /
# GetQueryResults state machine.
cat > "$TMPDIR/athenaserver.py" <<'PYEOF'
import datetime, http.server, json, re, sys
sys.path.insert(0, sys.argv[1]); import jci_aws
CREDS = jci_aws.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", None)
Q = {}
def kind(sql):
    w = (sql.strip().split() or ["?"])[0].upper()
    return "DDL" if w in ("CREATE", "DROP", "ALTER") else ("UTILITY" if w in ("SHOW", "DESCRIBE") else "DML")
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/x-amz-json-1.1")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _verify(self, body):
        authz = self.headers.get("authorization", "")
        m = re.search(r"SignedHeaders=([^,]+), Signature=([0-9a-f]+)", authz)
        if not m or not authz.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"):
            return "no SigV4 authorization"
        signed = {h: self.headers.get(h, "") for h in m.group(1).split(";")}
        now = datetime.datetime.strptime(self.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        creq, sh = jci_aws.canonical_request("POST", self.path, "", signed, body, "athena")
        sts = jci_aws.string_to_sign(now, "eu-west-1", "athena", creq)
        expect = jci_aws.authorization(CREDS, now, "eu-west-1", "athena", sh, sts)
        return None if expect == authz else "signature mismatch"
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n)
        bad = self._verify(body)
        if bad:
            return self._send(403, {"__type": "InvalidSignatureException", "message": bad})
        target = self.headers.get("x-amz-target", "").split(".")[-1]
        req = json.loads(body or b"{}")
        if target == "StartQueryExecution":
            qid = f"q-{len(Q)+1}"; Q[qid] = {"sql": req["QueryString"], "polls": 0, "ctx": req.get("QueryExecutionContext", {})}
            return self._send(200, {"QueryExecutionId": qid})
        if target == "GetQueryExecution":
            q = Q[req["QueryExecutionId"]]; q["polls"] += 1
            if "fail" in q["sql"]:
                st = {"State": "FAILED", "StateChangeReason": "SYNTAX_ERROR: line 1:8: mismatched input"}
            elif "slow" in q["sql"] and q["polls"] < 3:
                st = {"State": "RUNNING"}
            else:
                st = {"State": "SUCCEEDED"}
            return self._send(200, {"QueryExecution": {"QueryExecutionId": req["QueryExecutionId"],
                "Status": st, "StatementType": kind(q["sql"]), "Query": q["sql"],
                "QueryExecutionContext": q["ctx"], "Statistics": {"DataScannedInBytes": 2048, "TotalExecutionTimeInMillis": 120}}})
        if target == "GetQueryResults":
            q = Q[req["QueryExecutionId"]]
            if kind(q["sql"]) != "DML":
                return self._send(200, {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": []}, "Rows": []}})
            meta = {"ColumnInfo": [{"Name": "n", "Type": "bigint"}, {"Name": "s", "Type": "varchar"}]}
            hdr = {"Data": [{"VarCharValue": "n"}, {"VarCharValue": "s"}]}
            if "page" in q["sql"]:
                if req.get("NextToken") == "nt1":
                    return self._send(200, {"ResultSet": {"ResultSetMetadata": meta, "Rows": [{"Data": [{"VarCharValue": "3"}, {"VarCharValue": "c"}]}]}})
                return self._send(200, {"ResultSet": {"ResultSetMetadata": meta, "Rows": [hdr, {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "a"}]}, {"Data": [{"VarCharValue": "2"}, {"VarCharValue": "b"}]}]}, "NextToken": "nt1"})
            return self._send(200, {"ResultSet": {"ResultSetMetadata": meta, "Rows": [hdr, {"Data": [{"VarCharValue": "7"}, {"VarCharValue": "seven"}]}]}})
        if target == "ListDatabases":
            return self._send(200, {"DatabaseList": [{"Name": "db1"}, {"Name": "db2"}]})
        if target == "ListTableMetadata":
            return self._send(200, {"TableMetadataList": [{"Name": "t1", "TableType": "EXTERNAL_TABLE"}]})
        return self._send(400, {"__type": "InvalidRequestException", "message": "unknown target " + target})
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_port, flush=True)
srv.serve_forever()
PYEOF
mkfifo "$TMPDIR/athport"
"${PY:-python3}" "$TMPDIR/athenaserver.py" "$REPO/handlers/lib" > "$TMPDIR/athport" 2>"$TMPDIR/athenaserver.err" &
ATH_PID=$!
read -r ATH_PORT < "$TMPDIR/athport"
cat > "$TMPDIR/ath.conf" <<EOF
test.athena     $REPO/handlers/run athena
test.athenabad  $REPO/handlers/run athena
EOF
cat > "$TMPDIR/ath-backends.conf" <<EOF
[test.athena]
upstream   = http://127.0.0.1:$ATH_PORT
region     = eu-west-1
database   = db1
output     = s3://results-bucket/jci/
access_key = AKIDEXAMPLE
secret_key = wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY
[test.athenabad]
upstream   = http://127.0.0.1:$ATH_PORT
region     = eu-west-1
access_key = AKIDEXAMPLE
secret_key = not-the-right-secret
EOF
ath_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/ath.conf" \
        INTERCEPT_BACKENDS_CONF="$TMPDIR/ath-backends.conf" JCI_HANDLERS="$REPO/handlers" \
        "$CURL" -s --max-time 30 -w '\n%{http_code}' "$@"
}
RESP=$(ath_curl -G http://test.athena/query --data-urlencode 'sql=select 7 as n')
assert_eq    "athena GET select -> 200 (signature verified upstream)" "200" "${RESP##*$'\n'}"
assert_match "athena GET select typed rows, header row dropped" '"rows": [[7, "seven"]]' "${RESP%$'\n'*}"
assert_match "athena GET reports the execution id"           '"executionId": "q-' "${RESP%$'\n'*}"
assert_match "athena GET reports bytes scanned"              '"dataScannedBytes": 2048' "${RESP%$'\n'*}"
assert_no_match "athena GET select carries no warning"       '"warning"' "${RESP%$'\n'*}"
RESP=$(ath_curl -G http://test.athena/query --data-urlencode 'sql=select page')
assert_match "athena GET follows NextToken"                  '"rows": [[1, "a"], [2, "b"], [3, "c"]]' "${RESP%$'\n'*}"
RESP=$(ath_curl -G http://test.athena/query --data-urlencode 'sql=select slow')
assert_match "athena GET polls RUNNING to SUCCEEDED"         '"rows": [[7, "seven"]]' "${RESP%$'\n'*}"
RESP=$(ath_curl -G http://test.athena/query --data-urlencode 'sql=select fail')
assert_eq    "athena GET failed query -> 400"                "400" "${RESP##*$'\n'}"
assert_match "athena GET failed query carries the reason"    'SYNTAX_ERROR' "${RESP%$'\n'*}"
RESP=$(ath_curl -G http://test.athena/query --data-urlencode 'sql=create table t2 (x int)')
assert_eq    "athena GET create -> 200 (Athena has no pre-execution gate)" "200" "${RESP##*$'\n'}"
assert_match "athena GET create is flagged as a warning"     '"warning"' "${RESP%$'\n'*}"
assert_match "athena GET create reports DDL"                 '"statementType": "DDL"' "${RESP%$'\n'*}"
RESP=$(ath_curl -X POST http://test.athena/query -d 'create table t2 (x int)')
assert_eq    "athena POST create -> 200"                     "200" "${RESP##*$'\n'}"
RESP=$(ath_curl http://test.athena/databases)
assert_match "athena GET /databases"                         '"databases": ["db1", "db2"]' "${RESP%$'\n'*}"
RESP=$(ath_curl 'http://test.athena/tables?database=db1')
assert_match "athena GET /tables?database="                  '"t1"' "${RESP%$'\n'*}"
RESP=$(ath_curl http://test.athenabad/databases)
assert_eq    "athena wrong secret -> 502"                    "502" "${RESP##*$'\n'}"
assert_match "athena wrong secret names the signature"       'signature' "${RESP%$'\n'*}"
kill "$ATH_PID" 2>/dev/null; wait "$ATH_PID" 2>/dev/null

# ---------------------------------------------------------------------------
section "method permissions"

cat > "$TMPDIR/perms.conf" <<EOF
test.ro     GET           $REPO/handlers/run hello
test.rw     GET,POST      $REPO/handlers/run hello
test.any                  $REPO/handlers/run hello
test.star   *             $REPO/handlers/run hello
EOF

perm_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/perms.conf" \
        JCI_HANDLERS="$REPO/handlers" "$CURL" -s -o /dev/null -w '%{http_code}' "$@"
}

assert_eq "GET on GET-only -> 200"     "200" "$(perm_curl http://test.ro/)"
assert_eq "POST on GET-only -> 405"    "405" "$(perm_curl -X POST -d x http://test.ro/)"
assert_eq "DELETE on GET-only -> 405"  "405" "$(perm_curl -X DELETE http://test.ro/)"
assert_eq "GET on GET,POST -> 200"     "200" "$(perm_curl http://test.rw/)"
assert_eq "POST on GET,POST -> 200"    "200" "$(perm_curl -X POST -d x http://test.rw/)"
assert_eq "PUT on GET,POST -> 405"     "405" "$(perm_curl -X PUT http://test.rw/)"
assert_eq "DELETE on no-list -> 200"   "200" "$(perm_curl -X DELETE http://test.any/)"
assert_eq "DELETE on '*' -> 200"       "200" "$(perm_curl -X DELETE http://test.star/)"

ALLOW=$(env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/perms.conf" \
        JCI_HANDLERS="$REPO/handlers" "$CURL" -s -i -X POST -d x http://test.ro/ \
        | tr -d '\r' | grep -i '^allow:')
assert_match "Allow header present on 405" "Allow: GET" "$ALLOW"

# The gate lives in the shim, not the handler: a denied request must never
# spawn the handler at all. The route's handler leaves a marker file if run.
cat > "$TMPDIR/spy.conf" <<EOF
test.spy  GET  touch $TMPDIR/spy-ran; exec $REPO/handlers/run hello
EOF
spy_curl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/spy.conf" \
        JCI_HANDLERS="$REPO/handlers" "$CURL" -s -o /dev/null -w '%{http_code}' "$@"
}
rm -f "$TMPDIR/spy-ran"
assert_eq "denied request -> 405"  "405" "$(spy_curl -X DELETE http://test.spy/)"
sleep 0.2  # the handler is a detached grandchild; give it a beat to not run
[ ! -e "$TMPDIR/spy-ran" ] && ok "denied request never spawned the handler" \
    || bad "denied request spawned the handler"
assert_eq "allowed request -> 200"  "200" "$(spy_curl http://test.spy/)"
sleep 0.2
[ -e "$TMPDIR/spy-ran" ] && ok "allowed request spawned the handler" \
    || bad "allowed request didn't spawn the handler"

# ---------------------------------------------------------------------------
section "sse streaming"

# Self-contained SSE handler that emits 5 events at 0.5s intervals. If the
# shim or anything else buffers the response, all 5 arrive in a single burst
# at handler exit -- which we detect by comparing curl's time_starttransfer
# (first byte) to time_total. Streamed: gap is large. Buffered: gap ~= 0.
PY=${PY:-python3}
cat > "$TMPDIR/sse_routes.conf" <<EOF
test.clock  $PY $REPO/tests/sse_clock.py
EOF

TIMING=$(env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/sse_routes.conf" \
    JCI_HANDLERS="$REPO/handlers" \
    "$CURL" -sN --max-time 4 -o "$TMPDIR/sse.out" \
    -w 'start=%{time_starttransfer} total=%{time_total}' \
    http://test.clock/ 2>&1)

EVENTS=$(grep -c '^data:' "$TMPDIR/sse.out" 2>/dev/null || echo 0)
[ "$EVENTS" -ge 3 ] && ok "sse: streamed $EVENTS event(s)" \
    || bad "sse: only $EVENTS event(s) received" "$(head -c 200 "$TMPDIR/sse.out")"

START=$(echo "$TIMING" | grep -oE 'start=[0-9.]+' | cut -d= -f2)
TOTAL=$(echo "$TIMING" | grep -oE 'total=[0-9.]+' | cut -d= -f2)
SPLIT_OK=$(awk -v s="${START:-0}" -v t="${TOTAL:-0}" \
    'BEGIN { print (t - s > 0.8) ? "yes" : "no" }')
[ "$SPLIT_OK" = "yes" ] \
    && ok "sse: bytes arrived progressively (total - start_transfer > 0.8s)" \
    || bad "sse: response not streamed" "timing: $TIMING"

# ---------------------------------------------------------------------------
section "helpers (jci-run)"

# Pick a shell the OS will let the preload var through -- same reasoning as
# bin/jci's pick_shell (SIP strips DYLD_* from /bin/zsh and /bin/bash).
HELPER_SHELL=""
case "$(uname -s)" in
    Darwin)
        for s in /opt/homebrew/bin/zsh /usr/local/bin/zsh \
                 /opt/homebrew/bin/bash /usr/local/bin/bash; do
            [ -x "$s" ] && { HELPER_SHELL="$s"; break; }
        done
        ;;
    Linux) HELPER_SHELL=$(command -v bash) ;;
esac

if [ -z "$HELPER_SHELL" ]; then
    printf '  \033[33m~\033[0m no non-SIP shell found, skipping helper tests\n'
else
    cat > "$TMPDIR/helpers.conf" <<EOF
test.helper  $REPO/handlers/run hello
EOF

    # The script under test has the `#!/usr/bin/env bash` shebang that makes
    # macOS strip DYLD_* before its first line runs -- exactly the case
    # jci-run exists for. A bare curl here would fail to resolve test.helper.
    cat > "$TMPDIR/inner.sh" <<'EOS'
#!/usr/bin/env bash
set -u
. "$JCI_HELPERS"
jci-run "'$CURL' -s http://test.helper/"
EOS
    chmod +x "$TMPDIR/inner.sh"

    helper_env=(env
        INTERCEPT_CONFIG="$TMPDIR/helpers.conf"
        JCI_HANDLERS="$REPO/handlers"
        JCI_ENV=test
        JCI_LIB="$DYLIB"
        JCI_PRELOAD_VAR="$PRELOAD_VAR"
        JCI_SHELL="$HELPER_SHELL"
        JCI_HELPERS="$REPO/lib/helpers.sh"
        CURL="$CURL")

    OUT=$("${helper_env[@]}" "$TMPDIR/inner.sh" 2>&1)
    assert_match "jci-run restores the shim in a SIP-stripped script" \
                 '"handler":"hello.sh"' "$OUT"

    OUT=$("${helper_env[@]}" "$HELPER_SHELL" -c \
          '. "$JCI_HELPERS"; jci-routes')
    assert_match "jci-routes lists the active route" "test.helper" "$OUT"

    # No env -> clear error, not a cryptic `env: : No such file`.
    OUT=$(env -u JCI_LIB -u JCI_PRELOAD_VAR -u JCI_SHELL \
          JCI_HELPERS="$REPO/lib/helpers.sh" "$HELPER_SHELL" -c \
          '. "$JCI_HELPERS"; jci-run "true"' 2>&1)
    assert_match "jci-run outside an env explains itself" "not inside an activated env" "$OUT"
fi

# ---------------------------------------------------------------------------
section "ipv6 v4-mapped (the Java HttpClient case)"

cat > "$TMPDIR/v6probe.c" <<'C'
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "usage\n"); return 1; }
    struct addrinfo hints = {0}, *res;
    hints.ai_family = AF_UNSPEC; hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(argv[1], "80", &hints, &res) != 0) return 1;
    /* Force AF_INET6 socket + v4-mapped destination. */
    struct sockaddr_in6 v6 = {0};
    v6.sin6_family = AF_INET6;
    v6.sin6_port = htons(80);
    v6.sin6_addr.s6_addr[10] = 0xff;
    v6.sin6_addr.s6_addr[11] = 0xff;
    struct sockaddr_in *sin = (struct sockaddr_in *)res->ai_addr;
    memcpy(&v6.sin6_addr.s6_addr[12], &sin->sin_addr, 4);
    int s = socket(AF_INET6, SOCK_STREAM, 0);
    if (connect(s, (struct sockaddr *)&v6, sizeof(v6)) != 0) { perror("connect"); return 2; }
    const char *req = "GET / HTTP/1.1\r\nHost: ";
    write(s, req, strlen(req));
    write(s, argv[1], strlen(argv[1]));
    write(s, "\r\nConnection: close\r\n\r\n", 23);
    char buf[4096]; ssize_t n;
    while ((n = read(s, buf, sizeof(buf))) > 0) write(1, buf, n);
    close(s); freeaddrinfo(res); return 0;
}
C
if cc -O2 -o "$TMPDIR/v6probe" "$TMPDIR/v6probe.c" 2>/dev/null; then
    RESP=$(env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/routes.conf" \
           JCI_HANDLERS="$REPO/handlers" "$TMPDIR/v6probe" test.hello 2>&1)
    assert_match "v4-mapped v6 connect routed" '"host":"test.hello"' "$RESP"
else
    bad "couldn't compile v6probe (cc missing?)"
fi

# ---------------------------------------------------------------------------
section "auth proxy"

# httpbin.org is flaky enough that a single probe misreports "reachable" and
# then the real request below times out, failing the suite for network reasons.
# Require two clean 200s before trusting it; otherwise skip.
hb_reachable() {
    local i code
    for i in 1 2; do
        code=$("$CURL" -s -o /dev/null -w '%{http_code}' --max-time 10 \
               https://httpbin.org/get 2>/dev/null)
        [ "$code" = "200" ] || return 1
    done
}

if hb_reachable; then
    cat > "$TMPDIR/proxy_routes.conf"   <<EOF
test.hb  $REPO/handlers/run proxy
EOF
    cat > "$TMPDIR/proxy_backends.conf" <<EOF
[test.hb]
upstream = https://httpbin.org
authorization = Bearer test-jci-suite
header_x-jci-test = present
EOF

    RESP=$(env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/proxy_routes.conf" \
           INTERCEPT_BACKENDS_CONF="$TMPDIR/proxy_backends.conf" \
           JCI_HANDLERS="$REPO/handlers" \
           "$CURL" -s --max-time 15 http://test.hb/headers)

    assert_match "Authorization injected" "Bearer test-jci-suite" "$RESP"
    assert_match "extra header injected"  "present"               "$RESP"
else
    printf '  \033[33m~\033[0m httpbin.org unreachable/flaky, skipping auth proxy test\n'
fi

# ---------------------------------------------------------------------------
section "mtls upstream"

# A local TLS server with verify_mode=CERT_REQUIRED, so a missing client cert
# fails the handshake rather than being quietly accepted. Covers all three
# config forms: a path, a command printing a path, and a command printing the
# PEM itself (which proxy.py spills to a temp file and unlinks).
MTLS_DIR="$TMPDIR/mtls"
mkdir -p "$MTLS_DIR"

gen_certs() {
    openssl req -x509 -newkey rsa:2048 -noenc -days 1 \
        -keyout "$MTLS_DIR/ca.key" -out "$MTLS_DIR/ca.crt" \
        -subj "/CN=jci-test-ca" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign,digitalSignature" \
        >/dev/null 2>&1 || return 1
    openssl req -newkey rsa:2048 -noenc \
        -keyout "$MTLS_DIR/srv.key" -out "$MTLS_DIR/srv.csr" \
        -subj "/CN=localhost" >/dev/null 2>&1 || return 1
    # Python >= 3.13 verifies with VERIFY_X509_STRICT by default, which
    # rejects CA-issued leaves lacking SKI/AKI ("Missing Authority Key
    # Identifier"). `x509 -req` doesn't add them on its own.
    printf 'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' \
        > "$MTLS_DIR/leaf.ext"
    { cat "$MTLS_DIR/leaf.ext"; printf 'subjectAltName=IP:127.0.0.1\n'; } \
        > "$MTLS_DIR/srv.ext"
    openssl x509 -req -in "$MTLS_DIR/srv.csr" -days 1 \
        -CA "$MTLS_DIR/ca.crt" -CAkey "$MTLS_DIR/ca.key" -CAcreateserial \
        -extfile "$MTLS_DIR/srv.ext" -out "$MTLS_DIR/srv.crt" \
        >/dev/null 2>&1 || return 1
    openssl req -newkey rsa:2048 -noenc \
        -keyout "$MTLS_DIR/cli.key" -out "$MTLS_DIR/cli.csr" \
        -subj "/CN=jci-test-client" >/dev/null 2>&1 || return 1
    openssl x509 -req -in "$MTLS_DIR/cli.csr" -days 1 \
        -CA "$MTLS_DIR/ca.crt" -CAkey "$MTLS_DIR/ca.key" -CAcreateserial \
        -extfile "$MTLS_DIR/leaf.ext" -out "$MTLS_DIR/cli.crt" \
        >/dev/null 2>&1 || return 1
}

if ! command -v openssl >/dev/null 2>&1; then
    printf '  \033[33m~\033[0m no openssl, skipping mtls test\n'
elif ! gen_certs; then
    printf '  \033[33m~\033[0m cert generation failed (%s), skipping mtls test\n' \
        "$(openssl version 2>&1 | head -1)"
else
    cat > "$MTLS_DIR/server.py" <<'PYEOF'
import http.server, json, ssl, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/redirect", "/relative"):
            # Absolute Location on our own origin (proxy must rewrite it),
            # or a relative one (proxy must leave it alone).
            host = self.headers.get("Host", "")
            loc = f"https://{host}/landed" if self.path == "/redirect" else "/landed"
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        cert = self.connection.getpeercert()
        cn = ""
        for rdn in (cert or {}).get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    cn = v
        body = json.dumps({"peer_cert": bool(cert), "cn": cn, "path": self.path,
                           "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_PUT = do_GET   # the sigv4 handler test PUTs an object

    def log_message(self, *a):
        pass

ca, crt, key = sys.argv[1:4]
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(crt, key)
ctx.load_verify_locations(ca)
ctx.verify_mode = ssl.CERT_REQUIRED
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(srv.server_port, flush=True)
srv.serve_forever()
PYEOF

    mkfifo "$MTLS_DIR/port"
    "${PY:-python3}" "$MTLS_DIR/server.py" \
        "$MTLS_DIR/ca.crt" "$MTLS_DIR/srv.crt" "$MTLS_DIR/srv.key" \
        > "$MTLS_DIR/port" 2>"$MTLS_DIR/server.err" &
    MTLS_PID=$!
    trap 'kill '"$MTLS_PID"' 2>/dev/null; rm -rf "$TMPDIR"' EXIT
    read -r MTLS_PORT < "$MTLS_DIR/port"

    if [ -z "${MTLS_PORT:-}" ]; then
        bad "mtls server starts" "$(cat "$MTLS_DIR/server.err")"
    else
        ok "mtls server listening on $MTLS_PORT"

        echo "test.mtls  $REPO/handlers/run proxy" > "$MTLS_DIR/routes.conf"

        # $1 = extra backends.conf lines; echoes "<body>\n<status>"
        mtls_get() {
            { echo "[test.mtls]"
              echo "upstream = https://127.0.0.1:$MTLS_PORT"
              echo "ca_cert  = $MTLS_DIR/ca.crt"
              printf '%s\n' "$1"; } > "$MTLS_DIR/backends.conf"
            env "$PRELOAD_VAR=$DYLIB" \
                INTERCEPT_CONFIG="$MTLS_DIR/routes.conf" \
                INTERCEPT_BACKENDS_CONF="$MTLS_DIR/backends.conf" \
                JCI_HANDLERS="$REPO/handlers" \
                "$CURL" -s --max-time 15 -w '\n%{http_code}' http://test.mtls/
        }

        # Header injection is also asserted here, not only in the auth-proxy
        # section: that one needs httpbin.org and skips when the network is
        # unhappy, leaving inject_headers uncovered.
        OUT=$(mtls_get "client_cert = $MTLS_DIR/cli.crt
client_key  = $MTLS_DIR/cli.key
authorization     = Bearer test-mtls-suite
header_x-jci-test = present")
        assert_eq    "client cert path -> 200"  "200" "${OUT##*$'\n'}"
        assert_match "server saw the peer cert" '"peer_cert": true' "${OUT%$'\n'*}"
        assert_match "server saw the client CN" 'jci-test-client'   "${OUT%$'\n'*}"
        assert_match "Authorization injected"   'Bearer test-mtls-suite' "${OUT%$'\n'*}"
        assert_match "extra header injected"    '"present"'         "${OUT%$'\n'*}"
        assert_match "Host points at upstream"  '127.0.0.1'         "${OUT%$'\n'*}"

        # Location rewrite: an upstream-origin absolute URL comes back on the
        # intercepted host so `curl -L` stays inside the shim; a relative one
        # passes through untouched.
        mtls_hdr() {  # <path> -> the Location header line, CR stripped
            env "$PRELOAD_VAR=$DYLIB" \
                INTERCEPT_CONFIG="$MTLS_DIR/routes.conf" \
                INTERCEPT_BACKENDS_CONF="$MTLS_DIR/backends.conf" \
                JCI_HANDLERS="$REPO/handlers" \
                "$CURL" -s -i --max-time 15 "http://test.mtls$1" \
                | tr -d '\r' | grep -i '^location:'
        }
        assert_eq "absolute Location rewritten to intercepted host" \
            "Location: http://test.mtls/landed" "$(mtls_hdr /redirect)"
        assert_eq "relative Location passes through" \
            "Location: /landed" "$(mtls_hdr /relative)"

        # aws-sigv4 against the same TLS server (needs the client cert too,
        # since the server demands one): the echo shows what AWS would see.
        echo "test.s3  $REPO/handlers/run aws-sigv4" >> "$MTLS_DIR/routes.conf"
        cat > "$MTLS_DIR/s3-backends.conf" <<EOF
[test.s3]
upstream    = https://127.0.0.1:$MTLS_PORT
ca_cert     = $MTLS_DIR/ca.crt
client_cert = $MTLS_DIR/cli.crt
client_key  = $MTLS_DIR/cli.key
region      = us-east-1
service     = s3
access_key  = AKIDEXAMPLE
secret_key  = wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY
EOF
        s3_curl() {
            env "$PRELOAD_VAR=$DYLIB" \
                INTERCEPT_CONFIG="$MTLS_DIR/routes.conf" \
                INTERCEPT_BACKENDS_CONF="$MTLS_DIR/s3-backends.conf" \
                JCI_HANDLERS="$REPO/handlers" \
                "$CURL" -s --max-time 15 -w '\n%{http_code}' "$@"
        }
        OUT=$(s3_curl 'http://test.s3/sig/bucket?list-type=2&prefix=a/')
        assert_eq    "sigv4 GET -> 200"                       "200" "${OUT##*$'\n'}"
        assert_match "sigv4 Authorization is SigV4 for our key" \
            '"authorization": "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/' "${OUT%$'\n'*}"
        assert_match "sigv4 signs host, payload hash and date" \
            'SignedHeaders=host;x-amz-content-sha256;x-amz-date' "${OUT%$'\n'*}"
        assert_match "sigv4 Host is the upstream authority" \
            "\"host\": \"127.0.0.1:$MTLS_PORT\"" "${OUT%$'\n'*}"
        assert_match "sigv4 query reaches upstream intact" \
            '"path": "/sig/bucket?list-type=2&prefix=a/"' "${OUT%$'\n'*}"
        # An S3 key with a space arrives percent-encoded (curl refuses a raw
        # space in a URL), and must reach the upstream exactly that way.
        OUT=$(s3_curl -X PUT 'http://test.s3/sig/bucket/my%20key.txt' --data-binary 'hello')
        assert_eq    "sigv4 PUT -> 200"                       "200" "${OUT##*$'\n'}"
        assert_match "sigv4 payload hash is sha256(body)" \
            '"x-amz-content-sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"' \
            "${OUT%$'\n'*}"
        assert_match "sigv4 PUT path keeps its encoding on the wire" \
            '"path": "/sig/bucket/my%20key.txt"' "${OUT%$'\n'*}"

        OUT=$(mtls_get "# no client cert configured")
        assert_eq    "no client cert -> 502"    "502" "${OUT##*$'\n'}"
        assert_match "no client cert -> TLS error" "SSL" "${OUT%$'\n'*}"

        OUT=$(mtls_get "client_cert = $MTLS_DIR/cli.crt")
        assert_match "cert without key -> config error" \
            "must be set together" "${OUT%$'\n'*}"

        OUT=$(mtls_get "client_cert = $MTLS_DIR/nope.crt
client_key  = $MTLS_DIR/cli.key")
        assert_match "missing cert file -> named error" \
            "no such file" "${OUT%$'\n'*}"

        OUT=$(mtls_get "client_cert_cmd = echo $MTLS_DIR/cli.crt
client_key_cmd  = echo $MTLS_DIR/cli.key")
        assert_eq "_cmd printing a path -> 200" "200" "${OUT##*$'\n'}"

        OUT=$(mtls_get "client_cert_cmd = cat $MTLS_DIR/cli.crt
client_key_cmd  = cat $MTLS_DIR/cli.key")
        assert_eq "_cmd printing PEM -> 200"    "200" "${OUT##*$'\n'}"

        OUT=$(mtls_get "client_cert = \${JCI_NOT_SET_ANYWHERE}
client_key  = $MTLS_DIR/cli.key")
        assert_match "unexpanded \${VAR} -> named error" \
            "unexpanded" "${OUT%$'\n'*}"

        { kill "$MTLS_PID"; wait "$MTLS_PID"; } 2>/dev/null
        trap 'rm -rf "$TMPDIR"' EXIT
    fi
fi

# ---------------------------------------------------------------------------
section "unit: jci.py"

PY=${PY:-python3}
if PYTHONPATH="$REPO/handlers/lib" "$PY" -m unittest discover -s "$REPO/tests/unit" \
        > "$TMPDIR/unit.out" 2>&1; then
    ok "jci.py unit tests: $(grep -oE 'Ran [0-9]+ tests?' "$TMPDIR/unit.out")"
else
    bad "jci.py unit tests" "$(tail -20 "$TMPDIR/unit.out")"
fi

# ---------------------------------------------------------------------------
summary
