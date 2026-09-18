#!/bin/bash
# Check if the SLR permissions changed after BOT_MANAGEMENT toggle
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/resolve.sh"

ACCOUNT_ID=$(resolve_account_id)
SLR_NAME="AWSServiceRoleForLexV2Bots_AmazonConnect_${ACCOUNT_ID}"

echo "Account: $ACCOUNT_ID"
echo "SLR:     $SLR_NAME"
echo ""

echo "=== Attached Policies ==="
aws iam list-attached-role-policies --role-name "$SLR_NAME" --output json

echo ""
echo "=== Inline Policies ==="
aws iam list-role-policies --role-name "$SLR_NAME" --output json

for policy in $(aws iam list-role-policies --role-name "$SLR_NAME" --query 'PolicyNames[]' --output text 2>/dev/null); do
  echo ""
  echo "--- Inline: $policy ---"
  aws iam get-role-policy --role-name "$SLR_NAME" --policy-name "$policy" --output json
done

echo ""
echo "=== Trust Policy ==="
aws iam get-role --role-name "$SLR_NAME" --query 'Role.AssumeRolePolicyDocument' --output json

echo ""
echo "=== Managed Policy Version ==="
aws iam get-policy \
  --policy-arn "arn:aws:iam::aws:policy/aws-service-role/AmazonLexV2BotPolicy" \
  --query 'Policy.{Version:DefaultVersionId,Updated:UpdateDate}' \
  --output json

echo ""
echo "=== Managed Policy Contents ==="
POLICY_VERSION=$(aws iam get-policy \
  --policy-arn "arn:aws:iam::aws:policy/aws-service-role/AmazonLexV2BotPolicy" \
  --query 'Policy.DefaultVersionId' --output text)
aws iam get-policy-version \
  --policy-arn "arn:aws:iam::aws:policy/aws-service-role/AmazonLexV2BotPolicy" \
  --version-id "$POLICY_VERSION" \
  --query 'PolicyVersion.Document' \
  --output json

echo ""
echo "=== Check for any NEW SLRs ==="
aws iam list-roles \
  --path-prefix "/aws-service-role/lexv2.amazonaws.com/" \
  --query 'Roles[].{Name:RoleName,Created:CreateDate}' \
  --output table
