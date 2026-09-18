#!/usr/bin/env python3
"""Add missing Wisdom + Bedrock permissions to a Lex bot role.

The QInConnectIntent + Nova Sonic S2S require permissions beyond
what the CDK-created role has (only Polly + Comprehend). This script
adds Wisdom, Bedrock, and lexv2.aws.internal trust to the bot's role.

Bot is resolved by name (default: first QinConnect bot found) or by ID.

    python scripts/fix_lex_role_permissions.py
    python scripts/fix_lex_role_permissions.py --bot-id XXXXXXXXXX
    python scripts/fix_lex_role_permissions.py --bot-name MyCustomBot
"""

import argparse
import json
import os
import sys

import boto3


def get_account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def find_bot_id(region: str, bot_name: str | None = None) -> str:
    """Find a Lex bot ID by name, or return the first bot found."""
    lex = boto3.client("lexv2-models", region_name=region)
    paginator = lex.get_paginator("list_bots")
    for page in paginator.paginate():
        for bot in page.get("botSummaries", []):
            if bot_name and bot["botName"] == bot_name:
                return bot["botId"]
            if not bot_name:
                # Return first bot found (caller should use --bot-name or --bot-id)
                print(f"  Auto-selected bot: {bot['botName']} ({bot['botId']})")
                return bot["botId"]
    sys.exit(f"ERROR: No bot found{f' with name {bot_name!r}' if bot_name else ''}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--bot-id", default=None, help="Lex bot ID (overrides name lookup)")
    parser.add_argument("--bot-name", default=None, help="Lex bot name to search for")
    args = parser.parse_args()

    region = args.region
    account_id = get_account_id()

    if args.bot_id:
        bot_id = args.bot_id
    else:
        bot_id = find_bot_id(region, args.bot_name)

    lex = boto3.client("lexv2-models", region_name=region)
    iam = boto3.client("iam")

    # Get the role name from the bot
    role_arn = lex.describe_bot(botId=bot_id)["roleArn"]
    role_name = role_arn.split("/")[-1]

    print(f"Account:  {account_id}")
    print(f"Region:   {region}")
    print(f"Bot ID:   {bot_id}")
    print(f"Bot role: {role_name}")

    # 1. Add Wisdom permissions (for QInConnectIntent)
    wisdom_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WisdomAccess",
                "Effect": "Allow",
                "Action": [
                    "wisdom:CreateSession",
                    "wisdom:GetAssistant",
                    "wisdom:GetSession",
                    "wisdom:ListAssistants",
                    "wisdom:PutFeedback",
                    "wisdom:SendMessage",
                    "wisdom:GetNextMessage",
                    "wisdom:QueryAssistant",
                    "wisdom:GetRecommendations",
                    "wisdom:NotifyRecommendationsReceived",
                ],
                "Resource": "*",
            }
        ],
    }

    print("\n1. Adding Wisdom permissions...")
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="WisdomAccess",
        PolicyDocument=json.dumps(wisdom_policy),
    )
    print("   Done")

    # 2. Add Bedrock permissions (for Nova Sonic S2S)
    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/amazon.nova-2-sonic-v1:0",
                    f"arn:aws:bedrock:{region}::foundation-model/*",
                ],
            }
        ],
    }

    print("2. Adding Bedrock permissions...")
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="BedrockAccess",
        PolicyDocument=json.dumps(bedrock_policy),
    )
    print("   Done")

    # 3. Update trust policy to add lexv2.aws.internal
    print("3. Updating trust policy...")
    trust = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]

    has_internal = any(
        "lexv2.aws.internal" in str(s.get("Principal", {}))
        for s in trust["Statement"]
    )

    if not has_internal:
        trust["Statement"].append({
            "Effect": "Allow",
            "Sid": "LexV2InternalTrustPolicy",
            "Principal": {"Service": "lexv2.aws.internal"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:lex:*:{account_id}:bot-alias/{bot_id}/*"
                },
            },
        })
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust),
        )
        print("   Added lexv2.aws.internal trust")
    else:
        print("   lexv2.aws.internal already in trust policy")

    # 4. Verify
    print("\n=== Verification ===")
    policies = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    print(f"Inline policies: {policies}")
    trust = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]
    principals = []
    for s in trust["Statement"]:
        p = s.get("Principal", {})
        if isinstance(p, dict):
            for v in p.values():
                principals.append(v if isinstance(v, str) else str(v))
        else:
            principals.append(str(p))
    print(f"Trust principals: {principals}")
    print("\nPermissions updated.")


if __name__ == "__main__":
    main()
