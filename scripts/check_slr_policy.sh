#!/bin/bash
# Check the SLR managed policy for Wisdom permissions
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

ACCOUNT_ID=$(resolve_account_id)
SLR_NAME="AWSServiceRoleForLexV2Bots_AmazonConnect_${ACCOUNT_ID}"

echo "Account: $ACCOUNT_ID"
echo "SLR:     $SLR_NAME"
echo ""

echo "=== AmazonLexV2BotPolicy contents ==="
aws iam get-policy \
  --policy-arn "arn:aws:iam::aws:policy/aws-service-role/AmazonLexV2BotPolicy" \
  --region "$REGION" \
  --output json

POLICY_VERSION=$(aws iam get-policy \
  --policy-arn "arn:aws:iam::aws:policy/aws-service-role/AmazonLexV2BotPolicy" \
  --query 'Policy.DefaultVersionId' --output text)

echo ""
echo "=== Policy Version $POLICY_VERSION ==="
aws iam get-policy-version \
  --policy-arn "arn:aws:iam::aws:policy/aws-service-role/AmazonLexV2BotPolicy" \
  --version-id "$POLICY_VERSION" \
  --query 'PolicyVersion.Document' \
  --output json

echo ""
echo "=== SLR Trust Policy ==="
aws iam get-role \
  --role-name "$SLR_NAME" \
  --query 'Role.AssumeRolePolicyDocument' \
  --output json

echo ""
echo "=== SLR Inline Policies (if any) ==="
aws iam list-role-policies \
  --role-name "$SLR_NAME" \
  --output json

for policy in $(aws iam list-role-policies \
  --role-name "$SLR_NAME" \
  --query 'PolicyNames[]' --output text 2>/dev/null); do
  echo "--- Inline: $policy ---"
  aws iam get-role-policy \
    --role-name "$SLR_NAME" \
    --policy-name "$policy" \
    --output json
done
