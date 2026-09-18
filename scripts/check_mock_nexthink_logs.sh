#!/bin/bash
# Check mock Nexthink Lambda logs
REGION="us-east-1"

echo "=== Mock Nexthink Lambda — last 30 min ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-MockNexThink" \
  --start-time $(($(date +%s) * 1000 - 1800000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -v "^$"
