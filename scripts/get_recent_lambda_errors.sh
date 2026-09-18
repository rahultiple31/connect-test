#!/bin/bash
# Get recent Lambda logs for Submit and Polling — focus on errors and invocations
REGION="us-east-1"

echo "=== Submit Lambda — last 30 min ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Submit" \
  --start-time $(($(date +%s) * 1000 - 1800000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -v "^$"

echo ""
echo "=== Polling Lambda — last 30 min ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Polling" \
  --start-time $(($(date +%s) * 1000 - 1800000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -v "^$"
