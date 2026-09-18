#!/bin/bash
# Check CloudTrail for Lex RecognizeUtterance / StartConversation errors
REGION="us-east-1"

echo "=== Recent Lex CloudTrail events ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=models-v2-lex.us-east-1.amazonaws.com \
  --start-time "2026-03-02T00:00:00Z" \
  --max-results 5 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('Events', []):
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    print(f\"Time: {e.get('EventTime')}\")
    print(f\"Event: {e.get('EventName')}\")
    print(f\"Error: {ct.get('errorCode', 'none')} - {ct.get('errorMessage', 'none')}\")
    print(f\"Source IP: {ct.get('sourceIPAddress')}\")
    req = ct.get('requestParameters', {})
    if req:
        print(f\"Request: {json.dumps(req)[:200]}\")
    print('---')
" 2>/dev/null || echo "No events found for models-v2-lex"

echo ""
echo "=== Try runtime-v2-lex ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=runtime-v2-lex.us-east-1.amazonaws.com \
  --start-time "2026-03-02T00:00:00Z" \
  --max-results 5 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('Events', []):
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    print(f\"Time: {e.get('EventTime')}\")
    print(f\"Event: {e.get('EventName')}\")
    print(f\"Error: {ct.get('errorCode', 'none')} - {ct.get('errorMessage', 'none')}\")
    print('---')
" 2>/dev/null || echo "No events found for runtime-v2-lex"

echo ""
echo "=== Try bedrock ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=bedrock.amazonaws.com \
  --start-time "2026-03-02T00:00:00Z" \
  --max-results 5 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('Events', []):
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    print(f\"Time: {e.get('EventTime')}\")
    print(f\"Event: {e.get('EventName')}\")
    print(f\"Error: {ct.get('errorCode', 'none')} - {ct.get('errorMessage', 'none')}\")
    print('---')
" 2>/dev/null || echo "No events found for bedrock"

echo ""
echo "=== Try wisdom ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=wisdom.amazonaws.com \
  --start-time "2026-03-02T00:00:00Z" \
  --max-results 5 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('Events', []):
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    print(f\"Time: {e.get('EventTime')}\")
    print(f\"Event: {e.get('EventName')}\")
    print(f\"Error: {ct.get('errorCode', 'none')} - {ct.get('errorMessage', 'none')}\")
    print('---')
" 2>/dev/null || echo "No events found for wisdom"
