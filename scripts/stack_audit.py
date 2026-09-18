"""Audit deployed stack: list all resources, check key properties, flag gaps.

    python scripts/stack_audit.py
    python scripts/stack_audit.py --region us-west-2 --instance-id abc123
"""
import argparse
import os
import sys

import boto3

STACK = "ConnectAsyncMultiResponseStack"
SSM_PREFIX = "/connect-async-multi-response"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--instance-id", default=None, help="Connect instance ID (auto-discovered if omitted)")
    return parser.parse_args()


def get_connect_instance_id(connect_client) -> str:
    resp = connect_client.list_instances(MaxResults=1)
    instances = resp.get("InstanceSummaryList", [])
    if not instances:
        sys.exit("ERROR: No Connect instance found")
    return instances[0]["Id"]


def main():
    args = parse_args()
    region = args.region

    cfn = boto3.client("cloudformation", region_name=region)
    connect = boto3.client("connect", region_name=region)
    qconnect = boto3.client("qconnect", region_name=region)
    appintegrations = boto3.client("appintegrations", region_name=region)
    agentcore = boto3.client("bedrock-agentcore-control", region_name=region)
    ssm = boto3.client("ssm", region_name=region)
    sm = boto3.client("secretsmanager", region_name=region)

    instance_id = args.instance_id or get_connect_instance_id(connect)
    print(f"Region: {region}, Instance: {instance_id}\n")

    # 1. Stack status + outputs
    stack = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    print(f"=== Stack: {STACK} ===")
    print(f"Status: {stack['StackStatus']}")
    for o in stack.get("Outputs", []):
        print(f"  Output: {o['OutputKey']} = {o['OutputValue']}")

    # 2. Resource summary by type
    resources = []
    paginator = cfn.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=STACK):
        resources.extend(page["StackResourceSummaries"])

    by_type = {}
    for r in resources:
        by_type.setdefault(r["ResourceType"], []).append(r)

    print(f"\n=== Resources: {len(resources)} total ===")
    for rtype in sorted(by_type):
        items = by_type[rtype]
        statuses = set(r["ResourceStatus"] for r in items)
        status_str = ", ".join(statuses)
        names = [r["LogicalResourceId"] for r in items]
        print(f"  {rtype} ({len(items)}) [{status_str}]")
        for n in names:
            print(f"    - {n}")

    # 3. Failed/incomplete resources
    print("\n=== Non-complete resources ===")
    for r in resources:
        if "COMPLETE" not in r["ResourceStatus"] or "FAILED" in r["ResourceStatus"]:
            print(f"  {r['LogicalResourceId']}: {r['ResourceStatus']} — {r.get('ResourceStatusReason', '')}")

    # 4. SSM parameters
    print(f"\n=== SSM Parameters ({SSM_PREFIX}/*) ===")
    params = ssm.get_parameters_by_path(Path=SSM_PREFIX, Recursive=True)
    for p in sorted(params["Parameters"], key=lambda x: x["Name"]):
        val = p["Value"][:80] + "..." if len(p["Value"]) > 80 else p["Value"]
        print(f"  {p['Name']} = {val}")

    # 5. Secrets Manager
    print("\n=== Secrets Manager ===")
    for prefix in ["connect-async/NEXTHINK_API_KEY", "connect-async/CALLBACK_API_KEY"]:
        try:
            s = sm.describe_secret(SecretId=prefix)
            print(f"  {s['Name']}: exists (last changed: {s.get('LastChangedDate', 'never')})")
        except Exception:
            print(f"  {prefix}: NOT FOUND")

    # 6. Connect integration associations
    print("\n=== Connect Associations ===")
    for itype in ["LEX_BOT", "LAMBDA_FUNCTION", "APPLICATION", "WISDOM_ASSISTANT"]:
        try:
            assocs = connect.list_integration_associations(InstanceId=instance_id, IntegrationType=itype)
            for a in assocs["IntegrationAssociationSummaryList"]:
                arn = a["IntegrationArn"]
                short = arn.split(":")[-1] if ":" in arn else arn
                print(f"  {itype}: {short}")
            if not assocs["IntegrationAssociationSummaryList"]:
                print(f"  {itype}: (none)")
        except Exception as e:
            print(f"  {itype}: error — {e}")

    # 7. AgentCore Gateway
    print("\n=== AgentCore Gateway ===")
    gateways = agentcore.list_gateways()
    for gw in gateways["items"]:
        print(f"  {gw['name']} ({gw['gatewayId']}) — {gw['status']}")
        targets = agentcore.list_gateway_targets(gatewayIdentifier=gw["gatewayId"])
        for t in targets["items"]:
            print(f"    Target: {t['name']} — {t['status']}")

    # 8. Wisdom AI Agent — check tool configs
    print("\n=== Wisdom AI Agent ===")
    try:
        assistant_arn = ssm.get_parameter(Name=f"{SSM_PREFIX}/wisdom-assistant-arn")["Parameter"]["Value"]
        assistant_id = assistant_arn.split("/")[-1]
        print(f"  Assistant: {assistant_id}")

        agents = qconnect.list_ai_agents(assistantId=assistant_id)
        for a in agents.get("aiAgentSummaries", []):
            print(f"  Agent: {a['name']} ({a['aiAgentId']}) — {a.get('status', 'N/A')}")
            detail = qconnect.get_ai_agent(assistantId=assistant_id, aiAgentId=a["aiAgentId"])
            config = detail["aiAgent"].get("configuration", {})
            orch = config.get("orchestrationAIAgentConfiguration", {})
            tools = orch.get("toolConfigurations", [])
            print(f"    Prompt ID: {orch.get('orchestrationAIPromptId', 'NONE')}")
            print(f"    Connect ARN: {orch.get('connectInstanceArn', 'NONE')}")
            print(f"    Tools: {len(tools)}")
            for t in tools:
                print(f"      - {t['toolName']} ({t['toolType']})")
            if not tools:
                print("      *** NO MCP TOOLS CONFIGURED ***")

        if agents.get("aiAgentSummaries"):
            versions = qconnect.list_ai_agent_versions(
                assistantId=assistant_id,
                aiAgentId=agents["aiAgentSummaries"][0]["aiAgentId"],
            )
        else:
            versions = {"aiAgentVersionSummaries": []}
        print(f"  Agent versions: {len(versions.get('aiAgentVersionSummaries', []))}")
    except Exception as e:
        print(f"  Error: {e}")

    # 9. AppIntegrations Applications
    print("\n=== AppIntegrations Applications ===")
    apps = appintegrations.list_applications(MaxResults=50)
    for app in apps.get("Applications", []):
        detail = appintegrations.get_application(Arn=app["Arn"])
        print(f"  {detail['Name']} — Type: {detail.get('ApplicationType', 'N/A')}, Namespace: {detail.get('Namespace', 'N/A')}")


if __name__ == "__main__":
    main()
