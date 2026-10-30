# Kubernetes API

The API server is HTTPS with a bearer token and (usually) a private CA.
Both come from your kubeconfig.

## routes.conf

```ini
k8s.api    GET    ${JCI_HANDLERS}/run proxy      # read the cluster; drop GET to mutate
```

## backends.conf

```ini
[k8s.api]
upstream          = https://10.0.0.1:6443                      # kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'
authorization_cmd = printf 'Bearer %s' "$(kubectl create token default -n default --duration=1h)"
ca_cert_cmd       = kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d
```

`ca_cert_cmd` may print the PEM itself (it goes to a `0600` temp file that
is unlinked once loaded). Clusters that authenticate with a client cert
instead: `client_cert_cmd` / `client_key_cmd` printing the kubeconfig's
`client-certificate-data` / `client-key-data`, base64-decoded the same way.

## Try

```sh
curl -s http://k8s.api/api/v1/namespaces | jq '.items[].metadata.name'
curl -s http://k8s.api/api/v1/namespaces/default/pods | jq '.items[] | .metadata.name + " " + .status.phase'
curl -s 'http://k8s.api/apis/apps/v1/deployments?labelSelector=app=web' | jq '.items[].metadata.name'
curl -sN 'http://k8s.api/api/v1/namespaces/default/pods?watch=1'      # newline-delimited JSON, streams
```

## Notes

- `kubectl create token` mints a short-lived ServiceAccount token; pick a
  SA with the RBAC you want the shell to have. A read-only SA plus a
  `GET`-only route is read-only twice.
- Managed clusters (EKS/GKE/AKS) use exec plugins for auth;
  `authorization_cmd = printf 'Bearer %s' "$(aws eks get-token --cluster-name X | jq -r .status.token)"`
  and the GKE/AKS equivalents work the same way.
- Watches (`?watch=1`) stream through fine; `-N` stops curl buffering.

*Not run live from this repo.*
