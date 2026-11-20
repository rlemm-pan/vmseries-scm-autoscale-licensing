#!/usr/bin/env bash
# Deploy the lifecycle package to a Flex Consumption Function App with Azure's "one deploy" API
# (remote build installs requirements.txt). Used by terraform-azure instead of the provider's
# zip_deploy_file, which can fail on Flex with "waiting for deployment service to be ready: 404".
# Needs: az (signed in), curl, python3.   Usage: azure-deploy-function.sh <function-app-name> <zip>
set -euo pipefail
APP="$1"; ZIP="$2"
token() { PYTHONWARNINGS=ignore az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv; }
SCM="https://${APP}.scm.azurewebsites.net"
ID=""
for attempt in 1 2 3 4 5 6; do   # the deployment endpoint can take a minute to come up on a new app
  code=$(curl -sS -o /tmp/.fn-deploy.$$ -w '%{http_code}' -X POST "$SCM/api/publish?RemoteBuild=true&Deployer=terraform" \
    -H "Authorization: Bearer $(token)" -H "Content-Type: application/zip" --data-binary @"$ZIP" --max-time 600 || true)
  if [ "$code" = "202" ] || [ "$code" = "200" ]; then ID=$(tr -d '"' < /tmp/.fn-deploy.$$); break; fi
  echo "publish attempt $attempt: HTTP $code; retrying in 30s" >&2; sleep 30
done
rm -f /tmp/.fn-deploy.$$
[ -n "$ID" ] || { echo "publish failed" >&2; exit 1; }
echo "deployment $ID accepted; waiting for build"
for _ in $(seq 1 60); do
  st=$(curl -sS "$SCM/api/deployments/$ID" -H "Authorization: Bearer $(token)" --max-time 60 |
       python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status"), d.get("status_text") or "")' 2>/dev/null || echo "? ")
  case "$st" in
    4\ *) echo "deployment succeeded"; exit 0 ;;
    3\ *) echo "deployment failed: $st (see $SCM/api/deployments/$ID/log)" >&2; exit 1 ;;
  esac
  sleep 15
done
echo "timed out waiting for deployment $ID" >&2; exit 1
