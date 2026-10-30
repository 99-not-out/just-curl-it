# shim-meta

Introspect the active env from inside it: the route table the shim loaded
and the backends it points at, with secrets redacted. No backend of its
own. Useful first stop in an unfamiliar env (`jci-info` calls every
route's `/_info`; this one tells you what the routes *are*).

## Routes

```
GET /           {"routes": [...], "backends": {...}}
GET /routes     [{"host", "methods", "handler"}, ...]   -- methods is "*" when unrestricted
GET /backends   {"<host>": {redacted config}, ...}
GET /_info
```

## backends.conf

None read for itself. `/backends` shows every section of the env's
`backends.conf`, after redaction.

## Notes

- Redaction: keys matching `pass|secret|token|authorization|key` show as
  `***`; URL userinfo (`amqp://user:pw@host`) becomes `amqp://***@host`.
  The point is that an agent can run `jci-info` without a password landing
  in its context.
- Reads `INTERCEPT_CONFIG` and `INTERCEPT_BACKENDS_CONF`, which `jci use`
  exports.
