curl http://mylocal.hello/ping
curl http://mylocal.shim/routes | jq
info
curl 'http://mylocal.postgres/query?sql=select+now()' | jq
pgq "select relname from pg_class where relkind='r' limit 5"
curl -X PUT http://mylocal.redis/key/greeting --data-binary 'hello from curl'
curl http://mylocal.redis/key/greeting
curl 'http://mylocal.redis/keys?pattern=*'
kset foo bar
kget foo
curl -X POST http://mylocal.mongo/db/demo/coll/users -H 'content-type: application/json' -d '{"name":"ada","age":30}'
curl 'http://mylocal.mongo/db/demo/coll/users?filter=%7B%7D' | jq
curl -X POST http://mylocal.kafka/topic/demo/records -d 'k1|hello'
curl 'http://mylocal.kafka/topic/demo/records?limit=10' | jq
curl -X POST http://mylocal.rabbit/queue/demo/publish -d 'hi there'
curl http://mylocal.rabbit/queue/demo/get | jq
curl -X POST http://mylocal.smtp/send -H 'content-type: application/json' -d '{"to":"you@example.com","subject":"hi","body":"sent with curl"}'
curl -X POST http://mylocal.grpc/helloworld.Greeter/SayHello -d '{"name":"matt"}' | jq
curl http://mylocal.grpc/_services | jq
curl -sN http://mylocal.redis/subscribe/chat
kpub chat 'hello from another shell'
curl -sN 'http://mylocal.kafka/topic/demo/consume?from=earliest'
curl -sN http://mylocal.rabbit/queue/demo/consume
pglisten demo
pgnotify demo 'pg says hi'
curl -s 'http://mylocal.postgres/query?sql=select+now()' | jq -r '.rows[0][0]' | curl -s -X PUT http://mylocal.redis/key/last_pg_time --data-binary @-; curl http://mylocal.redis/key/last_pg_time
demo
