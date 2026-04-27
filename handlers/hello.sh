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

# Method allow-list (set by the shim from routes.conf).
if [[ -n "${INTERCEPT_METHODS-}" && "$INTERCEPT_METHODS" != *'*'* ]]; then
    method="${request_line%% *}"
    if [[ ",${INTERCEPT_METHODS}," != *",${method},"* ]]; then
        body='{"error":"method '"$method"' not allowed","allow":"'"$INTERCEPT_METHODS"'"}'
        printf 'HTTP/1.1 405 Method Not Allowed\r\n'
        printf 'Content-Type: application/json\r\n'
        printf 'Allow: %s\r\n' "$INTERCEPT_METHODS"
        printf 'Content-Length: %d\r\n' "${#body}"
        printf 'X-Handled-By: jci-gate\r\n'
        printf 'Connection: close\r\n\r\n'
        printf '%s' "$body"
        exit 0
    fi
fi

body='{"handler":"hello.sh","env":"'"${JCI_ENV-}"'","host":"'"${INTERCEPT_HOST-}"'","request":"'"$request_line"'","pid":'"$$"'}'
printf 'HTTP/1.1 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Content-Length: %d\r\n' "${#body}"
printf 'Connection: close\r\n'
printf 'X-Handled-By: jci-hello\r\n'
printf '\r\n'
printf '%s' "$body"
