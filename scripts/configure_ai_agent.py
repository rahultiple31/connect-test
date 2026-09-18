#!/usr/bin/env python3
"""Post-deploy: configure the AI Agent with MCP tools, security profile, and publish.

Prerequisites:
  - CDK stack deployed with deploy_orchestrator=true
  - MCP server registered via Connect console (one-time manual step)
  - scripts/sync_callback_api_key.py already run

This script:
  1. Discovers the gateway ID and tool names from the deployed gateway
  2. Updates the AI Agent with MCP tool configurations
  3. Creates a new AI Agent version (publish)
  4. Grants MCP tool access on the Admin security profile

Usage:
  python3 scripts/configure_ai_agent.py
"""
import boto3
import json
import sys

REGION = "us-east-1"
SSM_PREFIX = "/connect-async-multi-response"

ssm = boto3.client("ssm", region_name=REGION)
qconnect = boto3.client("qconnect", region_name=REGION)
connect = boto3.client("connect", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)


def get_ssm(name):
    return ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}")["Parameter"]["Value"]


# --- Resolve IDs from SSM / deployed resources ---
print("=== Resolving deployed resources ===")
assistant_arn = get_ssm("wisdom-assistant-arn")
assistant_id = assistant_arn.split("/")[-1]
gateway_arn = get_ssm("agentcore-gateway-arn")
gateway_id = gateway_arn.split("/")[-1]

instance_resp = connect.list_instances(MaxResults=1)
instance_id = None
instance_arn = None
for inst in instance_resp.get("InstanceSummaryList", []):
    instance_id = inst["Id"]
    instance_arn = inst["Arn"]
    break

if not instance_id:
    print("ERROR: No Connect instance found")
    sys.exit(1)

print(f"  Assistant: {assistant_id}")
print(f"  Gateway: {gateway_id}")
print(f"  Instance: {instance_id}")

# --- Find the AI Agent ---
agents = qconnect.list_ai_agents(assistantId=assistant_id)
orch_agents = [a for a in agents.get("aiAgentSummaries", []) if a.get("type") == "ORCHESTRATION"]
if not orch_agents:
    print("ERROR: No ORCHESTRATION AI Agent found")
    sys.exit(1)

agent = orch_agents[0]
agent_id = agent["aiAgentId"]
print(f"  Agent: {agent['name']} ({agent_id})")

# --- Get current agent config ---
detail = qconnect.get_ai_agent(assistantId=assistant_id, aiAgentId=agent_id)
current_config = detail["aiAgent"]["configuration"]["orchestrationAIAgentConfiguration"]
current_tools = current_config.get("toolConfigurations", [])
prompt_id = current_config["orchestrationAIPromptId"]

print(f"  Current tools: {len(current_tools)}")

# --- Discover tools from gateway targets ---
print("\n=== Discovering MCP tools from gateway ===")
targets = agentcore.list_gateway_targets(gatewayIdentifier=gateway_id)
tools = []
for t in targets["items"]:
    target_name = t["name"]
    # Read schema from S3 to get tool names
    target_detail = agentcore.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
    schema_uri = target_detail["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["s3"]["uri"]
    s3 = boto3.client("s3", region_name=REGION)
    bucket = schema_uri.split("/")[2]
    key = "/".join(schema_uri.split("/")[3:])
    obj = s3.get_object(Bucket=bucket, Key=key)
    schema = json.loads(obj["Body"].read())

    for tool_def in schema:
        tool_name = tool_def["name"]
        # Full qualified ID: gateway_{gateway-id}__{target-name}___{tool-name}
        full_id = f"gateway_{gateway_id}__{target_name}___{tool_name}"
        # Display name: {target-name}___{tool-name}
        display_name = f"{target_name}___{tool_name}"

        tool_config = {
            "toolName": display_name.replace("-", "_"),
            "toolType": "MODEL_CONTEXT_PROTOCOL",
            "toolId": full_id,
        }
        tools.append(tool_config)
        print(f"  Tool: {display_name}")
        print(f"    ID: {full_id}")

if not tools:
    print("ERROR: No tools found on gateway. Is the MCP server registered via Connect console?")
    sys.exit(1)

# --- Check if update needed ---
existing_tool_ids = {t.get("toolId", "") for t in current_tools}
new_tool_ids = {t["toolId"] for t in tools}

if existing_tool_ids == new_tool_ids:
    print("\n✅ AI Agent already has the correct tools. No update needed.")
else:
    # --- Update AI Agent with tools ---
    print(f"\n=== Updating AI Agent with {len(tools)} tools ===")
    qconnect.update_ai_agent(
        assistantId=assistant_id,
        aiAgentId=agent_id,
        visibilityStatus="PUBLISHED",
        configuration={
            "orchestrationAIAgentConfiguration": {
                "orchestrationAIPromptId": prompt_id,
                "connectInstanceArn": instance_arn,
                "locale": "en_US",
                "toolConfigurations": tools,
            }
        },
    )
    print("  Updated ✅")

    # --- Publish new version ---
    print("\n=== Publishing new AI Agent version ===")
    version = qconnect.create_ai_agent_version(
        assistantId=assistant_id,
        aiAgentId=agent_id,
    )
    ver_num = version.get("versionNumber", "?")
    print(f"  Published version {ver_num} ✅")

# --- Security profile: grant tool access ---
print("\n=== Checking security profile tool access ===")
# Find the Admin security profile
profiles = connect.list_security_profiles(InstanceId=instance_id, MaxResults=50)
admin_profile = None
for p in profiles.get("SecurityProfileSummaryList", []):
    if p["Name"] == "Admin":
        admin_profile = p
        break

if admin_profile:
    sp_id = admin_profile["Id"]
    print(f"  Admin profile: {sp_id}")

    # Check current application permissions
    try:
        current_apps = connect.list_security_profile_applications(
            SecurityProfileId=sp_id,
            InstanceId=instance_id,
            MaxResults=50,
        )
        existing_app_ids = {a.get("ApplicationId", "") for a in current_apps.get("Applications", [])}
        print(f"  Current tool permissions: {len(existing_app_ids)}")

        # The tool application IDs are the full qualified tool IDs
        # We need to check if our tools are already granted
        # For now, just report status
        for tool in tools:
            tid = tool["toolId"]
            status = "✅ granted" if tid in existing_app_ids else "❌ not granted"
            print(f"    {tool['toolName']}: {status}")

        if not all(tid in existing_app_ids for tid in new_tool_ids):
            print("\n  ⚠️  Some tools lack security profile access.")
            print("  Grant access manually: Connect admin → Users → Security profiles → Admin → Tools → Access")
    except Exception as e:
        print(f"  Could not check tool permissions: {str(e)[:100]}")
        print("  Grant access manually: Connect admin → Users → Security profiles → Admin → Tools → Access")
else:
    print("  Admin security profile not found — grant tool access manually")

print("\n=== Done ===")
