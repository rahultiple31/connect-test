#!/usr/bin/env python3
"""Create a Lex V2 bot with AMAZON.QinConnectIntent for Option E.

This bot is created via API because CloudFormation's AWS::Lex::Bot
does not support AMAZON.QinConnectIntent as a parentIntentSignature.

Usage:
    python scripts/create_qinconnect_bot.py

Prerequisites:
    - Wisdom assistant must already exist (deployed via CDK)
    - Lex bot role must already exist (deployed via CDK)
"""

import argparse
import json
import os
import sys
import time

import boto3

SSM_PREFIX = "/connect-async-multi-response"
BOT_NAME = "OptionEQinConnectBot"
ALIAS_NAME = "live"
LOCALE_ID = "en_US"


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

lex = boto3.client("lexv2-models", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
connect = boto3.client("connect", region_name=REGION)
CONNECT_INSTANCE_ID = None  # resolved in main()


def get_ssm(name: str) -> str:
    return ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}")["Parameter"]["Value"]


def find_existing_bot() -> dict | None:
    """Check if the bot already exists."""
    resp = lex.list_bots()
    for bot in resp.get("botSummaries", []):
        if bot["botName"] == BOT_NAME:
            return bot
    return None


def wait_for_bot(bot_id: str, target_status: str = "Available", timeout: int = 120):
    """Wait for bot to reach target status."""
    start = time.time()
    while time.time() - start < timeout:
        resp = lex.describe_bot(botId=bot_id)
        status = resp["botStatus"]
        print(f"  Bot status: {status}")
        if status == target_status:
            return resp
        if status in ("Failed", "Deleting"):
            raise RuntimeError(f"Bot reached unexpected status: {status}")
        time.sleep(5)
    raise TimeoutError(f"Bot did not reach {target_status} within {timeout}s")


def wait_for_locale(bot_id: str, timeout: int = 180):
    """Wait for bot locale to be built."""
    start = time.time()
    while time.time() - start < timeout:
        resp = lex.describe_bot_locale(
            botId=bot_id, botVersion="DRAFT", localeId=LOCALE_ID
        )
        status = resp["botLocaleStatus"]
        print(f"  Locale status: {status}")
        if status == "Built":
            return resp
        if status in ("Failed", "NotBuilt"):
            if status == "NotBuilt":
                # Trigger build
                print("  Triggering build...")
                lex.build_bot_locale(
                    botId=bot_id, botVersion="DRAFT", localeId=LOCALE_ID
                )
            elif status == "Failed":
                reasons = resp.get("failureReasons", [])
                raise RuntimeError(f"Locale build failed: {reasons}")
        time.sleep(5)
    raise TimeoutError(f"Locale did not build within {timeout}s")


def main():
    global lex, ssm, connect, CONNECT_INSTANCE_ID, REGION
    args = parse_args()
    REGION = args.region
    lex = boto3.client("lexv2-models", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)
    connect = boto3.client("connect", region_name=REGION)
    CONNECT_INSTANCE_ID = args.instance_id or get_connect_instance_id(connect)

    print(f"Region:      {REGION}")
    print(f"Instance ID: {CONNECT_INSTANCE_ID}")

    # Get required ARNs from SSM (deployed by CDK)
    print("Reading SSM parameters...")
    wisdom_arn = get_ssm("wisdom-assistant-arn")
    print(f"  Wisdom Assistant ARN: {wisdom_arn}")

    # Get Lex bot role from existing bot
    existing_bots = lex.list_bots()["botSummaries"]
    lex_role_arn = None
    for b in existing_bots:
        if b["botName"] == "AsyncResponseInterruptBot":
            desc = lex.describe_bot(botId=b["botId"])
            lex_role_arn = desc["roleArn"]
            break
    if not lex_role_arn:
        print("ERROR: Could not find AsyncResponseInterruptBot to get role ARN")
        sys.exit(1)
    print(f"  Lex Bot Role ARN: {lex_role_arn}")

    # Check if bot already exists
    existing = find_existing_bot()
    if existing:
        bot_id = existing["botId"]
        print(f"Bot already exists: {bot_id}")
    else:
        # Step 1: Create bot
        print("\n1. Creating bot...")
        resp = lex.create_bot(
            botName=BOT_NAME,
            description=(
                "Lex bot for Option E — has AMAZON.QinConnectIntent to hand "
                "conversation to the Q in Connect Orchestrator AI Agent."
            ),
            roleArn=lex_role_arn,
            dataPrivacy={"childDirected": False},
            idleSessionTTLInSeconds=300,
        )
        bot_id = resp["botId"]
        print(f"  Bot ID: {bot_id}")
        wait_for_bot(bot_id)

    # Step 2: Create locale (if not exists)
    print("\n2. Creating locale...")
    try:
        lex.describe_bot_locale(
            botId=bot_id, botVersion="DRAFT", localeId=LOCALE_ID
        )
        print("  Locale already exists")
    except lex.exceptions.ResourceNotFoundException:
        lex.create_bot_locale(
            botId=bot_id,
            botVersion="DRAFT",
            localeId=LOCALE_ID,
            nluIntentConfidenceThreshold=0.40,
            voiceSettings={"voiceId": "Matthew"},
        )
        print("  Locale created")
        time.sleep(3)

    # Step 3: Create FallbackIntent (if not exists)
    print("\n3. Creating FallbackIntent...")
    intents = lex.list_intents(
        botId=bot_id, botVersion="DRAFT", localeId=LOCALE_ID
    ).get("intentSummaries", [])
    has_fallback = any(i["intentName"] == "FallbackIntent" for i in intents)
    if has_fallback:
        print("  FallbackIntent already exists")
    else:
        lex.create_intent(
            botId=bot_id,
            botVersion="DRAFT",
            localeId=LOCALE_ID,
            intentName="FallbackIntent",
            parentIntentSignature="AMAZON.FallbackIntent",
        )
        print("  FallbackIntent created")

    # Step 4: Create QinConnectIntent
    print("\n4. Creating QinConnectIntent...")
    has_qinconnect = any(
        i["intentName"] == "QinConnectIntent"
        or i.get("parentIntentSignature") == "AMAZON.QInConnectIntent"
        for i in intents
    )
    if has_qinconnect:
        print("  QinConnectIntent already exists")
    else:
        lex.create_intent(
            botId=bot_id,
            botVersion="DRAFT",
            localeId=LOCALE_ID,
            intentName="QinConnectIntent",
            parentIntentSignature="AMAZON.QInConnectIntent",
            qInConnectIntentConfiguration={
                "qInConnectAssistantConfiguration": {
                    "assistantArn": wisdom_arn,
                }
            },
        )
        print("  QinConnectIntent created")

    # Step 5: Configure Nova Sonic Speech-to-Speech on the locale
    print("\n5. Configuring Nova Sonic Speech-to-Speech...")
    NOVA_SONIC_MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-2-sonic-v1:0"
    try:
        lex.update_bot_locale(
            botId=bot_id,
            botVersion="DRAFT",
            localeId=LOCALE_ID,
            nluIntentConfidenceThreshold=0.40,
            unifiedSpeechSettings={
                "speechFoundationModel": {
                    "modelArn": NOVA_SONIC_MODEL_ARN,
                    "voiceId": "matthew",
                }
            },
        )
        print(f"  Nova Sonic configured: {NOVA_SONIC_MODEL_ARN}")
    except Exception as e:
        print(f"  Warning: Nova Sonic config failed: {e}")
        print("  Trying without voiceId...")
        try:
            lex.update_bot_locale(
                botId=bot_id,
                botVersion="DRAFT",
                localeId=LOCALE_ID,
                nluIntentConfidenceThreshold=0.40,
                unifiedSpeechSettings={
                    "speechFoundationModel": {
                        "modelArn": NOVA_SONIC_MODEL_ARN,
                    }
                },
            )
            print(f"  Nova Sonic configured (no voiceId): {NOVA_SONIC_MODEL_ARN}")
        except Exception as e2:
            print(f"  ERROR: Nova Sonic config failed: {e2}")
            print("  Continuing without Nova Sonic — bot may not work for voice.")

    # Step 6: Build the locale
    print("\n6. Building bot locale...")
    lex.build_bot_locale(
        botId=bot_id, botVersion="DRAFT", localeId=LOCALE_ID
    )
    wait_for_locale(bot_id)

    # Step 7: Create bot version
    print("\n7. Creating bot version...")
    resp = lex.create_bot_version(
        botId=bot_id,
        botVersionLocaleSpecification={
            LOCALE_ID: {"sourceBotVersion": "DRAFT"}
        },
    )
    bot_version = resp["botVersion"]
    print(f"  Bot version: {bot_version}")

    # Wait for version to be available
    time.sleep(5)
    for _ in range(30):
        resp = lex.describe_bot_version(botId=bot_id, botVersion=bot_version)
        status = resp["botStatus"]
        print(f"  Version status: {status}")
        if status == "Available":
            break
        time.sleep(5)

    # Step 8: Create or update alias
    print("\n8. Creating bot alias...")
    try:
        aliases = lex.list_bot_aliases(botId=bot_id).get("botAliasSummaries", [])
        existing_alias = next((a for a in aliases if a["botAliasName"] == ALIAS_NAME), None)
        if existing_alias:
            lex.update_bot_alias(
                botId=bot_id,
                botAliasId=existing_alias["botAliasId"],
                botAliasName=ALIAS_NAME,
                botVersion=bot_version,
            )
            alias_id = existing_alias["botAliasId"]
            print(f"  Updated alias: {alias_id}")
        else:
            resp = lex.create_bot_alias(
                botId=bot_id,
                botAliasName=ALIAS_NAME,
                botVersion=bot_version,
            )
            alias_id = resp["botAliasId"]
            print(f"  Created alias: {alias_id}")
    except Exception as e:
        print(f"  Error with alias: {e}")
        sys.exit(1)

    alias_arn = f"arn:aws:lex:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:bot-alias/{bot_id}/{alias_id}"
    print(f"  Alias ARN: {alias_arn}")

    # Step 9: Store alias ARN in SSM
    print("\n9. Storing alias ARN in SSM...")
    ssm.put_parameter(
        Name=f"{SSM_PREFIX}/qinconnect-bot-alias",
        Value=alias_arn,
        Type="String",
        Description="Config: QINCONNECT_BOT_ALIAS — Option E QinConnect bot alias ARN",
        Overwrite=True,
    )
    print("  SSM parameter stored")

    # Step 10: Associate with Connect instance
    print("\n10. Associating with Connect instance...")
    try:
        connect.associate_lex_bot(
            InstanceId=CONNECT_INSTANCE_ID,
            LexBot={"LexRegion": REGION, "Name": BOT_NAME},
        )
        print("  Associated (V1 API)")
    except Exception:
        # Try V2 association via integration
        try:
            connect.associate_bot(
                InstanceId=CONNECT_INSTANCE_ID,
                LexV2Bot={"AliasArn": alias_arn},
            )
            print("  Associated (V2 API)")
        except connect.exceptions.ResourceConflictException:
            print("  Already associated")
        except Exception as e2:
            print(f"  Association error: {e2}")

    print(f"\n✅ Done! Bot ID: {bot_id}, Alias: {alias_id}")
    print(f"   Alias ARN: {alias_arn}")
    print(f"\n   Update your Option E flow's Get Customer Input block to use this bot.")


if __name__ == "__main__":
    main()
