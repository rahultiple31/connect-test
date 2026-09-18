#!/bin/bash
# Check Wisdom assistant access and a Lex bot's config.
# Usage: ./scripts/check_wisdom_access.sh [BOT_ID]
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

INSTANCE_ID=$(resolve_instance_id)
BOT_ID="${1:-}"

echo "Instance: $INSTANCE_ID"

if [ -z "$BOT_ID" ]; then
  echo "No bot ID given — will list all bots. Pass a bot ID as argument to inspect a specific bot."
fi
echo ""

echo "=== Wisdom Assistant Status ==="
aws wisdom list-assistants \
  --region "$REGION" \
  --query 'assistantSummaries[].{Name:name,Arn:assistantArn,Status:status,Type:type}' \
  --output table

if [ -n "$BOT_ID" ]; then
  echo ""
  echo "=== Bot Details ==="
  aws lexv2-models describe-bot \
    --bot-id "$BOT_ID" \
    --region "$REGION" \
    --query '{Name:botName,Id:botId,Role:roleArn,Status:botStatus}' \
    --output json

  echo ""
  echo "=== Bot Locale ==="
  aws lexv2-models describe-bot-locale \
    --bot-id "$BOT_ID" \
    --bot-version "DRAFT" \
    --locale-id "en_US" \
    --region "$REGION" \
    --query '{Status:botLocaleStatus,Voice:voiceSettings,UnifiedSpeech:unifiedSpeechSettings}' \
    --output json

  echo ""
  echo "=== Bot Intents ==="
  aws lexv2-models list-intents \
    --bot-id "$BOT_ID" \
    --bot-version "DRAFT" \
    --locale-id "en_US" \
    --region "$REGION" \
    --query 'intentSummaries[].{Name:intentName,ParentSignature:parentIntentSignature}' \
    --output table

  echo ""
  echo "=== Bot Role Permissions ==="
  ROLE_ARN=$(aws lexv2-models describe-bot --bot-id "$BOT_ID" --region "$REGION" --query 'roleArn' --output text)
  echo "Role ARN: $ROLE_ARN"

  if echo "$ROLE_ARN" | grep -q "aws-service-role"; then
    echo "  -> This is a Service-Linked Role (SLR)"
    ROLE_NAME=$(echo "$ROLE_ARN" | sed 's/.*\///')
    aws iam list-attached-role-policies --role-name "$ROLE_NAME" --output table 2>/dev/null
    aws iam list-role-policies --role-name "$ROLE_NAME" --output table 2>/dev/null
  else
    echo "  -> This is a custom role"
    ROLE_NAME=$(echo "$ROLE_ARN" | sed 's/.*\///')
    echo ""
    echo "  Inline policies:"
    for policy in $(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames[]' --output text 2>/dev/null); do
      echo "  --- $policy ---"
      aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$policy" --query 'PolicyDocument' --output json 2>/dev/null
    done
    echo ""
    echo "  Trust policy:"
    aws iam get-role --role-name "$ROLE_NAME" --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null
  fi

  echo ""
  echo "=== QInConnectIntent Config ==="
  INTENT_ID=$(aws lexv2-models list-intents \
    --bot-id "$BOT_ID" \
    --bot-version "DRAFT" \
    --locale-id "en_US" \
    --region "$REGION" \
    --query 'intentSummaries[?parentIntentSignature!=`null` && contains(parentIntentSignature, `QInConnect`)].intentId' \
    --output text 2>/dev/null)

  if [ -n "$INTENT_ID" ] && [ "$INTENT_ID" != "None" ]; then
    echo "Intent ID: $INTENT_ID"
    aws lexv2-models describe-intent \
      --bot-id "$BOT_ID" \
      --bot-version "DRAFT" \
      --locale-id "en_US" \
      --intent-id "$INTENT_ID" \
      --region "$REGION" \
      --query '{Name:intentName,ParentSignature:parentIntentSignature,QInConnectConfig:qInConnectIntentConfiguration}' \
      --output json
  else
    echo "No QInConnectIntent found"
  fi
fi

echo ""
echo "=== Wisdom Integration Associations ==="
aws connect list-integration-associations \
  --instance-id "$INSTANCE_ID" \
  --integration-type "WISDOM_ASSISTANT" \
  --region "$REGION" \
  --output json
