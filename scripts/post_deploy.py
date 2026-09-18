#!/usr/bin/env python3
"""Post-deploy wiring that CloudFormation cannot do (Option E / orchestrator).

Run ONCE after `cdk deploy` with deploy_orchestrator=true. Idempotent — safe
to re-run; every step checks before it creates.

    python3 scripts/post_deploy.py            # steps 1-3
    python3 scripts/post_deploy.py --tools    # step 4 (after console MCP registration)

Steps
  1. APPLICATION integration association  — MCP-server app  → Connect instance
  2. WISDOM_ASSISTANT integration assoc.   — Q in Connect assistant → instance
  3. ORCHESTRATION AI Agent + pinned version (no tools yet)
  4. --tools: add MCP tools + the Complete/Escalate RETURN_TO_CONTROL tools to
     the agent, publish a new version.
  4b. --tools: set the agent as the assistant's default self-service
     orchestrator (use case "Connect.SelfService"). Without this, calls route
     to the SYSTEM orchestrator and your prompt/tools are never used.
  5. --tools: grant the tools on a security profile (--security-profile,
     default Admin; no profile inherits them). This is also the catalog check:
     if Connect rejects the grant with "not found", register the MCP server in
     the console (AWS Console → Connect → instance → Third-party applications).
     On a fresh account with the gateway audience set correctly at deploy time
     this has NOT been necessary — the CDK app + step 1 association suffice.

Why not CDK: CreateIntegrationAssociation internally calls iam:PutRolePolicy
on the Connect service-linked role, which Lambda-backed custom resources
cannot do under a permissions boundary; and the AI Agent needs the
WISDOM_ASSISTANT association to exist before it can be created. So this
runs with the deployer's credentials.

All IDs are resolved from SSM (/connect-async-multi-response/*) and the
account — nothing is hardcoded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

SSM_PREFIX = "/connect-async-multi-response"
APP_NAME = "ConnectAsyncMcpServer"
PROMPT_NAME = "NexthinkOrchestrationPrompt"
AGENT_NAME = "NexthinkOrchestrationAgent"

# Undocumented, server-validated. Read back from get-assistant after setting
# the default once in the Connect admin UI. 18 other spellings were rejected.
ORCHESTRATOR_USE_CASE = "Connect.SelfService"

# Return-to-Control tools: how the orchestrator hands the call back to the flow.
# Without these the agent can never end or escalate — the flow's
# Check-contact-attribute block routes on the selected tool name (Complete /
# Escalate). Copied verbatim from the SYSTEM SelfServiceOrchestratorVoice agent
# (list-ai-agents --origin SYSTEM); the admin guide's "Copy from existing"
# path would have pre-populated them, the API create path does not.
RETURN_TO_CONTROL_TOOLS = [
    {
        "toolName": "Complete",
        "toolType": "RETURN_TO_CONTROL",
        "description": "Close conversation when customer has no more questions",
        "inputSchema": {"type": "object", "required": ["reason"],
                        "properties": {"reason": {"type": "string", "description": "Reason of completion"}}},
        "instruction": {"instruction": (
            "Mark the conversation as complete ONLY after confirming the customer has no "
            "additional questions or needs. Always ask if there's anything else you can help "
            "with before using this tool."
        )},
        "userInteractionConfiguration": {"isUserConfirmationRequired": False},
    },
    {
        "toolName": "Escalate",
        "toolType": "RETURN_TO_CONTROL",
        "description": "Escalate to human agent when customer issue can not solve",
        "inputSchema": {"type": "object", "required": ["reason"],
                        "properties": {"reason": {"type": "string", "description": "Reason of escalation"}}},
        "instruction": {
            "instruction": (
                "Escalate the conversation to a human agent when you cannot provide adequate "
                "assistance, when other tools fail or return errors, or when the customer's "
                "request requires human intervention."
            ),
            "examples": [
                "Good example - Tool errors:\n<message>\nI'm having trouble accessing the information "
                "you need right now. Would you like me to connect you with a human agent who can help "
                "you further?\n</message>",
                "Good example - Frustrated customer:\n<message>\nI understand your frustration with this "
                "issue. Would you like me to connect you with a human agent who can help you further?\n</message>",
                "Bad example (avoid this):\n<message>\nI'm unable to help with this. Let me escalate you "
                "to a human agent.\n</message>",
            ],
        },
        "userInteractionConfiguration": {"isUserConfirmationRequired": False},
    },
]


def die(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    p.add_argument("--instance-id", default=None, help="Connect instance ID (default: first instance in account)")
    p.add_argument("--tools", action="store_true", help="Only run steps 4-5: add MCP tools to the agent + grant on a security profile")
    p.add_argument("--security-profile", default="Admin", metavar="NAME",
                   help="Security profile to grant the tools on (default: Admin). Use a dedicated "
                        "profile for least privilege; create it in the Connect admin website first.")
    args = p.parse_args()

    r = args.region
    ssm = boto3.client("ssm", region_name=r)
    connect = boto3.client("connect", region_name=r)
    qconnect = boto3.client("qconnect", region_name=r)
    appint = boto3.client("appintegrations", region_name=r)
    agentcore = boto3.client("bedrock-agentcore-control", region_name=r)
    s3 = boto3.client("s3", region_name=r)

    # ---- resolve ------------------------------------------------------------
    print("=== Resolving deployed resources ===")
    try:
        assistant_arn = ssm.get_parameter(Name=f"{SSM_PREFIX}/wisdom-assistant-arn")["Parameter"]["Value"]
        gateway_arn = ssm.get_parameter(Name=f"{SSM_PREFIX}/agentcore-gateway-arn")["Parameter"]["Value"]
    except ClientError as e:
        die(f"SSM params missing — was the stack deployed with deploy_orchestrator=true? ({e})")
    assistant_id = assistant_arn.rsplit("/", 1)[-1]
    gateway_id = gateway_arn.rsplit("/", 1)[-1]

    if args.instance_id:
        inst = connect.describe_instance(InstanceId=args.instance_id)["Instance"]
    else:
        insts = connect.list_instances(MaxResults=10)["InstanceSummaryList"]
        if not insts:
            die("No Connect instance in this account/region")
        if len(insts) > 1:
            die("Multiple Connect instances — pass --instance-id:\n  " +
                "\n  ".join(f"{i['InstanceAlias']}  {i['Id']}" for i in insts))
        inst = insts[0]
    instance_id, instance_arn = inst["Id"], inst["Arn"]

    apps = [a for a in appint.list_applications(MaxResults=50)["Applications"] if a["Name"] == APP_NAME]
    if not apps:
        die(f"AppIntegrations app {APP_NAME!r} not found — the McpServerRegistration custom resource should have created it")
    app_arn = apps[0]["Arn"]

    print(f"  Instance : {inst.get('InstanceAlias', '?')}  {instance_id}")
    print(f"  Assistant: {assistant_id}")
    print(f"  Gateway  : {gateway_id}")
    print(f"  MCP app  : {app_arn.rsplit('/', 1)[-1]}")

    existing = connect.list_integration_associations(InstanceId=instance_id)["IntegrationAssociationSummaryList"]
    have = {(a["IntegrationType"], a["IntegrationArn"]) for a in existing}

    def ensure_association(kind: str, arn: str) -> None:
        if (kind, arn) in have:
            print(f"  ✓ {kind} association already exists")
            return
        connect.create_integration_association(InstanceId=instance_id, IntegrationType=kind, IntegrationArn=arn)
        print(f"  + {kind} association created")

    if not args.tools:
        # ---- 1 + 2: associations -------------------------------------------
        print("\n=== Step 1-2: Connect integration associations ===")
        ensure_association("APPLICATION", app_arn)
        ensure_association("WISDOM_ASSISTANT", assistant_arn)

        # ---- 3: AI agent ---------------------------------------------------
        print("\n=== Step 3: ORCHESTRATION AI Agent ===")
        prompts = [x for x in qconnect.list_ai_prompts(assistantId=assistant_id)["aiPromptSummaries"]
                   if x["name"] == PROMPT_NAME]
        if not prompts:
            die(f"AI prompt {PROMPT_NAME!r} not found on assistant {assistant_id}")
        prompt_id = prompts[0]["aiPromptId"]

        agents = [a for a in qconnect.list_ai_agents(assistantId=assistant_id)["aiAgentSummaries"]
                  if a.get("type") == "ORCHESTRATION"]
        if agents:
            agent_id = agents[0]["aiAgentId"]
            print(f"  ✓ agent already exists: {agents[0]['name']}  {agent_id}")
        else:
            resp = qconnect.create_ai_agent(
                assistantId=assistant_id,
                name=AGENT_NAME,
                type="ORCHESTRATION",
                visibilityStatus="PUBLISHED",  # required
                description="Orchestration AI Agent for the Nexthink async multi-response pattern",
                configuration={"orchestrationAIAgentConfiguration": {
                    "orchestrationAIPromptId": prompt_id,
                    "connectInstanceArn": instance_arn,
                    "locale": "en_US",
                    "toolConfigurations": [],
                }},
            )
            agent_id = resp["aiAgent"]["aiAgentId"]
            print(f"  + agent created: {AGENT_NAME}  {agent_id}")

        versions = qconnect.list_ai_agent_versions(assistantId=assistant_id, aiAgentId=agent_id)["aiAgentVersionSummaries"]
        if versions:
            print(f"  ✓ {len(versions)} version(s) already pinned")
        else:
            v = qconnect.create_ai_agent_version(assistantId=assistant_id, aiAgentId=agent_id)
            print(f"  + version {v['versionNumber']} pinned")

        print("\n=== NEXT: manual console step ===")
        print("  AWS Console → Amazon Connect → instance → Third-party applications → Add application")
        print(f"    Display name : {APP_NAME}")
        print("    Type         : MCP server")
        print(f"    Gateway      : ConnectAsyncMcpGateway ({gateway_id})")
        print("    Instance     : select this instance")
        print("  Then: python3 scripts/post_deploy.py --tools")
        return

    # ---- 4: tools ----------------------------------------------------------
    print("\n=== Step 4: MCP tools on the AI Agent ===")
    agents = [a for a in qconnect.list_ai_agents(assistantId=assistant_id)["aiAgentSummaries"]
              if a.get("type") == "ORCHESTRATION"]
    if not agents:
        die("No ORCHESTRATION agent — run without --tools first")
    agent_id = agents[0]["aiAgentId"]
    cfg = qconnect.get_ai_agent(assistantId=assistant_id, aiAgentId=agent_id)["aiAgent"]["configuration"]["orchestrationAIAgentConfiguration"]

    tools = []
    for t in agentcore.list_gateway_targets(gatewayIdentifier=gateway_id)["items"]:
        detail = agentcore.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
        uri = detail["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["s3"]["uri"]
        bucket, key = uri.split("/", 3)[2], uri.split("/", 3)[3]
        schema = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        for tool in schema:
            # toolName: underscores only (hyphens rejected). toolId keeps hyphens.
            tools.append({
                "toolName": f"{t['name']}___{tool['name']}".replace("-", "_"),
                "toolType": "MODEL_CONTEXT_PROTOCOL",
                "toolId": f"gateway_{gateway_id}__{t['name']}___{tool['name']}",
            })
            print(f"  tool: {tools[-1]['toolName']}")
    if not tools:
        die("No tools discovered on gateway targets")
    mcp_tools = tools
    tools = mcp_tools + RETURN_TO_CONTROL_TOOLS
    for t in RETURN_TO_CONTROL_TOOLS:
        print(f"  tool: {t['toolName']} (RETURN_TO_CONTROL)")

    def _key(t):  # MCP tools compare by toolId; RTC tools have none, compare by name
        return t.get("toolId") or f"RTC:{t['toolName']}"

    if {_key(x) for x in cfg.get("toolConfigurations", [])} == {_key(x) for x in tools}:
        print("  ✓ agent already has these tools")
    else:
        qconnect.update_ai_agent(
            assistantId=assistant_id, aiAgentId=agent_id, visibilityStatus="PUBLISHED",
            configuration={"orchestrationAIAgentConfiguration": {
                "orchestrationAIPromptId": cfg["orchestrationAIPromptId"],
                "connectInstanceArn": cfg.get("connectInstanceArn", instance_arn),
                "locale": cfg.get("locale", "en_US"),
                "toolConfigurations": tools,
            }},
        )
        v = qconnect.create_ai_agent_version(assistantId=assistant_id, aiAgentId=agent_id)
        print(f"  + agent updated with {len(tools)} tools, version {v['versionNumber']} published")

    # ---- 4b: make it the assistant's default self-service orchestrator --------
    # The Lex QInConnectIntent binds to the ASSISTANT, not to an agent. Which
    # orchestrator answers is decided by the assistant's default mapping; if
    # unset, calls route to the SYSTEM SelfServiceOrchestratorVoice — no custom
    # prompt, no MCP tools. The use-case string is not documented anywhere and
    # is validated server-side; "Connect.SelfService" was read back after
    # setting it once in the admin UI (AI Agents list → Default AI Agent
    # Configurations → Self Service row). The UI binds $LATEST.
    print("\n=== Step 4b: set as assistant default orchestrator ===")
    want = f"{agent_id}:$LATEST"
    current = qconnect.get_assistant(assistantId=assistant_id)["assistant"].get("orchestratorConfigurationList", [])
    if any(o.get("orchestratorUseCase") == ORCHESTRATOR_USE_CASE and o.get("aiAgentId") == want for o in current):
        print(f"  ✓ already default for {ORCHESTRATOR_USE_CASE}")
    else:
        qconnect.update_assistant_ai_agent(
            assistantId=assistant_id, aiAgentType="ORCHESTRATION",
            orchestratorUseCase=ORCHESTRATOR_USE_CASE, configuration={"aiAgentId": want},
        )
        print(f"  + set as default for {ORCHESTRATOR_USE_CASE} → {want}")

    # ---- 5: security-profile tool grant ---------------------------------------
    # No profile inherits MCP tool access; it must be granted per tool. For
    # Type=MCP the permissions are the tool identifiers in "<target>___<tool>"
    # form (hyphens kept — this is the gateway's naming, not the agent's
    # underscored toolName). If this call fails with "not found", the MCP
    # server is not in Connect's catalog — see DEPLOYMENT.md Step 6b.
    #
    # Only the profile's Tools section matters for gateway tools; the
    # Permissions grid (AI agent designer, Routing, …) can stay empty for a
    # profile the agent wears in voice self-service — that grid is for humans.
    print(f"\n=== Step 5: grant MCP tools on security profile {args.security_profile!r} ===")
    profiles = connect.list_security_profiles(InstanceId=instance_id)["SecurityProfileSummaryList"]
    profile = next((p for p in profiles if p["Name"] == args.security_profile), None)
    if not profile:
        die(f"No security profile named {args.security_profile!r} on this instance. "
            f"Available: {', '.join(p['Name'] for p in profiles)}")
    # Same discovery as step 4, but the permission string keeps the gateway's
    # hyphenated "<target>___<tool>" form (the agent's toolName is underscored).
    # RTC tools are agent-internal, not gateway tools — only MCP tools need profile grants.
    tool_perms = sorted({x["toolId"].split("__", 1)[1] for x in mcp_tools} | _gateway_builtin_tools(agentcore, gateway_id))
    granted = connect.list_security_profile_applications(InstanceId=instance_id, SecurityProfileId=profile["Id"])["Applications"]
    have_perms = sorted(p for a in granted if a.get("Namespace") == gateway_id for p in a.get("ApplicationPermissions", []))
    if have_perms == tool_perms:
        print(f"  ✓ {args.security_profile} already has {len(tool_perms)} tool grants")
    else:
        connect.update_security_profile(
            InstanceId=instance_id, SecurityProfileId=profile["Id"],
            Applications=[{"Namespace": gateway_id, "Type": "MCP", "ApplicationPermissions": tool_perms}],
        )
        print(f"  + {args.security_profile} granted: {tool_perms}")

    print("\n=== NEXT: Connect admin website (the only remaining console-only steps) ===")
    print(f"  1. AI agent designer → AI agents → {AGENT_NAME} → Edit in Agent Builder →")
    print(f"     Security Profiles dropdown → {args.security_profile} → Save → Publish")
    print("     (agent↔profile link has no API; tools flip from 'Insufficient' to OK once attached)")
    print("  2. Routing → Flows → Conversational AI → Create bot → Add language en-US →")
    print("     Configuration → enable 'Amazon Connect AI agent' → pick assistant → Build")
    print("  3. AWS Console → Connect → instance → Flows → toggle 'Enable Lex Bot Management' off/on → Save")
    print("     (writes wisdom:* + kms inline policies onto the Lex SLR; API toggle does NOT do this;")
    print("      only works AFTER step 2 of this script created the WISDOM_ASSISTANT association)")
    print("  4. Import config/Main Flow -- Option E.json, re-select the bot, publish, assign a number")


def _gateway_builtin_tools(agentcore, gateway_id: str) -> set[str]:
    """Built-in tools the gateway exposes that are not in any target schema.

    With ``protocolConfiguration.mcp.searchType == "SEMANTIC"`` (the CDK
    construct's default) AgentCore provisions ``x_amz_bedrock_agentcore_search``
    — natural-language tool discovery. Our agent pins its two tools explicitly
    and never needs to search, but the built-in shows up in Connect's tool
    catalog for this MCP server, so grant it too: read-only, zero risk, and it
    keeps the profile's Tools list matching what the console shows.
    """
    gw = agentcore.get_gateway(gatewayIdentifier=gateway_id)
    search = ((gw.get("protocolConfiguration") or {}).get("mcp") or {}).get("searchType")
    return {"x_amz_bedrock_agentcore_search"} if search == "SEMANTIC" else set()


if __name__ == "__main__":
    main()
