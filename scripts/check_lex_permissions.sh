#!/bin/bash
# Deep check of Lex bot role permissions and SLR
# Usage: ./scripts/check_lex_permissions.sh [BOT_ID]
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

BOT_ID="$(resolve_bot_id "${1:-}")"

ROLE_ARN=$(aws lexv2-models describe-bot \
  --bot-id "$BOT_ID" \
  --region "$REGION" \
  --query 'roleArn' --output text)

ROLE_NAME=$(echo "$ROLE_ARN" | sed 's/.*\///')
echo "=== Bot Role: $ROLE_NAME ==="
echo "ARN: $ROLE_ARN"

echo ""
echo "=== Inline Policies ==="
for policy in $(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames[]' --output text); do
  echo "--- Policy: $policy ---"
  aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$policy" --output json
done

echo ""
echo "=== Attached Policies ==="
aws iam list-attached-role-policies --role-name "$ROLE_NAME" --output json

echo ""
echo "=== Trust Policy ==="
aws iam get-role --role-name "$ROLE_NAME" --query 'Role.AssumeRolePolicyDocument' --output json

echo ""
echo "=== Lex Service-Linked Role ==="
# Lex creates one SLR per bot, suffixed with the bot ID; fall back to the unsuffixed name
aws iam get-role --role-name "AWSServiceRoleForLexV2Bots_$BOT_ID" 2>/dev/null \
  || aws iam get-role --role-name "AWSServiceRoleForLexV2Bots" 2>/dev/null \
  || echo "No Lex SLR found"

echo ""
echo "=== Check if bot uses SLR or custom role ==="
echo "Bot role: $ROLE_ARN"
echo "If this is a custom role (not aws-service-role), Wisdom + Bedrock permissions must be added manually."

echo ""
echo "=== CloudTrail: Recent Lex errors ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=runtime-v2-lex.us-east-1.amazonaws.com \
  --max-results 5 \
  --region "$REGION" \
  --query 'Events[].{Time:EventTime,Name:EventName,Error:CloudTrailEvent}' \
  --output text 2>/dev/null | head -20 || echo "No CloudTrail events found"
