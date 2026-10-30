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
for h in redis postgres mysql clickhouse smtp kafka grpc mongo rabbit; do
    echo "mylocal.$h   $JH/handlers/run $h" >> "$ROUTES"
done
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
    # Multi-statement GET: COMMIT would end the READ ONLY transaction and the
    # next statement would run read-write. Must be refused, not executed.
    OUT=$(hcurl -G http://mylocal.postgres/query --data-urlencode "sql=COMMIT; create table ${T}_esc(x int)")
    [ "$(code "$OUT")" != "200" ] && ok "postgres GET 'COMMIT; create' refused ($(code "$OUT"))" \
        || bad "postgres GET 'COMMIT; create' -> 200" "$(body "$OUT")"
    OUT=$(hcurl http://mylocal.postgres/tables)
    assert_no_match "postgres GET escape created nothing"  "${T}_esc" "$(body "$OUT")"
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
section "mysql"
if install_or_skip mysql mysql; then
    T="jci_test_$$"
    OUT=$(hcurl -G http://mylocal.mysql/query --data-urlencode 'sql=select 1 as one')
    assert_match "mysql GET select -> rows"               '"rows": [[1]]' "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.mysql/query --data-urlencode "sql=create table $T(x int)")
    assert_eq    "mysql GET create -> 405 (read-only)"     "405" "$(code "$OUT")"
    OUT=$(hcurl -G http://mylocal.mysql/query --data-urlencode "sql=select 1; create table ${T}_esc(x int)")
    [ "$(code "$OUT")" != "200" ] && ok "mysql GET multi-statement refused ($(code "$OUT"))" \
        || bad "mysql GET multi-statement -> 200" "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.mysql/query -d "create table $T(x int)")
    assert_eq    "mysql POST create -> 200"                "200" "$(code "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.mysql/query -H 'content-type: application/json' \
          -d "{\"sql\":\"insert into $T values (%s)\",\"params\":[7]}")
    assert_match "mysql POST insert with params"           '"rowCount": 1' "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.mysql/query --data-urlencode "sql=insert into $T values (8)")
    assert_eq    "mysql GET insert -> 405"                 "405" "$(code "$OUT")"
    OUT=$(hcurl http://mylocal.mysql/tables)
    assert_match "mysql GET /tables lists it"              "$T" "$(body "$OUT")"
    assert_no_match "mysql GET escape created nothing"     "${T}_esc" "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.mysql/query --data-urlencode "sql=select count(*) from $T")
    assert_match "mysql GET sees exactly the POSTed row"   '"rows": [[1]]' "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.mysql/query -d "drop table $T")
    assert_eq    "mysql POST drop -> 200"                  "200" "$(code "$OUT")"
    OUT=$(hcurl -G http://mylocal.mysql/query --data-urlencode 'sql=select * from nope_$$')
    assert_eq    "mysql bad SQL -> 400"                    "400" "$(code "$OUT")"
fi

# ---------------------------------------------------------------------------
section "clickhouse"
if install_or_skip clickhouse clickhouse; then
    T="jci_test_$$"
    OUT=$(hcurl -G http://mylocal.clickhouse/query --data-urlencode 'sql=select 1 as one')
    assert_match "clickhouse GET select -> rows"              '"rows": [[1]]' "$(body "$OUT")"
    assert_match "clickhouse GET select -> types"             '"types": ["UInt8"]' "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.clickhouse/query --data-urlencode "sql=create table $T (x Int32) engine = Memory")
    assert_eq    "clickhouse GET create -> 405 (readonly)"    "405" "$(code "$OUT")"
    OUT=$(hcurl -G http://mylocal.clickhouse/query --data-urlencode "sql=select 1; create table ${T}_esc (x Int32) engine = Memory")
    [ "$(code "$OUT")" != "200" ] && ok "clickhouse GET multi-statement refused ($(code "$OUT"))" \
        || bad "clickhouse GET multi-statement -> 200" "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.clickhouse/query -d "create table $T (x Int32) engine = Memory")
    assert_eq    "clickhouse POST create -> 200"              "200" "$(code "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.clickhouse/query -d "insert into $T values (7), (8)")
    assert_eq    "clickhouse POST insert -> 200"              "200" "$(code "$OUT")"
    assert_match "clickhouse POST insert reports written rows" '"rowCount": 2' "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.clickhouse/query --data-urlencode "sql=insert into $T values (9)")
    assert_eq    "clickhouse GET insert -> 405"               "405" "$(code "$OUT")"
    OUT=$(hcurl http://mylocal.clickhouse/tables)
    assert_match "clickhouse GET /tables lists it"            "$T" "$(body "$OUT")"
    assert_no_match "clickhouse GET escape created nothing"   "${T}_esc" "$(body "$OUT")"
    OUT=$(hcurl -G http://mylocal.clickhouse/query --data-urlencode "sql=select count() from $T")
    assert_match "clickhouse GET count sees 2"                '"rows": [["2"]]' "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.clickhouse/query -d "drop table $T")
    assert_eq    "clickhouse POST drop -> 200"                "200" "$(code "$OUT")"
    OUT=$(hcurl -G http://mylocal.clickhouse/query --data-urlencode 'sql=select * from nope_$$')
    assert_eq    "clickhouse bad SQL -> 400"                  "400" "$(code "$OUT")"
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

# ---------------------------------------------------------------------------
section "kafka"
if install_or_skip kafka kafka; then
    T="jci-test-$$"
    OUT=$(hcurl -X POST "http://mylocal.kafka/stream/$T?key=k1" -d 'one')
    assert_eq    "kafka POST /stream -> 200"             "200" "$(code "$OUT")"
    assert_match "kafka produce reports offset"          '"offset"' "$(body "$OUT")"
    OUT=$(hcurl -X POST "http://mylocal.kafka/stream/$T?key=k2" -d 'two')
    assert_eq    "kafka second POST -> 200"              "200" "$(code "$OUT")"
    OUT=$(hcurl "http://mylocal.kafka/stream/$T/peek?limit=10")
    assert_match "kafka peek sees both records"          '"count": 2' "$(body "$OUT")"
    assert_match "kafka peek carries the key"            '"key": "k2"' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.kafka/stream/$T/peek?limit=10")
    assert_match "kafka peek is non-destructive"         '"count": 2' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.kafka/streams")
    assert_match "kafka GET /streams lists the topic"    "\"$T\"" "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.kafka/stream/$T")
    assert_match "kafka GET /stream/{T} shows partitions" '"partitionCount"' "$(body "$OUT")"
    OUT=$(hcurl -X DELETE "http://mylocal.kafka/stream/$T")
    assert_eq    "kafka DELETE /stream -> 501"           "501" "$(code "$OUT")"
fi

# ---------------------------------------------------------------------------
section "grpc"
if install_or_skip grpc grpc; then
    OUT=$(hcurl http://mylocal.grpc/_services)
    assert_match "grpc /_services lists Greeter"          'helloworld.Greeter' "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.grpc/helloworld.Greeter/SayHello -d '{"name":"jci"}')
    assert_eq    "grpc unary call -> 200"                 "200" "$(code "$OUT")"
    assert_match "grpc unary call reply"                  '"message": "Hello, jci!"' "$(body "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.grpc/helloworld.Greeter/NoSuch -d '{}')
    assert_eq    "grpc unknown method -> 404"             "404" "$(code "$OUT")"
    OUT=$(hcurl -X POST http://mylocal.grpc/helloworld.Greeter/SayHello -d 'not json')
    assert_eq    "grpc bad JSON -> 400"                   "400" "$(code "$OUT")"
fi

# ---------------------------------------------------------------------------
section "mongo"
if install_or_skip mongo mongo; then
    C="jci_test_$$"
    OUT=$(hcurl -X POST "http://mylocal.mongo/db/jcitest/coll/$C" -H 'content-type: application/json' \
          -d '{"name":"ada","n":1}')
    assert_eq    "mongo POST insert -> 200"               "200" "$(code "$OUT")"
    assert_match "mongo insert returns id"                '"insertedId"' "$(body "$OUT")"
    OUT=$(hcurl -X POST "http://mylocal.mongo/db/jcitest/coll/$C" -H 'content-type: application/json' \
          -d '[{"name":"bob","n":2},{"name":"cy","n":3}]')
    assert_match "mongo insert_many count"                '"insertedCount": 2' "$(body "$OUT")"
    OUT=$(hcurl -G "http://mylocal.mongo/db/jcitest/coll/$C" --data-urlencode 'filter={"n":{"$gte":2}}' \
          --data-urlencode 'sort={"n":-1}')
    assert_match "mongo find with filter+sort count"      '"count": 2' "$(body "$OUT")"
    assert_match "mongo find sorted desc first is cy"     '"name": "cy"' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.mongo/db/jcitest/colls")
    assert_match "mongo /colls lists it"                  "\"$C\"" "$(body "$OUT")"
    OUT=$(hcurl -X DELETE "http://mylocal.mongo/db/jcitest/coll/$C" -G --data-urlencode 'filter={}')
    assert_match "mongo DELETE deletes 3"                 '"deletedCount": 3' "$(body "$OUT")"
    OUT=$(hcurl -G "http://mylocal.mongo/db/jcitest/coll/$C" --data-urlencode 'filter=not json')
    assert_eq    "mongo bad filter -> 400"                "400" "$(code "$OUT")"
fi

# ---------------------------------------------------------------------------
section "rabbit"
if install_or_skip rabbit rabbit; then
    Q="jci-test-$$"
    OUT=$(hcurl -X POST "http://mylocal.rabbit/stream/$Q" -d 'a')
    assert_eq    "rabbit POST /stream -> 200"             "200" "$(code "$OUT")"
    OUT=$(hcurl "http://mylocal.rabbit/stream/$Q/peek?limit=1")
    assert_match "rabbit peek sees the message"           '"count": 1' "$(body "$OUT")"
    assert_match "rabbit peek body"                       '"body": "a"' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.rabbit/stream/$Q/peek?limit=1")
    assert_match "rabbit peek is non-destructive"         '"count": 1' "$(body "$OUT")"
    assert_match "rabbit re-peeked message is flagged redelivered" '"redelivered": true' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.rabbit/stream/$Q")
    assert_match "rabbit GET /stream/{q} still counts 1"  '"messageCount": 1' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.rabbit/streams")
    assert_match "rabbit GET /streams lists the queue"    "\"$Q\"" "$(body "$OUT")"
    OUT=$(hcurl -X DELETE "http://mylocal.rabbit/stream/$Q")
    assert_match "rabbit DELETE purges 1"                 '"purged": 1' "$(body "$OUT")"
    # Real deployments use durable/quorum queues; a default (non-durable)
    # declare against one fails with PRECONDITION_FAILED. Declare passively.
    MGMT=$(awk '/^\[mylocal.rabbit\]/{f=1;next} /^\[/{f=0} f && /^mgmt_url/{sub(/^mgmt_url *= */,"");print}' "$BACKENDS")
    DQ="jci-durable-$$"
    "$CURL" -s -o /dev/null -X PUT "$MGMT/api/queues/%2F/$DQ" -H 'content-type: application/json' \
        -d '{"durable":true,"auto_delete":false}'
    OUT=$(hcurl -X POST "http://mylocal.rabbit/stream/$DQ" -d 'd1')
    assert_eq    "rabbit POST to a durable queue -> 200"  "200" "$(code "$OUT")"
    OUT=$(hcurl "http://mylocal.rabbit/stream/$DQ/peek?limit=1")
    assert_match "rabbit peek on a durable queue works"   '"body": "d1"' "$(body "$OUT")"
    OUT=$(hcurl "http://mylocal.rabbit/stream/$DQ")
    assert_match "rabbit GET on a durable queue works"    '"messageCount": 1' "$(body "$OUT")"
    "$CURL" -s -o /dev/null -X DELETE "$MGMT/api/queues/%2F/$DQ"
    # GET on a queue that doesn't exist is a 404 and creates nothing.
    OUT=$(hcurl "http://mylocal.rabbit/stream/jci-nonexistent-$$")
    assert_eq    "rabbit GET unknown queue -> 404"        "404" "$(code "$OUT")"
    MC=$("$CURL" -s -o /dev/null -w '%{http_code}' "$MGMT/api/queues/%2F/jci-nonexistent-$$")
    assert_eq    "rabbit GET unknown queue created nothing" "404" "$MC"
fi

summary
