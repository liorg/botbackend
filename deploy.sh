#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Load deployment variables
set -a
source .env
set +a

echo "Validating scenario stack..."
docker stack config -c compose.yml >/dev/null

echo "Deploying scenario stack..."
docker stack deploy \
  --with-registry-auth \
  -c compose.yml \
  scenario

echo
echo "Scenario stack:"
docker stack services scenario