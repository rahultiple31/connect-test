# Connect Async Multi-Response

Amazon Connect voice IVR that delivers multiple asynchronous responses from an external AI agent (Nexthink) to a caller in a fully automated self-service scenario.

In the standard Connect model, a caller asks a question and gets one synchronous response. This system breaks that pattern: a single caller input triggers the Nexthink AI agent, which produces multiple discrete responses over time via callback. Each response is individually spoken to the caller while the call remains active.

## How It Works

```
Caller speaks → Connect ──→ Submit Lambda ──→ Nexthink Agent
                  │  ↑                            │
                  │  │                            ↓ (async callbacks)
                  │  │                        API Gateway → Callback Lambda → DynamoDB
                  │  │                                                           ↑
                  │  └── Polling Lambda ─────────────────────────────────────────┘
                  ↓
            Caller hears each response as it arrives
```

The system has two architecture options sharing the same backend. See [Architecture](docs/architecture.md) for details.

| | Option E (Primary) | Option A (Fallback) |
|---|---|---|
| Approach | Orchestrator AI Agent + MCP Tools | Lambda Polling Loop + DynamoDB |
| Polling logic | LLM reasoning loop | Contact flow loop |
| Voice | Nova Sonic Speech-to-Speech | Amazon Polly / SSML |
| Tool timeout | 30s per MCP invocation | 8s per Lambda invocation |
| Contact flow | ~8 blocks | ~19 blocks |
| Caller interrupts | Handled by Orchestrator natively | Lex interrupt bot |

## Option E Architecture

The primary architecture uses the Connect AI Agent Orchestrator to handle the entire conversation. The Orchestrator reasons via LLM, invokes MCP tools through an AgentCore Gateway, and speaks to the caller via Nova Sonic.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Amazon Connect Contact Flow (8 blocks)                                         │
│                                                                                 │
│  Entry → Set Recording → Set Voice (Nova Sonic) → Connect Assistant             │
│              → Get Customer Input (Lex + QinConnectIntent)                      │
│                         │                                                       │
│                         ▼                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │  Orchestrator AI Agent (LLM reasoning loop)                             │    │
│  │                                                                         │    │
│  │  1. submit_query ──→ AgentCore Gateway ──→ Submit Lambda ──→ Nexthink   │    │
│  │     ← {submitted}     (MCP server)          (in VPC)     POST via NAT   │    │
│  │     → speaks: "I'm looking into that..."                                │    │
│  │                                                                         │    │
│  │  2. check_responses ──→ AgentCore Gateway ──→ Polling Lambda            │    │
│  │     ← {processing}                              ↓                       │    │
│  │     → speaks: "Still working on it..."      DynamoDB query              │    │
│  │                                                                         │    │
│  │  3. check_responses (repeat)                                            │    │
│  │     ← {results: ["Part 1...", "Part 2..."]}                             │    │
│  │     → speaks each response to caller                                    │    │
│  │                                                                         │    │
│  │  4. check_responses (repeat until complete)                             │    │
│  │     ← {complete}                                                        │    │
│  │     → speaks: "That's everything. Anything else?"                       │    │
│  │     → COMPLETE tool → exits flow                                        │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                         │                                                       │
│              ┌──────────┼──────────┐                                            │
│              ▼          ▼          ▼                                             │
│          COMPLETE   ESCALATION   Error                                          │
│           (end)    (transfer)   (retry)                                         │
└─────────────────────────────────────────────────────────────────────────────────┘

                    ┌──────────────────────────────────────────┐
                    │  Async Callback Path (runs in parallel)  │
                    │                                          │
                    │  Nexthink Agent (mock or Spark)          │
                    │      ↓ HTTPS POST (per response part)    │
                    │  API Gateway (/callback, API key in mock)│
                    │      ↓                                   │
                    │  Callback Lambda                         │
                    │      ↓ validate + write                  │
                    │  DynamoDB (AsyncResponseQueue)           │
                    │      SessionID / SequenceNumber          │
                    └──────────────────────────────────────────┘
```

The Orchestrator's behavior is driven by a custom prompt (`config/orchestrator_prompt.yaml`) that instructs it to submit, poll, deliver responses, vary hold messages, handle timeouts, and manage caller interruptions — all within the LLM reasoning loop. See [Architecture](docs/architecture.md) for Option A and deeper component details.

## Two Nexthink Backends

The outbound "Nexthink Agent" box above is pluggable. `NEXTHINK_BACKEND` in `.env` selects it:

| | `mock` (default) | `spark` |
|---|---|---|
| What it is | Bedrock-backed simulator deployed by this stack | Real Nexthink Spark **A2A** API |
| Needs Nexthink access | **No** — full end-to-end test in your own account | Yes — tenant ID, OAuth client credentials, API URLs |
| Outbound protocol | Simple JSON `{query, sessionId, callbackUrl}` | A2A `message:send` with OAuth2 bearer token |
| Callback auth | API key enforced | **API key disabled** (see [Known Limitations](docs/architecture.md#known-limitations)) |
| Where the code lives | `lambdas/mock_nexthink/handler.py` | `lambdas/submit/handler.py` (Spark section) + `lambdas/callback/handler.py` (`_translate_nexthink_payload`) |

Start with `mock` — it exercises every component (Orchestrator, MCP tools, DynamoDB, callbacks, multi-part delivery) with no external dependency. Switch to `spark` only when you have Nexthink tenant credentials. The inbound callback Lambda auto-detects both payload formats, so no switch is needed on that side. Protocol details in [Architecture — Nexthink Spark Backend](docs/architecture.md#nexthink-spark-a2a-backend).

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | System architecture, component interactions, sequence diagrams |
| [Data Model](docs/data-model.md) | DynamoDB schema, API contracts, MCP tool interfaces |
| [Networking](docs/networking.md) | VPC setup, NAT Gateway, IP whitelisting for Nexthink |
| [Deployment Guide](DEPLOYMENT.md) | Step-by-step CDK deployment, secrets, SSM config, manual console steps |
| [Contact Flow Guide](CONTACT_FLOW_BUILD_GUIDE.md) | Block-by-block instructions for building Option E and Option A flows |

## Quick Start

```bash
# Prerequisites: Python 3.12+, Node.js 18+, AWS CDK CLI, AWS credentials
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,infra]"

# Configure your environment
cp .env.example .env
# Edit .env with your Connect instance ARN and URL.
# Leave NEXTHINK_BACKEND=mock for a self-contained deployment.

# Deploy everything (including mock Nexthink for prototyping)
./scripts/deploy.sh

# Later, to switch to the real Nexthink Spark API:
#   fill in NEXTHINK_TENANT_ID / TOKEN_URL / SPARK_URL / USER_PRINCIPAL in .env, then
./scripts/deploy.sh --spark

# Sync the callback API key
python3 scripts/sync_callback_api_key.py

# Run tests
pytest tests/unit/ -v
```

After CDK deploy, several manual steps are required in the Connect console (MCP server registration, Lex bot creation, AI agent tool configuration). See [Deployment Guide](DEPLOYMENT.md) Step 6.

## Project Structure

```
├── lambdas/
│   ├── shared/                 # Lambda layer (models, config, session_manager, tts, metrics)
│   ├── callback/handler.py     # Receives Nexthink callbacks → DynamoDB
│   ├── polling/handler.py      # Polls DynamoDB for new responses
│   ├── submit/handler.py       # Creates session, POSTs to Nexthink (runs in VPC)
│   ├── disconnect/handler.py   # Marks session ABANDONED on caller hangup
│   ├── mock_nexthink/handler.py # Bedrock-backed mock Nexthink agent
│   └── custom_resources/       # MCP server registration Custom Resource
├── infrastructure/
│   ├── app.py                  # CDK app entry point
│   └── stack.py                # Full CDK stack (~1250 lines)
├── config/
│   ├── orchestrator_prompt.yaml       # Option E AI agent prompt (loaded by CDK)
│   ├── Main Flow -- Option E.json     # Actual deployed Option E flow (Connect export)
│   ├── option_e_flow.json             # Option E flow design spec (human-readable reference)
│   ├── option_a_flow.json             # Option A flow design spec (human-readable reference)
│   ├── lex_interrupt_bot.json         # Interrupt bot design spec (bot defined inline in CDK)
│   ├── schemas/                       # MCP tool schemas (loaded by CDK)
│   │   ├── submit_query.json
│   │   └── check_responses.json
│   └── flow_modules/                  # Workshop flow module exports (reference only)
│       ├── basic-setting-configurations_flow.json
│       └── customer-profile-lookup_flow.json
├── tests/unit/                  # 202 unit tests (pytest + moto)
├── scripts/
│   ├── lib/resolve.sh              # Shared shell helpers (auto-resolve instance, assistant, account)
│   ├── deploy.sh                   # CDK deploy wrapper (reads .env)
│   ├── configure_ai_agent.py       # Post-deploy: add MCP tools to AI Agent
│   ├── sync_callback_api_key.py    # Post-deploy: sync API key to Secrets Manager
│   └── ...                         # Debug, setup, and fix scripts
├── docs/                        # Architecture, data model, networking docs
├── .env.example                 # Template for deploy.sh configuration
├── DEPLOYMENT.md                # Step-by-step deployment guide
└── CONTACT_FLOW_BUILD_GUIDE.md  # Contact flow build instructions
```

## Key AWS Services

- Amazon Connect (IVR, contact flows, AI agents)
- Amazon Bedrock AgentCore Gateway (MCP tool server)
- Amazon Q in Connect / Wisdom (Orchestrator AI Agent)
- Amazon Nova Sonic (Speech-to-Speech voice)
- Amazon Lex V2 (speech input, caller interrupts)
- AWS Lambda (Python 3.12, 4 app functions + mock + custom resource)
- Amazon DynamoDB (response queue, session state)
- Amazon API Gateway (callback endpoint, mock Nexthink endpoint)
- AWS Secrets Manager, SSM Parameter Store, KMS, CloudWatch

## Configuration

All tunable parameters live in SSM Parameter Store under `/connect-async-multi-response/`. See [Data Model — Configuration](docs/data-model.md#configuration) for the full list.

Secrets (`NEXTHINK_API_KEY`, `CALLBACK_API_KEY`) are in AWS Secrets Manager.

## Tests

```bash
pytest tests/unit/ -v          # 207 unit tests, no AWS credentials needed
pytest tests/unit/ -v --cov    # With coverage
```

## Acknowledgements

The Option E architecture — Connect AI Agent Orchestrator invoking MCP tools through an Amazon Bedrock AgentCore Gateway — is based on the pattern from the [AWS Self-Service AI Agents Workshop](https://catalog.workshops.aws/self-service-ai-agents). The workshop's flow modules are included under `config/flow_modules/` — the basic-settings module is required by the Option E flow and must be imported before it (see [Deployment Guide](DEPLOYMENT.md) Step 7a).

This project extends that pattern with an asynchronous multi-response callback layer (API Gateway → Lambda → DynamoDB → polling tool) so a single caller question can be answered by several agent responses arriving over time, and adds a pluggable outbound backend (Bedrock mock / Nexthink Spark A2A).

## License

This code was developed by AWS as part of a free exploration engagement, governed by the
[Free Exploration Services section](https://aws.amazon.com/service-terms/#:~:text=110.%20Free-,Exploration,-Services)
(Section 110) of the [AWS Service Terms](https://aws.amazon.com/service-terms/).

Under Section 110.5, Developed Content is licensed as follows:

- **Software** (source code, sample code, scripts) — [Apache License, Version 2.0](LICENSE)
- **Documents** (documentation and diagrams, i.e. the Markdown files under `docs/` and in the
  repository root) — [Creative Commons Attribution 4.0 International (CC-BY 4.0)](https://creativecommons.org/licenses/by/4.0/)

See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party content provided under a separate
license remains governed by that license.
