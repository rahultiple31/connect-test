#!/bin/bash
# Deploy the CDK stack.
# Config is read from .env (project root) with CLI overrides.
#
# Usage:
#   ./scripts/deploy.sh                          # uses .env
#   ./scripts/deploy.sh --no-mock                # skip mock Nexthink
#   CONNECT_INSTANCE_ARN=arn:... ./scripts/deploy.sh  # env override
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if present (does not override existing env vars)
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

# Required
: "${CONNECT_INSTANCE_ARN:?Set CONNECT_INSTANCE_ARN in .env or environment}"
: "${CONNECT_INSTANCE_URL:?Set CONNECT_INSTANCE_URL in .env or environment}"

# Optional (defaults)
DEPLOY_ORCHESTRATOR="${DEPLOY_ORCHESTRATOR:-true}"
DEPLOY_MOCK_NEXTHINK="${DEPLOY_MOCK_NEXTHINK:-true}"
NEXTHINK_BACKEND="${NEXTHINK_BACKEND:-mock}"

# CLI flags: --no-mock  --spark
for arg in "$@"; do
  case "$arg" in
    --no-mock) DEPLOY_MOCK_NEXTHINK=false ;;
    --spark)   NEXTHINK_BACKEND=spark ;;
  esac
done

# Spark backend needs its four tenant-specific values
SPARK_CTX=()
if [[ "$NEXTHINK_BACKEND" == "spark" ]]; then
  : "${NEXTHINK_TENANT_ID:?NEXTHINK_BACKEND=spark requires NEXTHINK_TENANT_ID}"
  : "${NEXTHINK_TOKEN_URL:?NEXTHINK_BACKEND=spark requires NEXTHINK_TOKEN_URL}"
  : "${NEXTHINK_SPARK_URL:?NEXTHINK_BACKEND=spark requires NEXTHINK_SPARK_URL}"
  : "${NEXTHINK_USER_PRINCIPAL:?NEXTHINK_BACKEND=spark requires NEXTHINK_USER_PRINCIPAL}"
  SPARK_CTX=(
    -c nexthink_tenant_id="$NEXTHINK_TENANT_ID"
    -c nexthink_token_url="$NEXTHINK_TOKEN_URL"
    -c nexthink_spark_url="$NEXTHINK_SPARK_URL"
    -c nexthink_user_principal="$NEXTHINK_USER_PRINCIPAL"
  )
  echo "Backend: spark  (callback API key auth will be DISABLED — see infrastructure/stack.py)"
else
  echo "Backend: mock   (Bedrock simulator)"
fi

source "$PROJECT_ROOT/.venv/bin/activate"

cdk deploy --no-rollback \
  -c connect_instance_arn="$CONNECT_INSTANCE_ARN" \
  -c connect_instance_url="$CONNECT_INSTANCE_URL" \
  -c deploy_orchestrator="$DEPLOY_ORCHESTRATOR" \
  -c deploy_mock_nexthink="$DEPLOY_MOCK_NEXTHINK" \
  -c nexthink_backend="$NEXTHINK_BACKEND" \
  ${SPARK_CTX[@]+"${SPARK_CTX[@]}"} \
  --require-approval never
