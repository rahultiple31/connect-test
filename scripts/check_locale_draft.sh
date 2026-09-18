#!/bin/bash
# Compare DRAFT vs published locale settings
# Usage: ./scripts/check_locale_draft.sh [BOT_ID]
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

BOT_ID="$(resolve_bot_id "${1:-}")"

echo "=== DRAFT locale ==="
aws lexv2-models describe-bot-locale \
  --bot-id "$BOT_ID" \
  --bot-version "DRAFT" \
  --locale-id "en_US" \
  --region "$REGION" \
  --query '{Status:botLocaleStatus,Voice:voiceSettings,UnifiedSpeech:unifiedSpeechSettings}' \
  --output json

echo ""
echo "=== Version 3 locale ==="
aws lexv2-models describe-bot-locale \
  --bot-id "$BOT_ID" \
  --bot-version "3" \
  --locale-id "en_US" \
  --region "$REGION" \
  --query '{Status:botLocaleStatus,Voice:voiceSettings,UnifiedSpeech:unifiedSpeechSettings}' \
  --output json
