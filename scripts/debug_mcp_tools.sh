#!/bin/bash
# Debug MCP tool invocation chain: AI Agent -> Gateway -> Lambdas
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

ASSISTANT_ID=$(resolve_assistant_id "${1:-}")
echo "Assistant ID: $ASSISTANT_ID"
echo ""

echo "=== 1. AI Agent Configuration (tools) ==="
AGENT_ID=$(aws wisdom list-ai-agents \
  --assistant-id "$ASSISTANT_ID" \
  --region "$REGION" \
  --query 'aiAgentSummaries[0].aiAgentId' \
  --output text 2>/dev/null)
echo "Agent ID: $AGENT_ID"

if [ -n "$AGENT_ID" ] && [ "$AGENT_ID" != "None" ]; then
  aws wisdom get-ai-agent \
    --assistant-id "$ASSISTANT_ID" \
    --ai-agent-id "$AGENT_ID" \
    --region "$REGION" \
    --output json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
agent = data.get('aiAgent', {})
print(f\"Name: {agent.get('name')}\")
print(f\"Type: {agent.get('type')}\")
print(f\"Status: {agent.get('status')}\")
config = agent.get('configuration', {}).get('orchestrationAIAgentConfiguration', {})
print(f\"Prompt ID: {config.get('orchestrationAIPromptId')}\")
tools = config.get('toolConfigurations', [])
print(f\"Tool count: {len(tools)}\")
for t in tools:
  print(f\"  Tool: {json.dumps(t)}\")
"
fi

echo ""
echo "=== 2. AI Agent Versions ==="
if [ -n "$AGENT_ID" ] && [ "$AGENT_ID" != "None" ]; then
  aws wisdom list-ai-agent-versions \
    --assistant-id "$ASSISTANT_ID" \
    --ai-agent-id "$AGENT_ID" \
    --region "$REGION" \
    --query 'aiAgentVersionSummaries[].{Version:versionNumber,Modified:modifiedTime}' \
    --output table 2>/dev/null
fi

echo ""
echo "=== 3. Gateway Status ==="
GATEWAY_ARN=$(aws ssm get-parameter \
  --name "$SSM_PREFIX/agentcore-gateway-arn" \
  --region "$REGION" \
  --query 'Parameter.Value' --output text 2>/dev/null)
echo "Gateway ARN: $GATEWAY_ARN"

echo ""
echo "=== 4. Recent Submit Lambda Logs ==="
aws logs describe-log-groups \
  --log-group-name-prefix "/aws/lambda/ConnectAsync" \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text

echo ""
echo "--- Submit Lambda recent events ---"
SUBMIT_LOG=$(aws logs describe-log-groups \
  --log-group-name-prefix "/aws/lambda/ConnectAsync-Submit" \
  --region "$REGION" \
  --query 'logGroups[0].logGroupName' --output text 2>/dev/null)

if [ -n "$SUBMIT_LOG" ] && [ "$SUBMIT_LOG" != "None" ]; then
  LATEST=$(aws logs describe-log-streams \
    --log-group-name "$SUBMIT_LOG" \
    --order-by LastEventTime --descending --limit 1 \
    --region "$REGION" \
    --query 'logStreams[0].logStreamName' --output text 2>/dev/null)
  echo "Latest stream: $LATEST"
  if [ -n "$LATEST" ] && [ "$LATEST" != "None" ]; then
    aws logs get-log-events \
      --log-group-name "$SUBMIT_LOG" \
      --log-stream-name "$LATEST" \
      --limit 10 \
      --region "$REGION" \
      --query 'events[].message' --output text 2>/dev/null | tail -20
  fi
else
  echo "No submit lambda log group found"
fi

echo ""
echo "--- Polling Lambda recent events ---"
POLL_LOG=$(aws logs describe-log-groups \
  --log-group-name-prefix "/aws/lambda/ConnectAsync-Poll" \
  --region "$REGION" \
  --query 'logGroups[0].logGroupName' --output text 2>/dev/null)

if [ -n "$POLL_LOG" ] && [ "$POLL_LOG" != "None" ]; then
  LATEST=$(aws logs describe-log-streams \
    --log-group-name "$POLL_LOG" \
    --order-by LastEventTime --descending --limit 1 \
    --region "$REGION" \
    --query 'logStreams[0].logStreamName' --output text 2>/dev/null)
  echo "Latest stream: $LATEST"
  if [ -n "$LATEST" ] && [ "$LATEST" != "None" ]; then
    aws logs get-log-events \
      --log-group-name "$POLL_LOG" \
      --log-stream-name "$LATEST" \
      --limit 10 \
      --region "$REGION" \
      --query 'events[].message' --output text 2>/dev/null | tail -20
  fi
else
  echo "No polling lambda log group found"
fi

echo ""
echo "=== 5. Gateway Invocation Logs (CloudTrail) ==="
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=bedrock-agentcore.amazonaws.com \
  --max-results 5 \
  --region "$REGION" \
  --query 'Events[].{Time:EventTime,Name:EventName}' \
  --output table 2>/dev/null || echo "No AgentCore events"
