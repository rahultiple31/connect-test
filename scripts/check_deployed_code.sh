#!/bin/bash
# Download and inspect deployed Lambda code
REGION="us-east-1"
TMPDIR=$(mktemp -d)

for FN in ConnectAsync-Submit ConnectAsync-Polling ConnectAsync-Callback; do
  echo "=== $FN ==="
  URL=$(aws lambda get-function --function-name "$FN" --region "$REGION" --query 'Code.Location' --output text)
  curl -s "$URL" -o "$TMPDIR/$FN.zip"
  unzip -q -o "$TMPDIR/$FN.zip" -d "$TMPDIR/$FN"
  echo "--- handler.py (first 30 lines + logger lines) ---"
  head -30 "$TMPDIR/$FN/handler.py"
  echo "..."
  grep -n "logger.info\|logger.error\|CALLBACK_API_URL" "$TMPDIR/$FN/handler.py" 2>/dev/null
  echo ""
done

rm -rf "$TMPDIR"
