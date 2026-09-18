#!/bin/bash
# Check CloudTrail for Bedrock errors around the time of the "System instability" error
REGION="us-east-1"

echo "=== Bedrock CloudTrail events (last 2h) ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=bedrock.amazonaws.com \
  --start-time "$(date -u -v-2H +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 20 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('Events', []):
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    error = ct.get('errorCode', '')
    error_msg = ct.get('errorMessage', '')
    name = e.get('EventName', '?')
    time = str(e.get('EventTime', '?'))
    user = ct.get('userIdentity', {}).get('arn', '?')[:80]
    if error:
        print(f'{time}  {name}  ERROR: {error} — {error_msg[:150]}')
        print(f'  User: {user}')
    else:
        print(f'{time}  {name}  OK  User: {user[-40:]}')
"

echo ""
echo "=== Bedrock Runtime errors ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=bedrock-runtime.amazonaws.com \
  --start-time "$(date -u -v-2H +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 10 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('Events', []):
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    error = ct.get('errorCode', '')
    error_msg = ct.get('errorMessage', '')
    name = e.get('EventName', '?')
    time = str(e.get('EventTime', '?'))
    if error:
        print(f'{time}  {name}  ERROR: {error} — {error_msg[:200]}')
    else:
        print(f'{time}  {name}  OK')
"

echo ""
echo "=== Lex Runtime errors ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=runtime-v2-lex.us-east-1.amazonaws.com \
  --start-time "$(date -u -v-2H +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 10 \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
events = data.get('Events', [])
if not events:
    print('No events')
for e in events:
    ct = json.loads(e.get('CloudTrailEvent', '{}'))
    error = ct.get('errorCode', '')
    error_msg = ct.get('errorMessage', '')
    name = e.get('EventName', '?')
    time = str(e.get('EventTime', '?'))
    print(f'{time}  {name}  {\"ERROR: \" + error + \" — \" + error_msg[:150] if error else \"OK\"}')
"
