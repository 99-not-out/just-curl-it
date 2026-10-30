#!/usr/bin/env bash
# Minimal handler. Reads an HTTP request on stdin, writes an HTTP response
# on stdout. Echoes routing context so you can sanity-check the shim is wired
# up before adding real protocol handlers.
request_line=""
while IFS= read -r line; do
    line="${line%$'\r'}"
    [[ -z "$request_line" ]] && request_line="$line"
    [[ -z "$line" ]] && break
done

# No method check here: the shim enforces the route's allow-list before the
# handler is ever spawned, so a denied request never reaches this script.
body='{"handler":"hello.sh","env":"'"${JCI_ENV-}"'","host":"'"${INTERCEPT_HOST-}"'","request":"'"$request_line"'","pid":'"$$"'}'
printf 'HTTP/1.1 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Content-Length: %d\r\n' "${#body}"
printf 'Connection: close\r\n'
printf 'X-Handled-By: jci-hello\r\n'
printf '\r\n'
printf '%s' "$body"
