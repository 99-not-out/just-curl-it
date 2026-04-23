#!/bin/bash
# Tiny demo handler: echoes the request line back as JSON.
request_line=""
while IFS= read -r line; do
    line="${line%$'\r'}"
    [[ -z "$request_line" ]] && request_line="$line"
    [[ -z "$line" ]] && break
done

body='{"handler":"hello.sh","host":"'"$INTERCEPT_HOST"'","request_line":"'"$request_line"'","pid":'"$$"'}'
printf 'HTTP/1.1 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Content-Length: %d\r\n' "${#body}"
printf 'Connection: close\r\n'
printf 'X-Handled-By: intercept-hello\r\n'
printf '\r\n'
printf '%s' "$body"
