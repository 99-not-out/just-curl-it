# Slack

Bot or user token as a bearer. Slack's Web API is all under `/api/`, and
reads are `GET` while posting is `POST` — so a `GET`-only route is
"can read channels, can't post", exactly.

## routes.conf

```ini
slack.api    GET    ${JCI_HANDLERS}/run proxy
```

## backends.conf

```ini
[slack.api]
upstream               = https://slack.com
authorization_keychain = slack-bot-token         # stored as 'Bearer xoxb-...'
# or: authorization = Bearer ${SLACK_BOT_TOKEN}
```

## Try

```sh
curl -s http://slack.api/api/auth.test | jq .
curl -s 'http://slack.api/api/conversations.list?types=public_channel&limit=200' | jq -r '.channels[] | .id + " " + .name'
curl -sG http://slack.api/api/conversations.history --data-urlencode "channel=$CH" --data-urlencode 'limit=20' \
  | jq -r '.messages[] | .user + ": " + .text'
curl -s -X POST http://slack.api/api/chat.postMessage -H 'content-type: application/json' \
  -d "{\"channel\":\"$CH\",\"text\":\"hello from curl\"}"        # 405 on the GET-only route
```

## Notes

- Slack returns HTTP 200 with `"ok": false` on API errors; check `.ok`,
  not the status code.
- Pagination is `response_metadata.next_cursor` → `?cursor=`.

*Not run live from this repo.*
