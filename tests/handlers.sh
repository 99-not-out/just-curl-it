#!/usr/bin/env bash
# Docker-backed handler tests. Each section needs one service from
# demos/docker-compose.yml and skips when it isn't running:
#
#     cd demos && docker compose up -d
#     make test-handlers
#
# Handlers are installed with `jci handler install` into a throwaway
# JCI_HOME, so this also exercises the real venv path (network needed).
# Backend addresses come from demos/env/backends.conf.
set -u

# shellcheck source=tests/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

COMPOSE="docker compose -f $REPO/demos/docker-compose.yml"
running() { $COMPOSE ps --status running --services 2>/dev/null | grep -qx "$1"; }

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/jci-htests.XXXXXX")
trap 'rm -rf "$TMPDIR"' EXIT
JH="$TMPDIR/home"
mkdir -p "$JH/handlers"
cp -R "$REPO/handlers/lib" "$JH/handlers/lib"
cp "$REPO/handlers/run" "$JH/handlers/run"
jh() { JCI_HOME="$JH" "$REPO/bin/jci" "$@"; }

ROUTES="$TMPDIR/routes.conf"
BACKENDS="$REPO/demos/env/backends.conf"
: > "$ROUTES"
for h in redis postgres smtp; do echo "mylocal.$h   $JH/handlers/run $h" >> "$ROUTES"; done
# Same postgres backend, GET-only route: the shim gate plus the handler's
# read-only GET together.
echo "mylocal.postgres-ro  GET  $JH/handlers/run postgres" >> "$ROUTES"
cp "$BACKENDS" "$TMPDIR/backends.conf"
printf '\n[mylocal.postgres-ro]\ndsn = %s\n' \
    "$(awk '/^\[mylocal.postgres\]/{f=1;next} /^\[/{f=0} f && /^dsn/{sub(/^dsn *= */,"");print}' "$BACKENDS")" \
    >> "$TMPDIR/backends.conf"
BACKENDS="$TMPDIR/backends.conf"

hcurl() {
    env "$PRELOAD_VAR=$DYLIB" INTERCEPT_CONFIG="$ROUTES" INTERCEPT_BACKENDS_CONF="$BACKENDS" \
        JCI_HANDLERS="$JH/handlers" "$CURL" -s --max-time 20 -w '\n%{http_code}' "$@"
}
body() { printf '%s' "${1%$'\n'*}"; }
code() { printf '%s' "${1##*$'\n'}"; }

install_or_skip() {  # <handler> <compose service>
    if ! running "$2"; then skip "$2 not running (docker compose up -d $2); skipping $1"; return 1; fi
    if ! jh handler install "$1" >"$TMPDIR/install-$1.log" 2>&1; then
        bad "jci handler install $1" "$(tail -5 "$TMPDIR/install-$1.log")"; return 1
    fi
    ok "jci handler install $1"
}

# ---------------------------------------------------------------------------
section "build"
make -C "$REPO" build >/dev/null 2>&1 && ok "shim builds" || { bad "shim builds"; exit 1; }

# ---------------------------------------------------------------------------
section "redis"
if install_or_skip redis redis; then
    K="jci-test-$$"
    OUT=$(hcurl -X PUT "http://mylocal.redis/kv/$K" --data-binary 'hello')
    assert_eq    "redis PUT /kv -> 200"              "200" "$(code "$OUT")"
    OUT=$(hcurl "http://mylocal.redis/kv/$K")
    assert_eq    "redis GET /kv -> value"            "hello" "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.redis/keys?prefix=jci-test-")
    assert_match "redis GET /keys?prefix lists it"   "$K" "$(body "$OUT")"
    OUT=$(hcurl -X DELETE "http://mylocal.redis/kv/$K")
    assert_match "redis DELETE /kv -> deleted 1"     '"deleted": 1' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.redis/kv/$K")
    assert_eq    "redis GET after DELETE -> 404"     "404" "$(code "$OUT")"
    OUT=$(hcurl "http://mylocal.redis/_info")
    assert_match "redis /_info has shape"            '"shape"' "$(body "$OUT")"
fi

# ---------------------------------------------------------------------------
section "postgres"
if install_or_skip postgres postgres; then
    T="jci_test_$$"
    OUT=$(hcurl -G http://mylocal.postgres/query --data-urlencode 'sql=select 1 as one')
    assert_match "postgres GET select -> rows"           '"rows": [[1]]' "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.postgres/query --data-urlencode "sql=create table $T(x int)")
    assert_eq    "postgres GET create -> 405 (read-only)" "405" "$(code "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.postgres/query -d "create table $T(x int)")
    assert_eq    "postgres POST create -> 200"           "200" "$(code "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.postgres/query -H 'content-type: application/json' \
          -d "{\"sql\":\"insert into $T values (%s)\",\"params\":[7]}")
    assert_match "postgres POST insert with params"      '"rowCount": 1' "$(body "$OUT")"
    OUT=$(hcurl http://mylocal.postgres/tables)
    assert_match "postgres GET /tables lists it"         "$T" "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.postgres-ro/query -d "drop table $T")
    assert_eq    "postgres-ro POST -> 405 from the shim"  "405" "$(code "$OUT")"
    OUT=$(hcurl -G http://mylocal.postgres-ro/query --data-urlencode "sql=select count(*) from $T")
    assert_match "postgres-ro GET select works"          '"rows": [[1]]' "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.postgres/query -d "drop table $T")
    assert_eq    "postgres POST drop -> 200"             "200" "$(code "$OUT")"
fi

# ---------------------------------------------------------------------------
section "smtp"
if install_or_skip smtp smtp; then
    UI=$(awk '/^\[mylocal.smtp\]/{f=1;next} /^\[/{f=0} f && /^ui_url/{sub(/^ui_url *= */,"");print}' "$BACKENDS")
    BEFORE=$("$CURL" -s "$UI/api/v2/messages" | python3 -c 'import sys,json; print(json.load(sys.stdin)["total"])')
    OUT=$(hcurl -X POST http://mylocal.smtp/send -H 'content-type: application/json' \
          -d '{"to":"t@example.com","subject":"jci test","body":"hi"}')
    assert_eq    "smtp POST /send -> 200"              "200" "$(code "$OUT")"
    assert_match "smtp /send reports sent"             '"sent": true' "$(body "$OUT")"
    AFTER=$("$CURL" -s "$UI/api/v2/messages" | python3 -c 'import sys,json; print(json.load(sys.stdin)["total"])')
    [ "$AFTER" -gt "$BEFORE" ] && ok "mailhog received the message ($BEFORE -> $AFTER)" \
        || bad "mailhog count did not increase" "$BEFORE -> $AFTER"
    OUT=$(hcurl -X POST http://mylocal.smtp/send -d 'not json')
    assert_eq    "smtp bad body -> 400"                "400" "$(code "$OUT")"
fi

summary
