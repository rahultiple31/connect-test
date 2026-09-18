#!/bin/bash
# Check CloudFormation stack status and recent events
aws cloudformation describe-stacks \
  --stack-name ConnectAsyncMultiResponseStack \
  --region us-east-1 \
  --query 'Stacks[0].StackStatus' \
  --output text

echo "---"

aws cloudformation describe-stack-events \
  --stack-name ConnectAsyncMultiResponseStack \
  --region us-east-1 \
  --max-items 12 \
  --query 'StackEvents[].{Time:Timestamp,Resource:LogicalResourceId,Status:ResourceStatus,Reason:ResourceStatusReason}' \
  --output table
