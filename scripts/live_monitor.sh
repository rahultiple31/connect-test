#!/bin/bash
# Live monitor — check all Lambda logs from the last 2 minutes
REGION="us-east-1"
WINDOW=$(($(date +%s) * 1000 - 120000))

echo "=== $(date) ==="
echo ""

for FN in Submit Polling Callback MockNexThink; do
  LOGS=$(aws logs filter-log-events \
    --log-group-name "/aws/lambda/ConnectAsync-$FN" \
    --start-time "$WINDOW" \
    --region "$REGION" \
    --query 'events[].message' \
    --output text 2>/dev/null | grep -E "\[INFO\]|\[ERROR\]|Status: timeout")
  if [ -n "$LOGS" ]; then
    echo "--- ConnectAsync-$FN ---"
    echo "$LOGS"
    echo ""
  fi
done

echo "=== Connect Flow ==="
bash scripts/get_latest_connect_logs.sh 2>/dev/null | grep -E "GetUserInput|Error|Results" | tail -5
