#!/bin/bash
# Simpler approach — scan DynamoDB directly and check recent logs
REGION="us-east-1"

echo "=== All DynamoDB sessions ==="
aws dynamodb scan \
  --table-name AsyncResponseQueue \
  --region "$REGION" \
  --output json | python3 -c "
import json, sys, datetime
data = json.load(sys.stdin)
items = data.get('Items', [])
# Group by session
sessions = {}
for item in items:
    sid = item.get('SessionID', {}).get('S', '?')
    seq = int(item.get('SequenceNumber', {}).get('N', -1))
    if sid not in sessions:
        sessions[sid] = []
    sessions[sid].append(item)

for sid, records in sorted(sessions.items(), key=lambda x: int(x[1][0].get('CreatedAt', {}).get('N', '0'))):
    sentinel = [r for r in records if int(r.get('SequenceNumber', {}).get('N', -1)) == 0]
    responses = [r for r in records if int(r.get('SequenceNumber', {}).get('N', -1)) > 0]
    status = sentinel[0].get('SessionStatus', {}).get('S', '?') if sentinel else '?'
    ts = int(sentinel[0].get('CreatedAt', {}).get('N', '0')) if sentinel else 0
    t = datetime.datetime.fromtimestamp(ts/1000) if ts else '?'
    query = sentinel[0].get('QueryText', {}).get('S', '')[:60] if sentinel else ''
    print(f'{sid}  status={status}  responses={len(responses)}  time={t}')
    print(f'  query: {query}')
    for r in sorted(responses, key=lambda x: int(x.get('SequenceNumber', {}).get('N', 0))):
        seq = r.get('SequenceNumber', {}).get('N', '?')
        text = r.get('ResponseText', {}).get('S', '')[:80]
        delivered = r.get('Delivered', {}).get('BOOL', '?')
        print(f'  seq={seq} delivered={delivered} text={text}')
    print()
"

echo ""
echo "=== Submit Lambda — last 15 min (INFO/ERROR only) ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Submit" \
  --start-time $(($(date +%s) * 1000 - 900000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -E "\[INFO\]|\[ERROR\]" | tail -10

echo ""
echo "=== Mock Nexthink — last 15 min (INFO/ERROR only) ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-MockNexThink" \
  --start-time $(($(date +%s) * 1000 - 900000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -E "\[INFO\]|\[ERROR\]" | tail -10

echo ""
echo "=== Callback Lambda — last 15 min ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Callback" \
  --start-time $(($(date +%s) * 1000 - 900000)) \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | grep -E "\[INFO\]|\[ERROR\]|START|END" | tail -10
