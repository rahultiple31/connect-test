#!/bin/bash
# Get transcript using the v2 API for a contact.
# Usage: ./scripts/get_transcript_v2.sh <CONTACT_ID> [INSTANCE_ID]
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

CONTACT_ID="${1:?Usage: $0 <CONTACT_ID> [INSTANCE_ID]}"
INSTANCE_ID=$(resolve_instance_id "${2:-}")

echo "Instance: $INSTANCE_ID"
echo "Contact:  $CONTACT_ID"
echo ""

echo "=== Real-time Analysis Segments ==="
aws connect list-realtime-contact-analysis-segments-v2 \
  --instance-id "$INSTANCE_ID" \
  --contact-id "$CONTACT_ID" \
  --output-type "Raw" \
  --segment-types "Transcript" \
  --region "$REGION" \
  --output json 2>&1 | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    segments = data.get('segments', [])
    if not segments:
        print('No segments found')
    for seg in segments:
        rt = seg.get('realTimeContactAnalysis', {})
        t = rt.get('transcript', seg.get('transcript', {}))
        if t:
            role = t.get('participantRole', t.get('ParticipantRole', '?'))
            content = t.get('content', t.get('Content', ''))
            print(f'{role}: {content}')
except json.JSONDecodeError as e:
    print(f'JSON error: {e}')
    print(sys.stdin.read())
except Exception as e:
    print(f'Error: {e}')
"

echo ""
echo "=== Contact Details ==="
aws connect describe-contact \
  --instance-id "$INSTANCE_ID" \
  --contact-id "$CONTACT_ID" \
  --region "$REGION" \
  --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
c = data.get('Contact', {})
print(f'Channel: {c.get(\"Channel\")}')
print(f'Init: {c.get(\"InitiationTimestamp\")}')
print(f'Disconnect: {c.get(\"DisconnectTimestamp\")}')
print(f'Init Method: {c.get(\"InitiationMethod\")}')
wi = c.get('WisdomInfo', {})
if wi:
    print(f'Wisdom Session: {wi.get(\"sessionArn\")}')
tags = c.get('Tags', {})
if tags:
    print(f'Tags: {json.dumps(tags)}')
"
