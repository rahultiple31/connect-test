#!/bin/bash
# Deep check of the Connect-console-created bot
REGION="us-east-1"
BOT_ID="ZTCD0QS50N"

echo "=== Bot Aliases ==="
aws lexv2-models list-bot-aliases \
  --bot-id "$BOT_ID" \
  --region "$REGION" \
  --output json

echo ""
echo "=== Bot Versions ==="
aws lexv2-models list-bot-versions \
  --bot-id "$BOT_ID" \
  --region "$REGION" \
  --query 'botVersionSummaries[].{Version:botVersion,Status:botStatus}' \
  --output table

echo ""
echo "=== DRAFT Locale - QInConnect Intent Detail ==="
INTENT_ID=$(aws lexv2-models list-intents \
  --bot-id "$BOT_ID" \
  --bot-version "DRAFT" \
  --locale-id "en_US" \
  --region "$REGION" \
  --query 'intentSummaries[?parentIntentSignature!=`null` && contains(parentIntentSignature, `QInConnect`)].intentId' \
  --output text)

echo "Intent ID: $INTENT_ID"
aws lexv2-models describe-intent \
  --bot-id "$BOT_ID" \
  --bot-version "DRAFT" \
  --locale-id "en_US" \
  --intent-id "$INTENT_ID" \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f\"Name: {data.get('intentName')}\")
print(f\"Parent: {data.get('parentIntentSignature')}\")
config = data.get('qInConnectIntentConfiguration', {})
assistant_config = config.get('qInConnectAssistantConfiguration', {})
print(f\"Assistant ARN: {assistant_config.get('assistantArn', 'NOT SET')}\")
"

echo ""
echo "=== TSTALIASID Alias Detail ==="
aws lexv2-models describe-bot-alias \
  --bot-id "$BOT_ID" \
  --bot-alias-id "TSTALIASID" \
  --region "$REGION" \
  --query '{Name:botAliasName,Version:botVersion,Status:botAliasStatus}' \
  --output json

echo ""
echo "=== Check if there's a published version with the intent ==="
for ver in $(aws lexv2-models list-bot-versions --bot-id "$BOT_ID" --region "$REGION" --query 'botVersionSummaries[].botVersion' --output text 2>/dev/null); do
  echo "--- Version $ver ---"
  aws lexv2-models list-intents \
    --bot-id "$BOT_ID" \
    --bot-version "$ver" \
    --locale-id "en_US" \
    --region "$REGION" \
    --query 'intentSummaries[].{Name:intentName,Parent:parentIntentSignature}' \
    --output table 2>/dev/null
done
