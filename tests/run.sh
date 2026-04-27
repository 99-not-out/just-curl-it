#!/usr/bin/env bash
# jci test suite. Covers the regressions we hit during development:
#   - build sanity
#   - routes.conf parsing (comments, methods detection, handler with spaces)
#   - end-to-end interception (curl -> sentinel IP -> spawned handler)
#   - method permissions (GET-only denies POST/DELETE; Allow header)
#   - auth proxy header injection (against httpbin.org if reachable)
#   - AF_INET6 with v4-mapped destinations (the Java HttpClient case)
#   - Python unit tests for enforce_methods in proxy.py and shim.py
#
# Usage: ./tests/run.sh
#        make test
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0
declare -a FAILURES

case "$(uname -s)" in
    Darwin)
        BREW_CURL_DIR=$(brew --prefix curl 2>/dev/null)/bin
        CURL="$BREW_CURL_DIR/curl"
        [ -x "$CURL" ] || CURL=$(ls /opt/homebrew/Cellar/curl/*/bin/curl 2>/dev/null | head -1)
        DYLIB="$REPO/lib/intercept.dylib"
        PRELOAD_VAR=DYLD_INSERT_LIBRARIES
        ;;
    Linux)
        CURL=$(command -v curl)
        DYLIB="$REPO/lib/intercept.so"
        PRELOAD_VAR=LD_PRELOAD
        ;;
    *) echo "unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac

if [ ! -x "${CURL:-/nonexistent}" ]; then
    echo "FAIL: no non-SIP curl available (brew install curl)" >&2
    exit 1
fi

section() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()      { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()     { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  \033[31m✗\033[0m %s\n' "$1"; [ -n "${2-}" ] && printf '    %s\n' "$2"; }

assert_eq()    { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
assert_match() { [[ "$3" == *"$2"* ]] && ok "$1" || bad "$1" "expected to contain '$2'; got '$3'"; }

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

test.plain        $REPO/handlers/hello.sh
test.gated  GET   $REPO/handlers/hello.sh
test.spaces       $REPO/handlers/hello.sh extra args here
EOF

DEBUG=$(env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$TMPDIR/parse.conf" \
        INTERCEPT_DEBUG=1 "$CURL" --max-time 1 http://nope.invalid/ 2>&1 || true)

assert_match "loads 3 routes (comment + blank line ignored)" "loaded 3 route" "$DEBUG"
assert_match "no methods -> [*]" "test.plain [*]" "$DEBUG"
assert_match "GET methods detected"        "test.gated [GET]" "$DEBUG"
assert_match "handler with spaces preserved" "extra args here" "$DEBUG"

# ---------------------------------------------------------------------------
section "basic routing"

cat > "$TMPDIR/routes.conf" <<EOF
test.hello  $REPO/handlers/hello.sh
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
section "method permissions"

cat > "$TMPDIR/perms.conf" <<EOF
test.ro     GET           $REPO/handlers/hello.sh
test.rw     GET,POST      $REPO/handlers/hello.sh
test.any                  $REPO/handlers/hello.sh
test.star   *             $REPO/handlers/hello.sh
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

if "$CURL" -s -o /dev/null --max-time 5 https://httpbin.org/get; then
    cat > "$TMPDIR/proxy_routes.conf"   <<EOF
test.hb  $REPO/handlers/proxy.py
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
    printf '  \033[33m~\033[0m httpbin.org unreachable, skipping auth proxy test\n'
fi

# ---------------------------------------------------------------------------
section "python unit tests"

PY=${PY:-python3}

run_unit() {
    local module_dir="$1" module="$2" allow="$3" method="$4"
    local args=(env "PYTHONPATH=$module_dir")
    [ "$allow" != "-" ] && args+=("INTERCEPT_METHODS=$allow")
    "${args[@]}" "$PY" -c "
import sys
sys.path.insert(0, '$module_dir')
import $module
$module.enforce_methods('$method')
" 2>&1
}

# proxy.py: stdlib-only enforcement
RES=$(run_unit "$REPO/handlers" proxy "-" POST)
[ -z "$RES" ] && ok "proxy: no INTERCEPT_METHODS allows anything" || bad "proxy unrestricted" "$RES"

RES=$(run_unit "$REPO/handlers" proxy "*" DELETE)
[ -z "$RES" ] && ok "proxy: '*' allows anything"             || bad "proxy wildcard" "$RES"

RES=$(run_unit "$REPO/handlers" proxy "GET,POST" GET)
[ -z "$RES" ] && ok "proxy: method in list -> pass"          || bad "proxy in list" "$RES"

RES=$(run_unit "$REPO/handlers" proxy "GET" POST)
assert_match "proxy: method not in list -> 405" "405 Method Not Allowed" "$RES"

# shim.py: same logic, used by demo handlers
RES=$(run_unit "$REPO/demos/handlers" shim "-" POST)
[ -z "$RES" ] && ok "shim:  no INTERCEPT_METHODS allows anything" || bad "shim unrestricted" "$RES"

RES=$(run_unit "$REPO/demos/handlers" shim "GET" POST)
assert_match "shim:  method not in list -> 405" "405 Method Not Allowed" "$RES"

RES=$(run_unit "$REPO/demos/handlers" shim "GET,POST" POST)
[ -z "$RES" ] && ok "shim:  method in list -> pass" || bad "shim in list" "$RES"

# ---------------------------------------------------------------------------
section "summary"
TOTAL=$((PASS+FAIL))
if [ $FAIL -eq 0 ]; then
    printf '\033[32mall %d test(s) passed\033[0m\n' "$TOTAL"
    exit 0
else
    printf '\033[31m%d/%d failed:\033[0m\n' "$FAIL" "$TOTAL"
    for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
    exit 1
fi
