#!/bin/bash
# Quick smoke test of Submit and Polling Lambdas
REGION="us-east-1"

echo "=== Test Submit Lambda ==="
aws lambda invoke \
  --function-name ConnectAsync-Submit \
  --payload '{"transcript":"test query","sessionId":"smoke-test-001","callbackUrl":"https://example.com/callback"}' \
  --cli-binary-format raw-in-base64-out \
  --region "$REGION" \
  /dev/stdout 2>/dev/null

echo ""
echo ""
echo "=== Test Polling Lambda ==="
aws lambda invoke \
  --function-name ConnectAsync-Polling \
  --payload '{"sessionId":"smoke-test-001","cursor":0}' \
  --cli-binary-format raw-in-base64-out \
  --region "$REGION" \
  /dev/stdout 2>/dev/null

echo ""
