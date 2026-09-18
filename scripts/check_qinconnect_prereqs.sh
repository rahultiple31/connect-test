#!/bin/bash
# Check Q in Connect prerequisites
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

INSTANCE_ID=$(resolve_instance_id "${1:-}")
echo "Instance: $INSTANCE_ID"
echo ""

echo "=== Connect Instance ==="
aws connect describe-instance \
  --instance-id "$INSTANCE_ID" \
  --region "$REGION" \
  --query '{Id:Id,Arn:Arn,Status:InstanceStatus}' \
  --output json

echo ""
echo "=== Instance Attributes ==="
aws connect list-instance-attributes \
  --instance-id "$INSTANCE_ID" \
  --region "$REGION" \
  --output table

echo ""
echo "=== Wisdom Assistants ==="
aws wisdom list-assistants \
  --region "$REGION" \
  --query 'assistantSummaries[].{Name:name,Arn:assistantArn,Status:status,Type:type}' \
  --output table

echo ""
echo "=== Integration Associations (WISDOM) ==="
aws connect list-integration-associations \
  --instance-id "$INSTANCE_ID" \
  --integration-type "WISDOM_ASSISTANT" \
  --region "$REGION" \
  --output json 2>&1 || echo "(WISDOM_ASSISTANT type may not be supported in list API)"

echo ""
echo "=== Lex V2 Built-in Intents (check if QinConnect is available) ==="
aws lexv2-models list-built-in-intents \
  --locale-id en_US \
  --region "$REGION" \
  --query 'builtInIntentSummaries[?contains(intentSignature, `QinConnect`)]' \
  --output json
