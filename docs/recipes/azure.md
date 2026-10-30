# Azure

`az account get-access-token` prints a token scoped to a resource; the
Resource Manager API is the usual one.

## routes.conf

```ini
az.arm    GET    ${JCI_HANDLERS}/run proxy
```

## backends.conf

```ini
[az.arm]
upstream          = https://management.azure.com
authorization_cmd = printf 'Bearer %s' "$(az account get-access-token --query accessToken -o tsv)"
```

For another resource (Graph, Key Vault, Storage) add
`--resource https://graph.microsoft.com` (etc.) to the command and point
`upstream` at that host.

## Try

```sh
curl -s 'http://az.arm/subscriptions?api-version=2022-12-01' | jq '.value[].displayName'
curl -s "http://az.arm/subscriptions/$SUB/resourceGroups?api-version=2021-04-01" | jq '.value[].name'
curl -s "http://az.arm/subscriptions/$SUB/providers/Microsoft.Compute/virtualMachines?api-version=2024-07-01" | jq '.value[].name'
```

## Notes

- Every ARM call needs `?api-version=`; the error when you forget is clear.
- Tokens last about an hour; the `_cmd` runs per request and `az` caches
  the result, so refresh is automatic.

*Not run live from this repo.*
