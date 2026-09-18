#!/bin/bash
# Check Wisdom assistant for resource policies and encryption config
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

ASSISTANT_ID=$(resolve_assistant_id "${1:-}")
echo "Assistant ID: $ASSISTANT_ID"
echo ""

echo "=== Wisdom Assistant Full Details ==="
aws wisdom get-assistant \
  --assistant-id "$ASSISTANT_ID" \
  --region "$REGION" \
  --output json

echo ""
echo "=== KMS Key used by Wisdom ==="
KMS_KEY=$(aws wisdom get-assistant \
  --assistant-id "$ASSISTANT_ID" \
  --region "$REGION" \
  --query 'assistant.serverSideEncryptionConfiguration.kmsKeyId' \
  --output text 2>/dev/null)
echo "KMS Key: $KMS_KEY"

if [ -n "$KMS_KEY" ] && [ "$KMS_KEY" != "None" ]; then
  echo ""
  echo "=== KMS Key Policy ==="
  aws kms get-key-policy \
    --key-id "$KMS_KEY" \
    --policy-name default \
    --region "$REGION" \
    --output text 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Could not read key policy"

  echo ""
  echo "=== KMS Key Grants ==="
  aws kms list-grants \
    --key-id "$KMS_KEY" \
    --region "$REGION" \
    --query 'Grants[].{Grantee:GranteePrincipal,Operations:Operations}' \
    --output json 2>/dev/null
fi
