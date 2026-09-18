#!/bin/bash
# Check for Orchestrator / Wisdom / Q in Connect logs
REGION="us-east-1"

echo "=== Wisdom-related log groups ==="
aws logs describe-log-groups \
  --log-group-name-prefix "/aws/wisdom" \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text 2>/dev/null || echo "None"

aws logs describe-log-groups \
  --log-group-name-prefix "/aws/qconnect" \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text 2>/dev/null || echo "None"

aws logs describe-log-groups \
  --log-group-name-prefix "/aws/connect/wisdom" \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text 2>/dev/null || echo "None"

echo ""
echo "=== Recent Submit Lambda invocations (last hour) ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Submit" \
  --start-time $(($(date +%s) * 1000 - 3600000)) \
  --filter-pattern "REPORT" \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | head -10 || echo "No recent invocations"

echo ""
echo "=== Recent Polling Lambda invocations (last hour) ==="
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ConnectAsync-Polling" \
  --start-time $(($(date +%s) * 1000 - 3600000)) \
  --filter-pattern "REPORT" \
  --region "$REGION" \
  --query 'events[].message' \
  --output text 2>/dev/null | head -10 || echo "No recent invocations"

echo ""
echo "=== CloudTrail: Recent Gateway invocations ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=bedrock-agentcore.amazonaws.com \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 5 \
  --region "$REGION" \
  --query 'Events[].{Time:EventTime,Name:EventName}' \
  --output table 2>/dev/null || echo "No events"

echo ""
echo "=== CloudTrail: Recent Wisdom events ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=wisdom.amazonaws.com \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 10 \
  --region "$REGION" \
  --query 'Events[].{Time:EventTime,Name:EventName}' \
  --output table 2>/dev/null || echo "No events"
