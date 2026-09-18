# Networking

## VPC Architecture

The Submit Lambda runs inside a VPC private subnet to provide a static outbound IP address via NAT Gateway. This is required because the real Nexthink Spark agent exposes an API Gateway with IP whitelisting.

```
                                    VPC (ConnectAsync-SubmitLambdaVpc)
┌──────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│   Private Subnet (AZ 1)              Public Subnet (AZ 1)               │
│   ┌─────────────────────┐            ┌──────────────────────┐           │
│   │   Submit Lambda      │            │   NAT Gateway         │           │
│   │   (ENIs)             │───────────→│   EIP (see outputs)   │──→ IGW ──→ Internet
│   └─────────────────────┘            └──────────────────────┘           │
│                                                                          │
│   Private Subnet (AZ 2)              Public Subnet (AZ 2)               │
│   ┌─────────────────────┐            ┌──────────────────────┐           │
│   │   (available for     │            │   (no NAT GW —        │           │
│   │    HA if needed)     │            │    single NAT for POC)│           │
│   └─────────────────────┘            └──────────────────────┘           │
│                                                                          │
│   DynamoDB Gateway Endpoint (free — avoids NAT for DDB traffic)         │
└──────────────────────────────────────────────────────────────────────────┘
```

### What's in the VPC

Only the Submit Lambda. It's the only function that makes outbound HTTP calls to external endpoints (Nexthink).

### What's NOT in the VPC

- Callback Lambda — invoked by API Gateway, only talks to DynamoDB/SSM/Secrets Manager via SDK
- Polling Lambda — invoked by Connect or AgentCore Gateway, only talks to DynamoDB via SDK
- Disconnect Lambda — invoked by Connect, only talks to DynamoDB via SDK
- Mock Nexthink Lambda — it's the target, not the caller

### Security Group

`SubmitLambdaSg`: egress TCP 443 only (`allow_all_outbound=False`). No ingress rules — Lambda is invoked via the Lambda API, not via network.

### VPC Endpoints

- DynamoDB Gateway Endpoint (free) — DynamoDB traffic stays on the AWS backbone
- No interface endpoints — all other AWS API calls (SSM, Secrets Manager, KMS, CloudWatch) go through NAT. At POC volumes the data processing cost is negligible.

### Cost

~$32/month for the NAT Gateway (hourly charge + minimal data processing).

## IP Whitelisting

The NAT Gateway's Elastic IP is the single outbound IP for all Submit Lambda traffic. Whitelist this IP on the Nexthink API Gateway resource policy.

Retrieve the EIP from the CloudFormation stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name ConnectAsyncMultiResponseStack \
  --query 'Stacks[0].Outputs[?OutputKey==`NatGatewayEip0`].OutputValue' \
  --output text
```

## AgentCore Gateway Compatibility

Placing the Submit Lambda in a VPC does not affect the AgentCore Gateway integration. The Gateway invokes Lambda via the `lambda:InvokeFunction` API (not HTTP), so VPC placement is transparent to the caller.

Invocation path (unchanged):
```
Connect Orchestrator → AgentCore Gateway → lambda:InvokeFunction → Submit Lambda (in VPC)
```

No IAM or Gateway configuration changes needed. [AWS docs confirm](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-vpc-egress.html): "no additional configuration from customers — the gateway can immediately invoke Lambda functions that have been configured with VPC access."

## Submit Lambda Outbound Dependencies (from VPC)

| Service | Access Path | Purpose |
|---------|-------------|---------|
| DynamoDB | Gateway endpoint (free) | Session create + orphan cleanup |
| Nexthink endpoint | NAT Gateway → Internet | HTTP POST query submission |
| Secrets Manager | NAT Gateway | Read API keys |
| SSM Parameter Store | NAT Gateway | Load config |
| KMS | NAT Gateway | DynamoDB encryption |
| CloudWatch | NAT Gateway | PutMetricData |

## Scaling Considerations

For production with higher traffic:
- Add interface VPC endpoints for Secrets Manager, SSM, KMS, CloudWatch (~$7.20/mo each) to reduce NAT data processing costs
- Add a second NAT Gateway for multi-AZ resilience (second EIP to whitelist)
- The VPC is already configured with 2 AZs and subnets in both — just add `nat_gateways=2` in the CDK


---

## See Also

- [Architecture](architecture.md) — System overview and how the VPC fits into the broader design
- [Deployment Guide](../DEPLOYMENT.md) — CDK deploy commands and stack outputs
