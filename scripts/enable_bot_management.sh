#!/bin/bash
# Enable bot management and analytics on the Connect instance.
# Required for AMAZON.QinConnectIntent to be available.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

INSTANCE_ID=$(resolve_instance_id "${1:-}")
echo "Instance: $INSTANCE_ID"
echo ""

echo "Enabling BOT_MANAGEMENT..."
aws connect update-instance-attribute \
  --instance-id "$INSTANCE_ID" \
  --attribute-type "BOT_MANAGEMENT" \
  --value "true" \
  --region "$REGION"

echo "Enabling ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS..."
aws connect update-instance-attribute \
  --instance-id "$INSTANCE_ID" \
  --attribute-type "ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS" \
  --value "true" \
  --region "$REGION"

echo ""
echo "Verifying..."
aws connect list-instance-attributes \
  --instance-id "$INSTANCE_ID" \
  --region "$REGION" \
  --query 'Attributes[?AttributeType==`BOT_MANAGEMENT` || AttributeType==`ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS`]' \
  --output table

echo ""
echo "Re-checking built-in intents for QinConnect..."
aws lexv2-models list-built-in-intents \
  --locale-id en_US \
  --region "$REGION" \
  --query 'builtInIntentSummaries[?contains(intentSignature, `Qin`) || contains(intentSignature, `qin`) || contains(intentSignature, `QIn`)]' \
  --output json
