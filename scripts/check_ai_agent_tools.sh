#!/bin/bash
# Check AI Agent configuration and tools
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

ASSISTANT_ID=$(resolve_assistant_id "${1:-}")
echo "Assistant ID: $ASSISTANT_ID"
echo ""

echo "=== List AI Agents ==="
aws wisdom list-ai-agents \
  --assistant-id "$ASSISTANT_ID" \
  --region "$REGION" \
  --output json 2>&1

echo ""
echo "=== List AI Agent Versions ==="
AGENT_ID=$(aws wisdom list-ai-agents \
  --assistant-id "$ASSISTANT_ID" \
  --region "$REGION" \
  --query 'aiAgentSummaries[0].aiAgentId' \
  --output text 2>/dev/null)

echo "Agent ID: $AGENT_ID"

if [ -n "$AGENT_ID" ] && [ "$AGENT_ID" != "None" ]; then
  echo ""
  echo "=== Get AI Agent Detail ==="
  aws wisdom get-ai-agent \
    --assistant-id "$ASSISTANT_ID" \
    --ai-agent-id "$AGENT_ID" \
    --region "$REGION" \
    --output json
fi

echo ""
echo "=== Check via CloudFormation ==="
aws cloudformation describe-stack-resource \
  --stack-name ConnectAsyncMultiResponseStack \
  --logical-resource-id OrchestrationAgent \
  --region "$REGION" \
  --query 'StackResourceDetail.{PhysicalId:PhysicalResourceId,Status:ResourceStatus}' \
  --output json 2>/dev/null

echo ""
echo "=== To add MCP tools, run: ==="
echo "python scripts/configure_ai_agent.py"
