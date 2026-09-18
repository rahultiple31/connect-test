#!/usr/bin/env python3
"""Configure Nova Sonic S2S on the OptionEQinConnectBot and verify it sticks.

Usage:
    python scripts/configure_nova_sonic.py                       # looks up bot by name
    python scripts/configure_nova_sonic.py --bot-id XXXXXXXXXX   # explicit
    python scripts/configure_nova_sonic.py --bot-name MyBot --alias-id YYYYYYYYYY
"""

import argparse
import json
import os
import sys
import time
import boto3

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
parser.add_argument("--bot-id", default=None, help="Lex bot ID (overrides --bot-name lookup)")
parser.add_argument("--bot-name", default="OptionEQinConnectBot", help="Lex bot name to look up")
parser.add_argument("--alias-id", default=None, help="Bot alias ID (default: first non-TestBotAlias alias)")
parser.add_argument("--locale-id", default="en_US")
args = parser.parse_args()

REGION = args.region
LOCALE_ID = args.locale_id
MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-2-sonic-v1:0"

lex = boto3.client("lexv2-models", region_name=REGION)

if args.bot_id:
    BOT_ID = args.bot_id
else:
    bots = [b for p in lex.get_paginator("list_bots").paginate()
            for b in p.get("botSummaries", []) if b["botName"] == args.bot_name]
    if not bots:
        sys.exit(f"ERROR: no Lex bot named {args.bot_name!r} in {REGION}; pass --bot-id")
    BOT_ID = bots[0]["botId"]
    print(f"Resolved bot {args.bot_name!r} → {BOT_ID}")

if args.alias_id:
    ALIAS_ID = args.alias_id
else:
    aliases = [a for a in lex.list_bot_aliases(botId=BOT_ID).get("botAliasSummaries", [])
               if a["botAliasName"] != "TestBotAlias"]
    if not aliases:
        sys.exit(f"ERROR: bot {BOT_ID} has no non-test alias; pass --alias-id")
    ALIAS_ID = aliases[0]["botAliasId"]
    print(f"Resolved alias → {ALIAS_ID}")


def describe_locale(version="DRAFT"):
    resp = lex.describe_bot_locale(
        botId=BOT_ID, botVersion=version, localeId=LOCALE_ID
    )
    return {
        "status": resp["botLocaleStatus"],
        "voice": resp.get("voiceSettings"),
        "unifiedSpeech": resp.get("unifiedSpeechSettings"),
        "nluThreshold": resp.get("nluIntentConfidenceThreshold"),
    }


print("=== Before update ===")
draft = describe_locale()
print(json.dumps(draft, indent=2))

if draft.get("unifiedSpeech"):
    print("\n  Nova Sonic already configured on DRAFT. Skipping update.")
else:
    print("\n=== Updating locale with Nova Sonic... ===")
    resp = lex.update_bot_locale(
        botId=BOT_ID,
        botVersion="DRAFT",
        localeId=LOCALE_ID,
        nluIntentConfidenceThreshold=0.40,
        unifiedSpeechSettings={
            "speechFoundationModel": {
                "modelArn": MODEL_ARN,
                "voiceId": "matthew",
            }
        },
    )
    print(f"  Response status: {resp['botLocaleStatus']}")
    print(f"  Response unifiedSpeech: {resp.get('unifiedSpeechSettings')}")

print("\n=== After update (before build) ===")
time.sleep(2)
print(json.dumps(describe_locale(), indent=2))

print("\n=== Building locale... ===")
lex.build_bot_locale(botId=BOT_ID, botVersion="DRAFT", localeId=LOCALE_ID)
for _ in range(36):
    time.sleep(5)
    info = describe_locale()
    print(f"  Status: {info['status']}, UnifiedSpeech: {info['unifiedSpeech']}")
    if info["status"] in ("Built", "ReadyExpressTesting"):
        break
    if info["status"] == "Failed":
        print("  BUILD FAILED")
        break

print("\n=== After build ===")
print(json.dumps(describe_locale(), indent=2))

print("\n=== Creating new version... ===")
resp = lex.create_bot_version(
    botId=BOT_ID,
    botVersionLocaleSpecification={LOCALE_ID: {"sourceBotVersion": "DRAFT"}},
)
version = resp["botVersion"]
print(f"  Version: {version}")
time.sleep(5)

# Wait for version
for _ in range(20):
    resp = lex.describe_bot_version(botId=BOT_ID, botVersion=version)
    if resp["botStatus"] == "Available":
        break
    time.sleep(3)

print(f"\n=== Version {version} locale ===")
print(json.dumps(describe_locale(version), indent=2))

print("\n=== Updating alias... ===")
lex.update_bot_alias(
    botId=BOT_ID,
    botAliasId=ALIAS_ID,
    botAliasName="live",
    botVersion=version,
)
print(f"  Alias updated to version {version}")
