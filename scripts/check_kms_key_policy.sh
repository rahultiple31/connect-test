#!/bin/bash
# Check KMS key policy and all grants.
# Usage: ./scripts/check_kms_key_policy.sh [KMS_KEY_ID]
#   If no key ID given, resolves from Wisdom assistant's encryption config.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

KMS_KEY="${1:-}"
if [ -z "$KMS_KEY" ]; then
  ASSISTANT_ID=$(resolve_assistant_id)
  KMS_KEY=$(aws wisdom get-assistant \
    --assistant-id "$ASSISTANT_ID" \
    --region "$REGION" \
    --query 'assistant.serverSideEncryptionConfiguration.kmsKeyId' \
    --output text 2>/dev/null)
  echo "Auto-resolved KMS key from Wisdom assistant: $KMS_KEY"
fi

if [ -z "$KMS_KEY" ] || [ "$KMS_KEY" = "None" ]; then
  echo "ERROR: Could not resolve KMS key. Pass it as argument: $0 <key-id>"
  exit 1
fi

echo ""
echo "=== KMS Key Description ==="
aws kms describe-key \
  --key-id "$KMS_KEY" \
  --region "$REGION" \
  --query 'KeyMetadata.{Id:KeyId,State:KeyState,Manager:KeyManager}' \
  --output json

echo ""
echo "=== KMS Key Policy ==="
aws kms get-key-policy \
  --key-id "$KMS_KEY" \
  --policy-name default \
  --region "$REGION" \
  --output text 2>&1

echo ""
echo "=== All KMS Grants ==="
aws kms list-grants \
  --key-id "$KMS_KEY" \
  --region "$REGION" \
  --query 'Grants[].{Name:Name,Grantee:GranteePrincipal,Ops:Operations}' \
  --output json
