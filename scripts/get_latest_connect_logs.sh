#!/bin/bash
# Get the latest Connect log stream and events.
# Usage: ./scripts/get_latest_connect_logs.sh [INSTANCE_ALIAS]
#   (alias auto-discovered from the first Connect instance if omitted)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

INSTANCE_ALIAS="$(resolve_instance_alias "${1:-}")"
LOG_GROUP="/aws/connect/$INSTANCE_ALIAS"

echo "=== Latest log streams ==="
aws logs describe-log-streams \
  --log-group-name "$LOG_GROUP" \
  --order-by LastEventTime \
  --descending \
  --limit 3 \
  --region "$REGION" \
  --query 'logStreams[].{Name:logStreamName,LastEvent:lastEventTimestamp}' \
  --output table

echo ""
echo "=== Events from latest stream ==="
LATEST_STREAM=$(aws logs describe-log-streams \
  --log-group-name "$LOG_GROUP" \
  --order-by LastEventTime \
  --descending \
  --limit 1 \
  --region "$REGION" \
  --query 'logStreams[0].logStreamName' \
  --output text)

echo "Stream: $LATEST_STREAM"
echo ""

aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LATEST_STREAM" \
  --region "$REGION" \
  --query 'events[].message' \
  --output text
