#!/bin/bash
REGION="us-east-1"

echo "=== Lex logs (last 2h) ==="
aws logs filter-log-events \
  --log-group-name "/aws/lex/P6J5SRSIEC" \
  --start-time $(($(date +%s) * 1000 - 7200000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | head -40
