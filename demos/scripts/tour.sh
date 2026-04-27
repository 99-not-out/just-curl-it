#!/usr/bin/env bash
# Walkthrough of the demos env. Run this from inside a `jci use demos` shell.
# Press enter between steps; 's' to skip; 'q' to quit.
set -u

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
step()  {
    printf '\n'
    green "▸ $1"
    [ -n "${2-}" ] && dim "  $2"
    printf '  \033[1;34m$ %s\033[0m\n' "$3"
    if [ "${NOPAUSE-}" != "1" ]; then
        read -r -p "  [enter to run, s to skip, q to quit] " a
        case "$a" in q|Q) exit 0 ;; s|S) return ;; esac
    fi
    bash -c "$3"
    printf '\n'
}

if [ -z "${JCI_ENV-}" ]; then
    echo "this script expects to run inside 'jci use demos'." >&2
    exit 1
fi

step "shim meta" "the shim introspects its own route table" \
     "curl -s http://mylocal.shim/routes | jq"

step "hello" "tiny bash handler -- proves the pipe is wired up" \
     "curl -s http://mylocal.hello/ping | jq"

step "postgres" "plain SQL via HTTP" \
     "curl -sG http://mylocal.postgres/query --data-urlencode 'sql=select now(), version()' | jq"

step "redis set/get" "curl as a redis client" \
     "curl -s -X PUT http://mylocal.redis/key/greeting --data-binary 'hello from curl'; echo; curl -s http://mylocal.redis/key/greeting; echo"

step "mongo" "insert then find" \
     "curl -s -X POST http://mylocal.mongo/db/demo/coll/users -H 'content-type: application/json' -d '{\"name\":\"ada\",\"age\":30}' | jq; curl -sG 'http://mylocal.mongo/db/demo/coll/users' --data-urlencode 'filter={}' | jq"

step "kafka produce/peek" "produce 3 records, read them back" \
     "curl -s -X POST http://mylocal.kafka/topic/demo/records -d $'k1|one\nk2|two\nk3|three' | jq; curl -s 'http://mylocal.kafka/topic/demo/records?limit=10' | jq"

step "rabbit" "publish then get" \
     "curl -s -X POST http://mylocal.rabbit/queue/demo/publish -d 'hi from curl' | jq; curl -s http://mylocal.rabbit/queue/demo/get | jq"

step "smtp" "send mail (open http://localhost:8025 for the mailhog UI)" \
     "curl -s -X POST http://mylocal.smtp/send -H 'content-type: application/json' -d '{\"to\":\"you@example.com\",\"subject\":\"hi\",\"body\":\"sent with curl\"}' | jq"

step "grpc" "curl speaks protobuf/HTTP2 under the hood" \
     "curl -s http://mylocal.grpc/_services | jq; curl -s -X POST http://mylocal.grpc/helloworld.Greeter/SayHello -d '{\"name\":\"matt\"}' | jq"

step "cross-backend pipeline" "stash postgres output straight into redis with jq + curl" \
     "curl -s 'http://mylocal.postgres/query?sql=select+now()' | jq -r '.rows[0][0]' | curl -s -X PUT http://mylocal.redis/key/last_pg_time --data-binary @-; echo; curl -s http://mylocal.redis/key/last_pg_time; echo"

green "done!  now try:  python3 demos/apps/python/demo.py   or   java demos/apps/java/Main.java"
