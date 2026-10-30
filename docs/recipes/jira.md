# Jira Cloud

Basic auth with your email and an API token. The `_cmd` form builds the
header so the base64 never sits in a file.

## routes.conf

```ini
jira.api    GET    ${JCI_HANDLERS}/run proxy      # read issues; drop GET to create/transition
```

## backends.conf

```ini
[jira.api]
upstream          = https://yourteam.atlassian.net
authorization_cmd = printf 'Basic %s' "$(printf '%s:%s' "$JIRA_EMAIL" "$(security find-generic-password -s jira-api-token -w)" | base64)"
header_accept     = application/json
```

## Try

```sh
curl -s http://jira.api/rest/api/3/myself | jq .displayName
curl -sG http://jira.api/rest/api/3/search --data-urlencode 'jql=assignee = currentUser() AND resolution = Unresolved' \
     --data-urlencode 'fields=summary,status' | jq -r '.issues[] | .key + "  " + .fields.status.name + "  " + .fields.summary'
curl -s http://jira.api/rest/api/3/issue/PROJ-123 | jq '.fields.description'
```

## Notes

- Issue descriptions are Atlassian Document Format (nested JSON), not
  text; `jq -r '.. | .text? // empty'` flattens it roughly.
- Confluence Cloud is the same host and auth under `/wiki/rest/api/`.

*Not run live from this repo.*
