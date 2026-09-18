#!/bin/bash
# Check for AI agent / Q in Connect CloudWatch logs
REGION="us-east-1"

echo "=== Log groups with 'wisdom' or 'qconnect' or 'ai-agent' ==="
aws logs describe-log-groups \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text 2>/dev/null | tr '\t' '\n' | grep -iE "wisdom|qconnect|ai.agent|connect.*agent"

echo ""
echo "=== All log groups (looking for anything relevant) ==="
aws logs describe-log-groups \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text 2>/dev/null | tr '\t' '\n' | grep -iE "connect|lex|wisdom"

echo ""
echo "=== Check if AI agent logging is enabled ==="
echo "Per docs: enable via Monitor Connect AI agents page"
echo "Log entries have event_type=TRANSCRIPT_SELF_SERVICE_MESSAGE"
echo ""
echo "To enable: Connect admin → Analytics → AI agent monitoring → Enable CloudWatch logging"
