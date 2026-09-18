#!/bin/bash
# Check all Lambda logs from the last 10 minutes
REGION="us-east-1"
WINDOW=$(($(date +%s) * 1000 - 600000))

for FN in Submit Polling Callback MockNexThink; do
  echo "=== ConnectAsync-$FN ==="
  aws logs filter-log-events \
    --log-group-name "/aws/lambda/ConnectAsync-$FN" \
    --start-time "$WINDOW" \
    --region "$REGION" \
    --query 'events[].message' \
    --output text 2>/dev/null | grep -E "INFO|ERROR|REPORT|ImportModule" | tail -5
  echo ""
done
