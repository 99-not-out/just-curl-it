# smtp

Send mail with a `curl -X POST`. Stdlib only, nothing to install. Point it
at MailHog or Mailpit for development, or at a real relay with STARTTLS and
a login for the odd notification script.

## Routes

```
POST /send    JSON body: {"to": "a@x" | ["a@x","b@y"], "subject"?, "body"?, "html"?, "cc"?, "from"?}
              -> {"sent": true, "to", "subject", "ui"}
GET  /_info
```

```sh
curl -X POST http://mylocal.smtp/send -H 'content-type: application/json' \
     -d '{"to":"you@example.com","subject":"hi","body":"sent with curl"}'
```

## backends.conf

```ini
[mylocal.smtp]
smtp_host = localhost
smtp_port = 1025
from      = jci@localhost            # default From when the body has none
ssl       = false                    # true: implicit TLS (usually port 465)
starttls  = false                    # true: STARTTLS after connect (usually 587)
username  = ${SMTP_USER}             # optional; with password -> AUTH
password  = ${SMTP_PASSWORD}
timeout   = 5
ui_url    = http://localhost:8025    # optional; echoed in the response for dev inboxes
```

## Notes

- Only `POST` sends. There is nothing to `GET` here except `/_info`, so a
  `GET`-only route makes this handler inert — by design.
- `html` adds a `text/html` alternative alongside the plain `body`.
- Relay errors (refused recipient, auth failure) come back as `502` with
  the server's message.
