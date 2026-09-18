#!/bin/bash
# Check the session in DynamoDB and callback Lambda logs
REGION="us-east-1"
SESSION_ID="printer-e150-network-001"

echo "=== DynamoDB Session Records ==="
aws dynamodb query \
  --table-name AsyncResponseQueue \
  --key-condition-expression "SessionID = :sid" \
  --expression-attribute-values '{":sid": {"S": "'$SESSION_ID'"}}' \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('Items', []):
    seq = item.get('SequenceNumber', {}).get('N', '?')
    status = item.get('SessionStatus', {}).get('S', '?')
    text = item.get('ResponseText', {}).get('S', '')[:80]
    delivered = item.get('Delivered', {}).get('BOOL', '?')
    print(f'  Seq={seq} Status={status} Delivered={delivered} Text={text}')
if not data.get('Items'):
    print('  No records found')
"

echo ""
echo "=== Callback Lambda — last 30 min ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Callback" \
  --start-time $(($(date +%s) * 1000 - 1800000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -v "^$" | head -30

echo ""
echo "=== Mock Nexthink Lambda — last 30 min ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-MockNexThink" \
  --start-time $(($(date +%s) * 1000 - 1800000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -v "^$"
