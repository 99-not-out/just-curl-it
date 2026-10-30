# mongo

Find, insert and delete MongoDB documents with curl. Filters, sorts and
documents are MongoDB Extended JSON, so `{"$oid": ...}` and `{"$date": ...}`
round-trip. Needs `jci handler install mongo`.

```sh
curl -X POST http://mylocal.mongo/db/app/coll/users -H 'content-type: application/json' \
     -d '{"name":"ada","age":36}'
curl -G http://mylocal.mongo/db/app/coll/users --data-urlencode 'filter={"age":{"$gt":30}}' \
     --data-urlencode 'sort={"age":-1}' | jq '.documents[].name'
```

## Routes

```
GET    /dbs                                          {"databases": [...]}
GET    /db/{d}/colls                                 {"collections": [...]}
GET    /db/{d}/coll/{c}?filter={json}&limit=50&sort={json}   {"count", "documents"}
POST   /db/{d}/coll/{c}                              body = one document, or an array -> insert_many
DELETE /db/{d}/coll/{c}?filter={json}                delete_many -> {"deletedCount"}
GET    /_info
```

## backends.conf

```ini
[mylocal.mongo]
uri     = mongodb+srv://app:${MONGO_PASSWORD}@cluster0.example.net/app?tls=true
timeout = 5          # serverSelectionTimeoutMS and connectTimeoutMS = timeout*1000
```

Auth, TLS and replica-set options all live in the URI, as pymongo expects.

## Notes

- `GET` only reads (ADR 0005). There is no update route; `DELETE` with an
  empty filter deletes everything in the collection, so put `GET` on routes
  an agent gets.
- Bad filter JSON is a `400`; a server that can't be reached is a `502`
  after `timeout` seconds.
- `limit` defaults to 50 on `GET`.
