# Interactive bash for the intercept shim demo.
[ -f /etc/motd ] && cat /etc/motd

export PS1='\[\033[1;32m\]shim\[\033[0m\]:\w\$ '
alias ll='ls -alF'

# Tiny DSL over the shim -- all of these are just curl underneath.
kget()   { curl -s "http://mylocal.redis/key/$1"; echo; }
kset()   { curl -s -X PUT "http://mylocal.redis/key/$1" --data-binary "$2"; echo; }
ksub()   { curl -sN "http://mylocal.redis/subscribe/${1:-chat}"; }
kpub()   { curl -s -X POST "http://mylocal.redis/publish/${1:-chat}" --data-binary "$2"; echo; }

pgq()    { curl -sG http://mylocal.postgres/query --data-urlencode "sql=$*" | jq; }
pglisten() { curl -sN "http://mylocal.postgres/listen/${1:-demo}"; }
pgnotify() { pgq "select pg_notify('${1:-demo}', '${2:-hi}')" >/dev/null; }

kprod()  { curl -s -X POST "http://mylocal.kafka/topic/$1/records" --data-binary "$2" | jq; }
kpeek()  { curl -s "http://mylocal.kafka/topic/$1/records?limit=${2:-10}" | jq; }
kconsume(){ curl -sN "http://mylocal.kafka/topic/$1/consume?from=${2:-latest}"; }

rpub()   { curl -s -X POST "http://mylocal.rabbit/queue/$1/publish" --data-binary "$2" | jq; }
rget()   { curl -s "http://mylocal.rabbit/queue/$1/get" | jq; }
rconsume(){ curl -sN "http://mylocal.rabbit/queue/$1/consume"; }

mfind()  { curl -sG "http://mylocal.mongo/db/$1/coll/$2" --data-urlencode "filter=${3:-{}}" | jq; }
minsert(){ curl -s -X POST "http://mylocal.mongo/db/$1/coll/$2" -H 'content-type: application/json' --data-binary "$3" | jq; }

mail_send(){ curl -s -X POST http://mylocal.smtp/send -H 'content-type: application/json' --data-binary "$1" | jq; }
grpc_call(){ curl -s -X POST "http://mylocal.grpc/$1" -H 'content-type: application/json' --data-binary "${2:-{}}" | jq; }

alias info='for h in hello shim kafka redis mongo postgres rabbit smtp grpc; do echo "=== $h ==="; curl -s "http://mylocal.$h/_info" | jq -c; done'
alias routes='curl -s http://mylocal.shim/routes | jq'
