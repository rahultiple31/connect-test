# Deployment Guide

This document covers exactly what the CDK stack deploys, what it does NOT deploy, what you need to configure manually, and how to test each layer.

## Prerequisites

- AWS CLI configured with credentials for the target account
- Python 3.12+
- Node.js 18+ (required by CDK CLI)
- AWS CDK CLI installed (`npm install -g aws-cdk`)
- An Amazon Connect instance (existing)
- The target account must be in `us-east-1`, `us-west-2`, or `eu-central-1` (required for Nova Sonic Generative Voice)

```bash
# Create and activate a virtual environment (Python 3.12+ required)
python3.12 -m venv .venv   # or python3.13, whichever is available
source .venv/bin/activate

# Install project dependencies
pip install -e ".[dev,infra]"

# Bootstrap CDK (first time only, per account/region)
cdk bootstrap aws://ACCOUNT_ID/REGION
```

---

## What the CDK Stack Deploys

Running `cdk deploy` creates all of the following automatically:

| Resource | Name / ID | Details |
|----------|-----------|---------|
| KMS Key | `ResponseTableKey` | Customer-managed, auto-rotation enabled |
| DynamoDB Table | `ConnectAsyncMultiResponseStack-AsyncResponseQueue…` (CDK-generated) | PK: SessionID, SK: SequenceNumber, on-demand billing, KMS encryption, TTL on `TTL` attribute. Lambdas get the real name via `RESPONSE_TABLE_NAME`; for CLI use: `aws lambda get-function-configuration --function-name ConnectAsync-Callback --query Environment.Variables.RESPONSE_TABLE_NAME` |
| DynamoDB GSI | `SessionStatusIndex` | PK: SessionStatus, SK: CreatedAt |
| Lambda | `ConnectAsync-Callback` | Python 3.12, 256MB, 10s timeout |
| Lambda | `ConnectAsync-Polling` | Python 3.12, 256MB, 8s timeout |
| Lambda | `ConnectAsync-Submit` | Python 3.12, 256MB, 8s timeout |
| Lambda | `ConnectAsync-Disconnect` | Python 3.12, 256MB, 10s timeout |
| Lambda Layer | `SharedLayer` | Shared modules (models, config, session_manager, tts, metrics, observability) |
| API Gateway | `ConnectAsyncCallbackApi` | REST API, `POST /callback`, stage: `prod` |
| API Key | `ConnectAsyncCallbackApiKey` | For callback endpoint authentication |
| Usage Plan | `ConnectAsyncCallbackUsagePlan` | 100 req/s rate, 200 burst |
| Request Validator | `BodyValidator` | Validates callback JSON body against schema |
| SSM Parameters | `/connect-async-multi-response/*` | 11 static parameters with defaults (+4 Spark params in spark mode) + 2 auto-wired Lex bot alias ARNs (see table below) |
| Secrets Manager | `connect-async/NEXTHINK_API_KEY` | Placeholder — must be updated manually |
| Secrets Manager | `connect-async/CALLBACK_API_KEY` | Placeholder — must be updated manually |
| CloudWatch Log Groups | `/aws/lambda/ConnectAsync-*` | 4 log groups, 2-week retention |
| IAM Roles | Per-Lambda | Least-privilege: scoped DynamoDB actions, KMS, SSM, CloudWatch, Secrets Manager |
| IAM Role | `LexBotRole` | Lex V2 runtime role (polly:SynthesizeSpeech, comprehend:DetectSentiment) |
| Lex V2 Bot | `AsyncResponseInterruptBot` | Interrupt bot: CancelQuery, AskNewQuestion, RepeatLastResponse, FallbackIntent |
| Lex V2 Bot | `NexThinkQuestionCapture` | Question capture bot: CaptureQuestion (AMAZON.FreeFormInput slot), FallbackIntent |
| Lex Bot Version + Alias | Per bot | Auto-built version + `live` alias for each bot |
| Connect Integration Associations | Conditional | Lex bots + Lambda functions registered with Connect instance (when `connect_instance_arn` provided) |
| Connect Phone Number | Conditional | DID or TOLL_FREE number claimed to instance (when `claim_phone_number=true`) |
| Wisdom Assistant | Conditional | Q in Connect assistant domain, KMS-encrypted (when `deploy_orchestrator=true`) |
| Wisdom AI Prompt | Conditional | Orchestration prompt loaded from `config/orchestrator_prompt.yaml` |
| ~~Wisdom AI Agent~~ + version | **Not in CDK** | Created post-deploy by `scripts/post_deploy.py` — it requires the WISDOM_ASSISTANT association to exist first, which CDK cannot create (see the two rows below). Note `configure_ai_agent.py` only *updates* an existing agent; it cannot create one. |
| AgentCore Gateway | Conditional | `ConnectAsyncMcpGateway` — MCP server with Connect OIDC auth (when `deploy_orchestrator=true`) |
| Gateway Lambda Target | Conditional | `submit-query` — routes to `ConnectAsync-Submit` Lambda (when `deploy_orchestrator=true`) |
| Gateway Lambda Target | Conditional | `check-responses` — routes to `ConnectAsync-Polling` Lambda (when `deploy_orchestrator=true`) |
| Lambda | Conditional | `ConnectAsync-McpRegistration` — Custom Resource handler (when `deploy_orchestrator=true` + `connect_instance_arn`). Does two things CFN can't: (1) sets the Gateway's JWT `allowedAudience` to the gateway's **own ID** post-create — a CFN resource can't reference its own attributes, so this can't be in the template; (2) creates the AppIntegrations Application with `ApplicationType=MCP_SERVER`, which the CFN resource doesn't expose. Does **not** create Connect associations and does **not** attach the tools to the AI agent — `scripts/post_deploy.py` does both (Step 6). |
| Custom Resource | Conditional | Invokes the Lambda above. **Connect integration associations (APPLICATION + WISDOM_ASSISTANT) are NOT created here** — `CreateIntegrationAssociation` internally calls `iam:PutRolePolicy` on the Connect SLR, which a Lambda-backed custom resource can't do under a permissions boundary. `scripts/post_deploy.py` creates them with your credentials. |
| SSM Parameter | Conditional | `/connect-async-multi-response/agentcore-gateway-arn` — Gateway ARN (when `deploy_orchestrator=true`) |
| Mock Nexthink Lambda | Conditional | Bedrock-backed mock agent (when `deploy_mock_nexthink=true`) |
| Mock Nexthink API GW | Conditional | `POST /agent` endpoint for the mock |

---

## What the CDK Stack Does NOT Deploy

The following resources must be created manually in the AWS Console or via separate tooling:

| Resource | Why not in CDK | Reference |
|----------|---------------|-----------|
| Amazon Connect instance | Typically pre-existing; complex to automate | — |
| Amazon Connect contact flow (Option A or E) | Must be built or imported in the Connect flow editor. `config/option_e_flow.json` and `config/option_a_flow.json` are human-readable design specs (not importable). `config/Main Flow -- Option E.json` is the actual deployed flow export and **is** importable. | See Step 7 below |
| Contact flow **modules** (Option E) | `config/flow_modules/basic-setting-configurations_flow.json` is **required** — the Option E flow invokes it. Import the modules *before* importing the flow, or the flow import fails on the missing module reference. | See Step 7 below |
| Lex bot — QinConnect (Option E) | **MUST be created via Connect admin console** — not CDK, not Lex API. Bots created outside Connect console are rejected at runtime with `BadRequestException`. See Step 6e. | See Step 6 below |
| AI agent + MCP tool grants | Automated by `scripts/post_deploy.py` (associations → AI agent → tool grants on the security profile). Console registration under *Third-party applications* is **not** required — the public API path is sufficient. | See Step 6 below |
| Phone number / queue assignment | Phone number: automated (opt-in via `claim_phone_number=true`). Flow association: manual in Connect console |
| Contact Lens enablement | Must be enabled at the instance level | Connect console → Analytics |

---

## Step-by-Step Deployment

### Step 1: Deploy the CDK Stack

> We recommend `--no-rollback` for all deploys. This prevents CloudFormation from rolling back the entire stack on partial failures, making it easier to debug and retry individual resources.

**Recommended: Use the deploy script**

Copy `.env.example` to `.env`, fill in your values, then run:

```bash
cp .env.example .env
# Edit .env with your Connect instance ARN and URL
./scripts/deploy.sh
./scripts/deploy.sh --no-mock   # skip mock Nexthink
```

The script reads from `.env` and passes values to CDK. You can also override via environment variables:

```bash
CONNECT_INSTANCE_ARN=arn:... CONNECT_INSTANCE_URL=https://... ./scripts/deploy.sh
```

**Manual CDK commands** (if you prefer direct control):

```bash
# Preview what will be created
cdk diff

# Deploy (basic — without Connect instance association)
cdk deploy --no-rollback

# Deploy WITH Connect instance association (recommended — associates Lex bots
# and Lambda functions with your Connect instance automatically)
cdk deploy --no-rollback -c connect_instance_arn=arn:aws:connect:us-east-1:123456789012:instance/your-instance-id

# Deploy AND claim a phone number for the IVR line
cdk deploy --no-rollback -c connect_instance_arn=arn:aws:connect:us-east-1:123456789012:instance/your-instance-id \
           -c claim_phone_number=true

# Deploy with Option E Orchestrator AI Agent (Wisdom assistant + AI agent + MCP tools)
cdk deploy --no-rollback -c connect_instance_arn=arn:aws:connect:us-east-1:123456789012:instance/your-instance-id \
           -c connect_instance_url=https://your-alias.my.connect.aws \
           -c deploy_orchestrator=true

# Full deployment (all optional resources)
cdk deploy --no-rollback -c connect_instance_arn=arn:aws:connect:us-east-1:123456789012:instance/your-instance-id \
           -c connect_instance_url=https://your-alias.my.connect.aws \
           -c claim_phone_number=true \
           -c deploy_orchestrator=true

# Deploy with mock Nexthink agent (Bedrock-backed, for prototyping without real Nexthink)
cdk deploy --no-rollback -c deploy_mock_nexthink=true

# Full prototype deployment (everything including mock)
cdk deploy --no-rollback -c connect_instance_arn=arn:aws:connect:us-east-1:123456789012:instance/your-instance-id \
           -c connect_instance_url=https://your-alias.my.connect.aws \
           -c claim_phone_number=true \
           -c deploy_orchestrator=true \
           -c deploy_mock_nexthink=true

# Customize phone number country/type (defaults: US, DID)
cdk deploy --no-rollback -c connect_instance_arn=arn:aws:connect:... \
           -c claim_phone_number=true \
           -c phone_country_code=GB \
           -c phone_type=TOLL_FREE

# Note these outputs (you'll need them later):
# - API Gateway URL: https://XXXXXXXX.execute-api.REGION.amazonaws.com/prod/callback
# - API Key: retrieve from API Gateway console → API Keys
# - Lambda ARNs: from CloudFormation outputs or Lambda console
```

When `connect_instance_arn` is provided, the stack also creates `AWS::Connect::IntegrationAssociation` resources that register both Lex bots and the Polling/Submit/Disconnect Lambda functions with the Connect instance. This eliminates the need to manually add them in the Connect console before building the contact flow.

When `claim_phone_number=true` is also provided, the stack claims a DID phone number (default: US) to the Connect instance. Customize with `phone_country_code` and `phone_type` context variables. Note: claiming a phone number incurs AWS telephony charges.

When `deploy_orchestrator=true` is provided (requires `connect_instance_arn` and `connect_instance_url`), the stack creates the Option E infrastructure:
- Q in Connect Assistant domain (KMS-encrypted)
- AI Prompt (ORCHESTRATION type, loaded from `config/orchestrator_prompt.yaml`)
- SSM parameter with the assistant ARN
- AgentCore Gateway (`ConnectAsyncMcpGateway`) with Connect OIDC inbound auth and Lambda targets for `submit_query` and `check_responses`
- MCP server registration via Custom Resource (creates AppIntegrations Application with `MCP_SERVER` type only — integration associations are created manually post-deploy)
- SSM parameter with the Gateway ARN

> **Important**: After CDK deploy, you must:
> 1. Create the APPLICATION and WISDOM_ASSISTANT integration associations manually (see Step 6a)
> 2. Register the MCP server via the Connect admin console (see Step 6b)
> 3. Create the AI Agent and add MCP tools via `scripts/configure_ai_agent.py` or the Connect console (see Step 6d)
>
> The CDK creates the infrastructure (Gateway, targets, Wisdom assistant, prompt), but the AI Agent and integration associations are created manually due to permissions boundary constraints.

When `deploy_mock_nexthink=true` is provided, the stack creates a mock Nexthink agent for prototyping:
- Lambda function (`ConnectAsync-MockNexThink`) backed by Amazon Bedrock (Claude 3 Haiku)
- API Gateway endpoint (`POST /agent`) that receives queries and sends multi-part callbacks
- SSM parameter `nexthink-agent-url` automatically pointed to the mock endpoint (overrides the placeholder)
- The mock receives a query, calls Bedrock to generate 2-4 response parts, then POSTs each part to the callback URL with delays — simulating real async behavior
- Works with both Option A and Option E (feeds the shared callback ingestion layer)

### Step 2: Sync the Callback API Key

The CDK creates the API Gateway API key and the Secrets Manager secret separately. Run this script to copy the API key value into the secret so the mock Nexthink Lambda can authenticate its callbacks:

```bash
python3 scripts/sync_callback_api_key.py
```

This reads the `ConnectAsyncCallbackApiKey` from API Gateway and writes it to `connect-async/CALLBACK_API_KEY` in Secrets Manager. Run once after each fresh deploy.

### Step 3: Update Secrets with Real Values

The CDK creates placeholder secrets. Update them with actual values:

**Mock backend (`NEXTHINK_BACKEND=mock`, the default):** nothing to do. The mock ignores `NEXTHINK_API_KEY`.

**Spark backend (`NEXTHINK_BACKEND=spark`):** the secret must hold the OAuth2 **HTTP Basic** credential — `base64("<client_id>:<client_secret>")` — that the Submit Lambda exchanges for a bearer token at `NEXTHINK_TOKEN_URL`:

```bash
BASIC=$(printf '%s:%s' "$NEXTHINK_CLIENT_ID" "$NEXTHINK_CLIENT_SECRET" | base64)
aws secretsmanager put-secret-value \
  --secret-id connect-async/NEXTHINK_API_KEY \
  --secret-string "$BASIC"
```

The other four Spark values (`NEXTHINK_TENANT_ID`, `NEXTHINK_TOKEN_URL`, `NEXTHINK_SPARK_URL`, `NEXTHINK_USER_PRINCIPAL`) are not secrets; `deploy.sh` reads them from `.env` and writes them to SSM. They are visible in SSM after deploy, and can be changed there without redeploying (the Lambda re-reads config on cold start).

> **Note**: The callback API key is already synced by Step 2. You do NOT need to set it manually.
> In spark mode it is still synced but **not enforced** on `/callback` — see [Architecture — Known Limitations](docs/architecture.md#known-limitations).

### Step 4: Update SSM Parameters

Update placeholder SSM parameters with real values:

```bash
# Set the actual Nexthink agent endpoint
aws ssm put-parameter \
  --name "/connect-async-multi-response/NEXTHINK_AGENT_URL" \
  --value "https://your-nexthink-instance.nexthink.com/api/agent" \
  --type String \
  --overwrite
```

The Lex bot alias ARNs are now automatically wired into SSM by the CDK stack:
- `/connect-async-multi-response/lex-bot-alias` — Interrupt bot alias ARN
- `/connect-async-multi-response/question-capture-bot-alias` — Question capture bot alias ARN

When deployed with `deploy_orchestrator=true`, the Wisdom assistant ARN is also auto-wired:
- `/connect-async-multi-response/wisdom-assistant-arn` — Q in Connect assistant ARN

All SSM parameters and their defaults:

| Parameter | Default | Update needed? |
|-----------|---------|---------------|
| `NEXTHINK_AGENT_URL` | `https://placeholder...` | Yes — set real URL |
| `LEX_BOT_ALIAS` | Auto-wired from CDK | No — interrupt bot alias ARN |
| `QUESTION_CAPTURE_BOT_ALIAS` | Auto-wired from CDK | No — question capture bot alias ARN |
| `WISDOM_ASSISTANT_ARN` | Auto-wired (if `deploy_orchestrator=true`) | No — Wisdom assistant ARN |
| `AGENTCORE_GATEWAY_ARN` | Auto-wired (if `deploy_orchestrator=true`) | No — AgentCore Gateway ARN |
| `POLLING_INTERVAL_MS` | `3000` | No (tune later) |
| `MAX_SESSION_DURATION_S` | `300` | No (tune later) |
| `INITIAL_RESPONSE_TIMEOUT_S` | `30` | No (tune later) |
| `HOLD_MESSAGE_POOL` | 3 messages (JSON) | No (customize later) |
| `POLLY_VOICE_ID` | `Matthew` | No |
| `NOVA_SONIC_VOICE` | `Matthew` | No |
| `MAX_RESPONSE_LENGTH` | `3000` | No |
| `SILENCE_TIMEOUT_S` | `4` | No |
| `MAX_POLL_ITERATIONS` | `10` | No |

### Step 5: Verify Lex Bots (automated by CDK)

Both Lex V2 bots are now created automatically by the CDK stack. Verify they were built successfully:

```bash
# List bots — should show AsyncResponseInterruptBot and NexThinkQuestionCapture
aws lexv2-models list-bots --query 'botSummaries[].botName'

# Verify the interrupt bot has all 4 intents
INTERRUPT_BOT_ID=$(aws lexv2-models list-bots \
  --query 'botSummaries[?botName==`AsyncResponseInterruptBot`].botId' --output text)
aws lexv2-models list-intents \
  --bot-id $INTERRUPT_BOT_ID --bot-version DRAFT --locale-id en_US \
  --query 'intentSummaries[].intentName'
# Expected: ["CancelQuery", "AskNewQuestion", "RepeatLastResponse", "FallbackIntent"]

# Verify the question capture bot
CAPTURE_BOT_ID=$(aws lexv2-models list-bots \
  --query 'botSummaries[?botName==`NexThinkQuestionCapture`].botId' --output text)
aws lexv2-models list-intents \
  --bot-id $CAPTURE_BOT_ID --bot-version DRAFT --locale-id en_US \
  --query 'intentSummaries[].intentName'
# Expected: ["CaptureQuestion", "FallbackIntent"]
```

The bot alias ARNs are automatically stored in SSM parameters and available to the Lambda functions.

### Step 6: Register MCP Server and Configure AI Agent (Option E only)

> **Why is this manual?** The CDK stack creates the AgentCore Gateway, Lambda targets, and AppIntegrations Application via API. However, Q in Connect maintains an internal tool catalog that is only populated when the MCP server is registered through the Connect admin console. The public `CreateApplication` + `CreateIntegrationAssociation` APIs create the plumbing but do not trigger the internal catalog sync. This is a known limitation — the [AWS Self-Service AI Agents Workshop](https://catalog.workshops.aws/self-service-ai-agents/en-US/02-reservation-system/02-associating-mcp-server) also performs this step through the console.

#### Step 6a: Run the post-deploy script (associations + AI Agent)

```bash
python3 scripts/post_deploy.py
```

Idempotent. Creates, in order, the APPLICATION and WISDOM_ASSISTANT integration associations on your instance, then the `NexthinkOrchestrationAgent` (ORCHESTRATION type, no tools yet) and pins a version. Resolves every ID from SSM and the account — pass `--instance-id` if the account has more than one Connect instance.

Then add the tools and grant them:

```bash
python3 scripts/post_deploy.py --tools
```

This discovers the tools from the gateway targets, updates the AI Agent, publishes a new agent version, and grants both tools on the **Admin** security profile (Admin does not inherit MCP tool access). Also idempotent.

> **Steps 6b–6d below are now fallbacks, not requirements.** Verified 2026-09-17 on a fresh account: after `post_deploy.py` (no console interaction at all), the AWS Console → Amazon Connect → **Integrations** page already lists the MCP server, and the security-profile tool grant via API **succeeds** — which is only possible if the tools are in Connect's catalog. The earlier belief that only the console populates the catalog dates from when the gateway's JWT audience was misconfigured; with the audience set correctly at deploy time, the API path is sufficient.
>
> **Only fall back to 6b if `--tools` fails on Step 5 with "not found".** Note that `aws connect list-security-profile-applications` returns apps *granted* to a profile, not the catalog — an empty list before you grant anything is normal and proves nothing.

#### Old Step 6a (only if re-registering after a failed attempt): delete a stale MCP server application

If a previous console registration conflicted with the CDK-created application ("Name already in use"), clean up first:

```bash
# Check for existing APPLICATION associations
aws connect list-integration-associations \
  --instance-id YOUR_INSTANCE_ID \
  --integration-type APPLICATION \
  --query 'IntegrationAssociationSummaryList[*].[IntegrationAssociationId, IntegrationArn]' \
  --output table

# Delete any existing APPLICATION associations
aws connect delete-integration-association \
  --instance-id YOUR_INSTANCE_ID \
  --integration-association-id THE_ASSOCIATION_ID

# Delete any existing MCP server applications
python3 -c "
import boto3
client = boto3.client('appintegrations')
apps = client.list_applications(MaxResults=50)
for app in apps.get('Applications', []):
    if 'ConnectAsync' in app['Name'] or 'McpServer' in app['Name']:
        print(f'Deleting: {app[\"Name\"]} ({app[\"Arn\"]})')
        client.delete_application(Arn=app['Arn'])
"
```

#### Step 6b (fallback): Register MCP server via Connect console

Only if `post_deploy.py --tools` failed with "not found" on the security-profile grant. Otherwise the MCP server is already registered — clicking "Add application" would collide on the name.

Follow the [AWS workshop instructions](https://catalog.workshops.aws/self-service-ai-agents/en-US/02-reservation-system/02-associating-mcp-server):

1. Open the **Amazon Connect console** → select your instance
2. In the left navigation, click **Third-party applications**
3. Click **Add application**
4. Configure:
   - **Display name**: `ConnectAsyncMcpServer`
   - **Description**: `MCP server for async multi-response pattern`
   - **Application type**: Select **MCP server**
5. In **Application details**, select the `ConnectAsyncMcpGateway` gateway from the dropdown
   - If the gateway doesn't appear, verify the gateway's inbound auth Discovery URL is set to `https://YOUR-ALIAS.my.connect.aws/.well-known/openid-configuration`
6. In **Instance association**, select your Connect instance
   - If you can't select the instance, the gateway's Discovery URL doesn't match your instance URL
7. Click **Save**

#### Step 6c (fallback): Grant tool access on a Security Profile

`post_deploy.py --tools` already does this for the **Admin** profile via `UpdateSecurityProfile` with `Type=MCP` and the tool identifiers `<target>___<tool>` as permissions. Use the console only for a *different* profile, or to inspect what was granted.

1. Log into the **Connect admin website** (not the AWS console — the Connect admin URL, e.g., `https://your-alias.my.connect.aws`)
2. Go to **Users** → **Security profiles**
3. Either create a new profile or edit the **Admin** profile
4. Scroll to the **Tools** section
5. Grant **Access** to both MCP tools (`check_responses__check_responses` and `submit_query__submit_query`)
6. Click **Save**

> **Important**: The Admin security profile does NOT automatically include MCP tool permissions. You must explicitly grant "Access" per tool, even for Admin. Without this, the AI Agent will show "Insufficient" permissions and tools won't work at runtime.

#### Step 6d: Configure the AI Agent with MCP tools

You can either add tools manually via the console (see below) or use the automated script.

**Automated (recommended after first-time console MCP registration):**

```bash
python3 scripts/configure_ai_agent.py
```

This script:
1. Discovers tools from the deployed AgentCore Gateway targets
2. Updates the AI Agent with the correct MCP tool configurations
3. Publishes a new AI Agent version
4. Reports security profile tool access status

**Manual (via Connect admin console):**

1. In the Connect admin website, go to **AI agent designer** → **AI agents**
2. Find the orchestration agent (created via `scripts/configure_ai_agent.py` or manually)
3. Click to edit it
4. Set the **locale** to `English (US)`
5. Select the security profile (Admin or the one created in Step 6c)
6. In the **Tools** section, click **Add tool**
7. Select the **Namespace** from the dropdown — it will be `gateway_connectasyncmcpgateway-XXXXX`
8. Select the tool from the dropdown
9. Click **Add**
10. Repeat for the second tool
11. **Save** the AI Agent
12. **Publish** the AI Agent to make it the active version

> **Note on tool names**: The gateway prefixes tool names with the target name and triple underscores (`___`). The console displays them with double underscores (`__`), but the API uses triple. The tools will appear as `check_responses__check_responses` and `submit_query__submit_query` in the console dropdown.

#### Step 6e: Create the Lex bot with QinConnectIntent (Connect console ONLY)

> **CRITICAL**: This bot MUST be created through the Connect admin website's bot builder. Bots created via the Lex API, CLI, or CloudFormation will NOT work — Connect returns `BadRequestException` at runtime. The [AWS Self-Service AI Agents Workshop](https://catalog.workshops.aws/self-service-ai-agents/en-US/02-reservation-system/07-create-lex-bot) confirms: "Amazon Connect AI agent is not supported for the Conversational AI bot created outside Connect console."

**Prerequisites:**
- `BOT_MANAGEMENT` must be enabled on the Connect instance. If not already enabled:
  ```bash
  aws connect update-instance-attribute --instance-id YOUR_INSTANCE_ID --attribute-type BOT_MANAGEMENT --value true
  aws connect update-instance-attribute --instance-id YOUR_INSTANCE_ID --attribute-type ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS --value true
  ```
- If you just enabled `BOT_MANAGEMENT`, or if you get "could not access your Q In Connect Assistant" errors later, **toggle it off and back on** in the Connect console (Flows page) to force a refresh of the Lex Service-Linked Role permissions. This adds the required Wisdom + KMS inline policies to the SLR.

**Steps:**

1. Log in to the Connect admin website: `https://your-alias.my.connect.aws`
2. Navigate to **Routing** → **Flows** → **Bots** (or **Conversational AI**)
3. Click **Create Conversational AI bot**
4. Configure:
   - Bot name: e.g., `NexThinkOptionEBot`
   - COPPA: Choose `No` (unless required)
5. Click **Create**
6. Click **Add language** → Select **English (US)**
7. Navigate to the **Configuration** tab
8. Find the **Amazon Connect AI agent** / **Connect intent** toggle → set to **Enabled**
9. In the dialog, select your **Wisdom Assistant ARN** from the dropdown
10. Click **Confirm**
11. Click **Build language** (top right) and wait for "Successfully built" (~2 minutes)

The bot is now available in your Connect instance for use in contact flows.

**Optional: Enable Nova Sonic Speech-to-Speech (recommended for voice quality)**

After the basic bot is working, you can upgrade to Nova Sonic:

1. In the bot's **Configuration** tab, find the **Speech model** section
2. Click **Edit**
3. Set **Model type** to **Speech-to-speech**
4. Set **Voice provider** to **Amazon Nova Sonic**
5. Click **Confirm**
6. Click **Build language** again

> Test with basic voice first to confirm the Orchestrator works, then enable Nova Sonic.

**If you get "could not access your Q In Connect Assistant" after creating the bot:**

This means the Lex Service-Linked Role doesn't have Wisdom permissions. Fix:
1. Go to AWS Console → Amazon Connect → select instance → **Flows**
2. Uncheck "Enable Lex Bot Management in Amazon Connect" → **Save**
3. Re-check it → **Save**
4. Also toggle "Enable Bot Analytics and Transcripts" off/on → **Save**
5. Go back to Connect admin, open your bot, and **rebuild**

This forces Connect to refresh the SLR, adding `wisdom:CreateSession`, `wisdom:GetAssistant`, `wisdom:SendMessage`, `wisdom:GetNextMessage` and KMS decrypt permissions.

**If the Wisdom assistant uses a customer-managed KMS key (as deployed by CDK):**

The Lex SLR also needs a KMS grant. The BOT_MANAGEMENT toggle should add this automatically, but if not:
```bash
python3 scripts/fix_kms_grant_for_lex.py
```

**If you deployed without `deploy_orchestrator`**: follow the full manual setup:

1. In the Amazon Connect console, go to **Amazon Q in Connect** (or AI Agent settings)
2. Create an **Orchestration AI Agent** assistant domain
3. Configure the model: select Nova Sonic for voice
4. Set the custom prompt using the content from `config/orchestrator_prompt.yaml`
5. Add the MCP tools to the AI agent and grant them on the security profile (`python3 scripts/post_deploy.py --tools`, or Step 6b/6d as a console fallback)
6. Create a Lex bot with `AMAZON.QinConnectIntent` enabled
7. Note the assistant domain ARN and Lex bot alias ARN

### Step 7: Import the Contact Flow

#### Step 7a: Import the flow modules first

The Option E flow invokes a flow module, so the modules must exist **before** the flow is
imported — otherwise the import fails on the unresolved module reference.

In the Connect console: **Flows → Modules → Create module → Import (⋮ menu)**, once per file:

| File | Status |
|------|--------|
| `config/flow_modules/basic-setting-configurations_flow.json` | **Required** |
| `config/flow_modules/customer-profile-lookup_flow.json` | Optional (see note below) |

Then open the imported `basic-setting-configurations` module, select its **Create Wisdom session**
block, and point it at **your** Q in Connect assistant — the export carries the workshop's
assistant ARN, which does not exist in your account. **Save** and **Publish** the module.

`scripts/post_deploy.py` prints your assistant ARN, or:

```bash
aws connect list-integration-associations \
  --instance-id "$CONNECT_INSTANCE_ID" \
  --integration-type WISDOM_ASSISTANT \
  --query 'IntegrationAssociationSummaryList[0].IntegrationArn' --output text
```

> `customer-profile-lookup_flow.json` is optional. Importing it is harmless — every branch
> converges on the same next block — but it calls a Lambda in the workshop account
> `055929692747`, so the lookup will not resolve in your account.

See [config/flow_modules/README.md](config/flow_modules/README.md) for details.

#### Step 7b: Import the flow

**For Option E** (recommended — simpler flow):

Import `config/Main Flow -- Option E.json` in the Connect flow editor
(**Flows → Create flow → Import**). Follow `config/option_e_flow.json` for a human-readable
block-by-block description of what it contains.

The other JSON files in `config/` are reference specifications — they describe every block, its
configuration, and its branching logic, but they are **not** importable into Connect. To build the
flow by hand instead, see [CONTACT_FLOW_BUILD_GUIDE.md](CONTACT_FLOW_BUILD_GUIDE.md) for
block-by-block instructions with exact settings, wiring, and session attribute configurations.

> **After import, fix two environment-specific references, then Publish:**
>
> 1. **Lex bot** — the export carries a placeholder
>    `arn:aws:lex:us-east-1:123456789012:bot-alias/YOUR_BOT_ID/YOUR_ALIAS_ID` in the
>    *Get Customer Input* block. Open that block and re-select your QinConnect bot
>    (created in Step 6e) and its alias.
> 2. **Flow modules** — the two `FlowModuleId` values point at the module IDs from the source
>    account. Open each *Invoke module* block and re-select the modules you imported in Step 7a.

The flow has 8 blocks:

1. **Set Recording & Analytics** — enable Contact Lens real-time (required for AI agents on voice)
2. **Set Voice** — Nova Sonic, Generative style, Matthew en-US
3. **Connect Assistant** — associate the AI agent domain from Step 6
4. **Get Customer Input** — Lex bot with `AMAZON.QinConnectIntent`
5. **Check Contact Attribute** — route on tool name: COMPLETE → end, ESCALATION → transfer
6. **Error Handler** — play error message, retry or end
7. **Transfer to Queue** — for escalation
8. **Disconnect** — end call

**For Option A** (fallback — more complex):

Follow `config/option_a_flow.json`. The flow has 19 blocks. Key wiring:

- Block 4 (Submit Lambda): set the `lambdaFunction` to the `ConnectAsync-Submit` ARN
- Block 7 (Polling Lambda): set the `lambdaFunction` to the `ConnectAsync-Polling` ARN
- Block 3 (Get Customer Input): point to the `NexThinkQuestionCapture` bot (created by CDK)
- Block 11 (Lex Interrupt): point to the `AsyncResponseInterruptBot` (created by CDK)
- The polling loop: blocks 7 → 8 → (9 or 10) → back to 7
- Pass `callbackUrl` as the API Gateway URL from Step 1

### Step 8: Configure the Disconnect Handler

The Disconnect Lambda must be triggered when a caller hangs up during an active session.

1. In the Connect contact flow, set the **Disconnect flow** to a flow that invokes the `ConnectAsync-Disconnect` Lambda
2. The Lambda reads `sessionId` from `event["Details"]["ContactData"]["Attributes"]["sessionId"]`
3. Alternatively, create a minimal disconnect flow with one "Invoke Lambda" block pointing to `ConnectAsync-Disconnect`

### Step 9: Assign a Phone Number

**If you deployed with `claim_phone_number=true`**: a phone number was already claimed to your Connect instance by the CDK stack. Find it in the Connect console under Phone Numbers, or via:

```bash
aws connect list-phone-numbers-v2 \
  --target-arn arn:aws:connect:REGION:ACCOUNT:instance/INSTANCE_ID \
  --query 'ListPhoneNumbersSummaryList[].PhoneNumber'
```

Associate it with the contact flow in the Connect console (Phone Numbers → select the number → set Contact Flow).

**If you deployed without `claim_phone_number`**: claim and assign a number manually:

1. In the Amazon Connect console, go to **Phone numbers**
2. Claim or assign a phone number
3. Associate it with the contact flow created in Step 6

### Step 10: Provide the Callback URL to Nexthink

**Spark backend:** nothing to hand over. The Submit Lambda passes the callback URL to Spark on every request (`configuration.pushNotification.url`), and Spark POSTs its own `statusUpdate` format, which the Callback Lambda translates automatically. What Nexthink *does* need from you is the **NAT Gateway Elastic IP** for their allow-list — read it from the stack output (`NatGatewayEip`) and see [Networking](docs/networking.md).

**Custom / non-Spark agent:** if you point `NEXTHINK_AGENT_URL` at your own agent (mock-style contract), give its team:
- **Callback URL**: `https://XXXXXXXX.execute-api.REGION.amazonaws.com/prod/callback`
- **API Key**: the value from API Gateway → API Keys (sent as `x-api-key` header)
- **Payload format**:
  ```json
  {
    "sessionId": "uuid-from-submit",
    "sequenceNumber": 1,
    "responseText": "Response content",
    "isComplete": false
  }
  ```

---

## Testing

### Layer 1: Unit Tests (no AWS required)

```bash
pytest tests/unit/ -v
```

Runs the unit suite using moto mocks. Covers all Lambda handlers, session manager, models, config loader, and TTS utilities. No AWS credentials needed.

### Layer 2: Test the Callback Endpoint (after CDK deploy)

Send a test callback to verify the API Gateway → Lambda → DynamoDB path:

```bash
# First, create a test session directly in DynamoDB
aws dynamodb put-item \
  --table-name AsyncResponseQueue \
  --item '{
    "SessionID": {"S": "test-session-001"},
    "SequenceNumber": {"N": "0"},
    "SessionStatus": {"S": "ACTIVE"},
    "QueryText": {"S": "test query"},
    "CreatedAt": {"N": "'$(date +%s000)'"},
    "TTL": {"N": "'$(($(date +%s) + 86400))'"},
    "IsComplete": {"BOOL": false},
    "Delivered": {"BOOL": false},
    "ResponseCount": {"N": "0"}
  }'

# Get the API key value
API_KEY=$(aws apigateway get-api-key \
  --api-key $(aws apigateway get-api-keys --query 'items[?name==`ConnectAsyncCallbackApiKey`].id' --output text) \
  --include-value --query 'value' --output text)

# Get the API URL
API_URL=$(aws cloudformation describe-stacks \
  --stack-name ConnectAsyncMultiResponseStack \
  --query 'Stacks[0].Outputs[?contains(OutputKey,`CallbackApi`)].OutputValue' --output text)

# Send a test callback
curl -X POST "${API_URL}prod/callback" \
  -H "Content-Type: application/json" \
  -H "x-api-key: ${API_KEY}" \
  -d '{
    "sessionId": "test-session-001",
    "sequenceNumber": 1,
    "responseText": "This is a test response from Nexthink.",
    "isComplete": false
  }'
# Expected: {"message": "Response stored"}

# Verify the record was written
aws dynamodb get-item \
  --table-name AsyncResponseQueue \
  --key '{"SessionID": {"S": "test-session-001"}, "SequenceNumber": {"N": "1"}}' \
  --query 'Item.{Text: ResponseText.S, Delivered: Delivered.BOOL}'
# Expected: {"Text": "This is a test response from Nexthink.", "Delivered": false}
```

**Test validation (should return 400):**

```bash
curl -X POST "${API_URL}prod/callback" \
  -H "Content-Type: application/json" \
  -H "x-api-key: ${API_KEY}" \
  -d '{"sessionId": "test-session-001", "sequenceNumber": 0, "responseText": "bad seq"}'
# Expected: 400 (sequenceNumber must be >= 1)
```

**Test unknown session (should return 404):**

```bash
curl -X POST "${API_URL}prod/callback" \
  -H "Content-Type: application/json" \
  -H "x-api-key: ${API_KEY}" \
  -d '{"sessionId": "nonexistent", "sequenceNumber": 1, "responseText": "hello", "isComplete": false}'
# Expected: {"error": "Session not found"}
```

**Test missing API key (should return 403):**

```bash
curl -X POST "${API_URL}prod/callback" \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "test-session-001", "sequenceNumber": 2, "responseText": "no key", "isComplete": false}'
# Expected: 403 Forbidden
```

### Layer 3: Test the Polling Lambda (after CDK deploy)

Invoke the Polling Lambda directly to verify it reads from DynamoDB:

```bash
# Option E format (MCP tool)
aws lambda invoke \
  --function-name ConnectAsync-Polling \
  --payload '{"sessionId": "test-session-001", "cursor": 0}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
# Expected: {"status": "results", "responses": [{"sequenceNumber": 1, "text": "This is a test response from Nexthink."}], "cursor": 1, ...}

# Poll again — should show "processing" (no new undelivered responses)
aws lambda invoke \
  --function-name ConnectAsync-Polling \
  --payload '{"sessionId": "test-session-001", "cursor": 1}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
# Expected: {"status": "processing", "cursor": 1, ...}

# Option A format (contact flow)
aws lambda invoke \
  --function-name ConnectAsync-Polling \
  --payload '{"Details": {"Parameters": {"sessionId": "test-session-001", "cursor": "0"}}}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
# Expected: {"status": "results", "responseText": "<speak>...</speak>", "cursor": "1", ...}
```

### Layer 4: Test the Submit Lambda (after CDK deploy)

The Submit Lambda POSTs to the Nexthink agent, so this test requires either the real Nexthink endpoint or a mock.

**With a mock endpoint** (recommended for initial testing):

1. Set up a temporary endpoint that returns 202 (e.g., using https://webhook.site or a simple Lambda behind API Gateway)
2. Update the SSM parameter:
   ```bash
   aws ssm put-parameter \
     --name "/connect-async-multi-response/NEXTHINK_AGENT_URL" \
     --value "https://your-mock-endpoint.example.com" \
     --type String --overwrite
   ```
3. Invoke:
   ```bash
   aws lambda invoke \
     --function-name ConnectAsync-Submit \
     --payload '{
       "transcript": "My laptop is slow",
       "sessionId": "test-submit-001",
       "callbackUrl": "https://YOUR_API_URL/prod/callback"
     }' \
     --cli-binary-format raw-in-base64-out \
     /dev/stdout
   # Expected: {"status": "submitted", "sessionId": "test-submit-001"}
   ```
4. Verify the session was created:
   ```bash
   aws dynamodb get-item \
     --table-name AsyncResponseQueue \
     --key '{"SessionID": {"S": "test-submit-001"}, "SequenceNumber": {"N": "0"}}' \
     --query 'Item.{Status: SessionStatus.S, Query: QueryText.S}'
   # Expected: {"Status": "ACTIVE", "Query": "My laptop is slow"}
   ```

### Layer 5: Test the Disconnect Lambda (after CDK deploy)

```bash
# Create a session first
aws lambda invoke \
  --function-name ConnectAsync-Submit \
  --payload '{
    "transcript": "test disconnect",
    "sessionId": "test-disconnect-001",
    "callbackUrl": "https://mock.example.com"
  }' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout

# Simulate a disconnect
aws lambda invoke \
  --function-name ConnectAsync-Disconnect \
  --payload '{"sessionId": "test-disconnect-001"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
# Expected: {"status": "processed", "finalStatus": "ABANDONED", ...}

# Verify session is ABANDONED
aws dynamodb get-item \
  --table-name AsyncResponseQueue \
  --key '{"SessionID": {"S": "test-disconnect-001"}, "SequenceNumber": {"N": "0"}}' \
  --query 'Item.SessionStatus.S'
# Expected: "ABANDONED"
```

### Layer 6: End-to-End Test (after contact flow is built)

1. Call the phone number assigned to the contact flow
2. Speak a question
3. While on hold, send callbacks to the API Gateway endpoint using the session ID from the DynamoDB table:
   ```bash
   # Find the active session
   aws dynamodb scan \
     --table-name AsyncResponseQueue \
     --filter-expression "SessionStatus = :s AND SequenceNumber = :z" \
     --expression-attribute-values '{":s": {"S": "ACTIVE"}, ":z": {"N": "0"}}' \
     --query 'Items[0].SessionID.S'

   # Send responses
   curl -X POST "${API_URL}prod/callback" \
     -H "Content-Type: application/json" \
     -H "x-api-key: ${API_KEY}" \
     -d '{"sessionId": "THE_SESSION_ID", "sequenceNumber": 1, "responseText": "First part of the answer.", "isComplete": false}'

   # Wait a few seconds, then send the final response
   curl -X POST "${API_URL}prod/callback" \
     -H "Content-Type: application/json" \
     -H "x-api-key: ${API_KEY}" \
     -d '{"sessionId": "THE_SESSION_ID", "sequenceNumber": 2, "responseText": "Second part. That is everything.", "isComplete": true}'
   ```
4. Verify the caller hears both responses spoken back
5. Verify the call ends gracefully after the completion message

---

## Monitoring

After deployment, check these to confirm the system is healthy:

```bash
# Check Lambda errors (last 15 minutes)
for fn in Callback Polling Submit Disconnect; do
  echo "=== ConnectAsync-${fn} ==="
  aws logs filter-log-events \
    --log-group-name "/aws/lambda/ConnectAsync-${fn}" \
    --start-time $(($(date +%s) * 1000 - 900000)) \
    --filter-pattern "ERROR" \
    --query 'events[].message' --output text
done

# Check CloudWatch metrics
aws cloudwatch get-metric-statistics \
  --namespace ConnectAsyncMultiResponse \
  --metric-name CallbackReceived \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Sum

# Check DynamoDB for stuck sessions (ACTIVE for > 10 minutes)
aws dynamodb scan \
  --table-name AsyncResponseQueue \
  --filter-expression "SessionStatus = :s AND SequenceNumber = :z AND CreatedAt < :t" \
  --expression-attribute-values '{
    ":s": {"S": "ACTIVE"},
    ":z": {"N": "0"},
    ":t": {"N": "'$(($(date +%s) * 1000 - 600000))'"}
  }' \
  --query 'Items[].SessionID.S'
```

---

## Teardown

```bash
# Destroy all resources
cdk destroy

# This removes everything EXCEPT:
# - Lex bots for Option E / QinConnect (created manually by Connect)
# - Connect contact flows (created manually)
# - Phone number → contact flow association (manual in Connect console)
# Remove those manually from their respective consoles.
# Note: Option A Lex bots, Wisdom assistant/agent (if deployed with
# deploy_orchestrator), and claimed phone numbers ARE destroyed
# automatically since they are CDK-managed.
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `MCP tool with ID 'X' not found in MCP tools` during AI Agent creation | MCP server not registered via Connect console (API-only registration doesn't populate Q in Connect's internal tool catalog) | Register MCP server via Connect console: Third-party applications → Add application → MCP server. See Step 6. |
| Gateway doesn't appear in Connect console MCP server dropdown | Gateway Discovery URL doesn't match Connect instance URL | Update gateway inbound auth Discovery URL to `https://YOUR-ALIAS.my.connect.aws/.well-known/openid-configuration` |
| Can't select instance in MCP server Instance Association | Same as above — Discovery URL mismatch | Fix the gateway Discovery URL |
| `BadRequestException` wrapping `TimeoutException` (~2s) | Lex bot created via API/CLI instead of Connect console | Delete the API-created bot and create a new one through the Connect admin website (Routing → Bots → Create Conversational AI bot). See Step 6e. |
| `Invalid Bot Configuration: Amazon Lex could not access your Q In Connect Assistant` (ValidationException, ~8s) | Lex SLR missing Wisdom/KMS permissions | Toggle BOT_MANAGEMENT off/on in Connect console (Flows page) to refresh SLR permissions. See Step 6e. |
| `AMAZON.QInConnectIntent` not available as built-in intent | `BOT_MANAGEMENT` not enabled on Connect instance | Enable via: `aws connect update-instance-attribute --instance-id ID --attribute-type BOT_MANAGEMENT --value true` |
| CloudFormation rejects `AMAZON.QInConnectIntent` as `parentIntentSignature` | CFN `AWS::Lex::Bot` does not support this intent type | Known CFN gap. Must create bot via Connect console, not CDK. |
| Callback returns 403 | Missing or wrong API key | Check `x-api-key` header matches the API Gateway key |
| Callback returns 404 | Session doesn't exist | Verify the session was created by the Submit Lambda first |
| Polling returns `"error"` with "Session not found" | Wrong `sessionId` or session expired (TTL) | Check DynamoDB for the session record |
| Polling always returns `"processing"` | Callbacks not arriving | Check Nexthink is POSTing to the correct URL with the correct API key |
| Submit returns `"error"` with connection error | Wrong Nexthink URL or network issue | Check SSM parameter `NEXTHINK_AGENT_URL` and Lambda VPC/security group config |
| Lambda `ImportModuleError: No module named 'lambdas'` | Shared layer packaging mismatch — layer puts files flat but handler imports `from lambdas.shared...` | Fix layer packaging to preserve `lambdas/shared/` directory structure, or change imports |
| Lambda timeout (8s) | DynamoDB latency or cold start | Check CloudWatch logs; consider provisioned concurrency |
| Contact flow hangs after Submit | Submit Lambda returned `"error"` but flow doesn't branch correctly | Check the "Check Submission Status" block routes non-`"submitted"` to error |
| Caller hears nothing during hold | `holdMessage` not wired in the Play Prompt block | Ensure block 10 reads `$.External.holdMessage` |
| Option E: Orchestrator engages but can't invoke MCP tools | AI Agent has no tool configurations | Run `python3 scripts/configure_ai_agent.py` or add tools via Connect AI agent designer |
