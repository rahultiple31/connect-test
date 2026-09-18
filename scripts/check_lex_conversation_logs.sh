#!/bin/bash
# Check if Lex conversation logs are enabled and look for errors
# Usage: ./scripts/check_lex_conversation_logs.sh [BOT_ID] [ALIAS_ID]
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

BOT_ID="$(resolve_bot_id "${1:-}")"
ALIAS_ID="$(resolve_bot_alias_id "$BOT_ID" "${2:-}")"

echo "=== Bot Alias Details ==="
aws lexv2-models describe-bot-alias \
  --bot-id "$BOT_ID" \
  --bot-alias-id "$ALIAS_ID" \
  --region "$REGION" \
  --output json

echo ""
echo "=== Bot Locale Details ==="
aws lexv2-models describe-bot-locale \
  --bot-id "$BOT_ID" \
  --bot-version "3" \
  --locale-id "en_US" \
  --region "$REGION" \
  --query '{Status:botLocaleStatus,Voice:voiceSettings,UnifiedSpeech:unifiedSpeechSettings,Failures:failureReasons}' \
  --output json

echo ""
echo "=== Lex Bot Role Permissions ==="
# Get the role name from the bot
ROLE_ARN=$(aws lexv2-models describe-bot --bot-id "$BOT_ID" --region "$REGION" --query 'roleArn' --output text)
echo "Role ARN: $ROLE_ARN"
ROLE_NAME=$(echo "$ROLE_ARN" | sed 's/.*\///')
echo "Role Name: $ROLE_NAME"
aws iam list-attached-role-policies --role-name "$ROLE_NAME" --output table 2>/dev/null
aws iam list-role-policies --role-name "$ROLE_NAME" --output table 2>/dev/null
