# Prometheus

Plain HTTP, often no auth — the proxy here is just a nice hostname and a
`GET`-only guarantee. Also a reminder that `upstream` can be `http://`.

## routes.conf

```ini
prom    GET    ${JCI_HANDLERS}/run proxy
```

## backends.conf

```ini
[prom]
upstream = http://prometheus.monitoring.svc:9090
# authorization = Basic ${PROM_BASIC}        # if fronted by basic auth
```

## Try

```sh
curl -sG http://prom/api/v1/query --data-urlencode 'query=up' | jq '.data.result[] | .metric.instance + " " + .value[1]'
curl -sG http://prom/api/v1/query_range --data-urlencode 'query=rate(http_requests_total[5m])' \
     --data-urlencode "start=$(date -u -v-1H +%s)" --data-urlencode "end=$(date -u +%s)" --data-urlencode 'step=60' | jq '.data.result | length'
curl -s http://prom/api/v1/targets | jq '.data.activeTargets[] | .labels.job + " " + .health'
```

## Notes

- Grafana, Alertmanager, Loki and Tempo are the same shape; one route each.
- The Prometheus API's `POST /api/v1/query` form exists for long queries;
  on a `GET`-only route use `-G --data-urlencode` as above.

*Run live only against the shape; no Prometheus in the demo compose.*
