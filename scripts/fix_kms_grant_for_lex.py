#!/usr/bin/env python3
"""Add KMS grant for the Lex SLR to access the Wisdom assistant's KMS key.

The Wisdom assistant uses a customer-managed KMS key. The Lex SLR needs
a grant on this key to decrypt Wisdom data when QInConnectIntent is invoked.

Values are resolved automatically from SSM and STS. Override with env vars
or CLI args if needed:

    python scripts/fix_kms_grant_for_lex.py
    python scripts/fix_kms_grant_for_lex.py --kms-key-id abc123 --region us-west-2
"""

import argparse
import os
import sys

import boto3


SSM_PREFIX = "/connect-async-multi-response"


def get_ssm(name: str, region: str) -> str:
    """Read a value from SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}")["Parameter"]["Value"]


def get_account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def resolve_kms_key_id(region: str) -> str:
    """Get the KMS key ID from the Wisdom assistant's encryption config."""
    assistant_arn = get_ssm("wisdom-assistant-arn", region)
    assistant_id = assistant_arn.split("/")[-1]
    qconnect = boto3.client("qconnect", region_name=region)
    assistant = qconnect.get_assistant(assistantId=assistant_id)
    kms_key_id = assistant["assistant"].get("serverSideEncryptionConfiguration", {}).get("kmsKeyId", "")
    if not kms_key_id:
        sys.exit("ERROR: Wisdom assistant has no customer-managed KMS key")
    return kms_key_id


def resolve_lex_slr_arn(account_id: str) -> str:
    """Build the Lex SLR ARN from the account ID."""
    return (
        f"arn:aws:iam::{account_id}:role/aws-service-role/"
        f"lexv2.amazonaws.com/AWSServiceRoleForLexV2Bots_AmazonConnect_{account_id}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--kms-key-id", default=None, help="Override KMS key ID (auto-resolved from Wisdom assistant)")
    args = parser.parse_args()

    region = args.region
    account_id = get_account_id()
    kms_key_id = args.kms_key_id or resolve_kms_key_id(region)
    lex_slr_arn = resolve_lex_slr_arn(account_id)

    print(f"Account:  {account_id}")
    print(f"Region:   {region}")
    print(f"KMS Key:  {kms_key_id}")
    print(f"Lex SLR:  {lex_slr_arn}")

    kms = boto3.client("kms", region_name=region)

    print(f"\nAdding KMS grant for Lex SLR on key {kms_key_id}...")
    try:
        resp = kms.create_grant(
            KeyId=kms_key_id,
            GranteePrincipal=lex_slr_arn,
            Operations=["Decrypt", "GenerateDataKey", "DescribeKey", "RetireGrant"],
            Name="LexQInConnectWisdomAccess",
        )
        print(f"Grant created: {resp['GrantId']}")
    except kms.exceptions.AlreadyExistsException:
        print("Grant already exists")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Verify
    print("\nVerifying grants...")
    grants = kms.list_grants(KeyId=kms_key_id)["Grants"]
    for g in grants:
        if "LexV2Bots" in g.get("GranteePrincipal", ""):
            print(f"  Lex SLR grant found: {g['GrantId']}")
            print(f"  Operations: {g['Operations']}")


if __name__ == "__main__":
    main()
