# Architecture

## Overview

The system bridges Amazon Connect's synchronous voice model with Nexthink's asynchronous multi-response AI agent. A single caller question triggers multiple responses delivered over time, each spoken individually while the call stays active.

Two architecture options share the same callback ingestion backend:

```
                    ┌─────────────────────────────────────────────┐
                    │           Shared Backend                     │
                    │                                              │
  Nexthink Agent ──→ API Gateway ──→ Callback Lambda ──→ DynamoDB │
                    │                                        ↑     │
                    │  Polling Lambda ────────────────────────┘     │
                    └─────────────────────────────────────────────┘
                              ↑
              ┌───────────────┴───────────────┐
              │                               │
      Option E (Primary)             Option A (Fallback)
      Orchestrator AI Agent          Contact Flow Loop
      + MCP Tools                    + Lex Interrupt Bot
```

## Option E: Orchestrator AI Agent + MCP Tools

The Connect AI Agent Orchestrator handles the entire conversation loop via LLM reasoning. It invokes MCP tools through an AgentCore Gateway, speaks responses via Nova Sonic, and manages caller interruptions natively.

```
Caller speaks
    ↓
Amazon Connect Contact Flow (8 blocks)
    ↓
Set Recording → Set Voice (Nova Sonic) → Connect Assistant → Get Customer Input (Lex + QinConnectIntent)
    ↓
Orchestrator AI Agent takes over
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  LLM Reasoning Loop                                              │
│                                                                  │
│  1. submit_query(transcript, sessionId, callbackUrl)             │
│     → AgentCore Gateway → Submit Lambda → Nexthink (HTTP POST)  │
│     ← {status: "submitted"}                                     │
│     → <message>I'm looking into that for you...</message>        │
│                                                                  │
│  2. check_responses(sessionId, cursor=0)                         │
│     → AgentCore Gateway → Polling Lambda → DynamoDB query        │
│     ← {status: "processing"} or {status: "results", responses}  │
│     → <message>Hold message</message> or <message>Response</message>│
│                                                                  │
│  3. Repeat step 2 until status="complete"                        │
│     → <message>That's everything. Anything else?</message>       │
│                                                                  │
│  4. COMPLETE tool → exits GCI block → End Call                   │
│     or ESCALATION tool → Transfer to Queue                       │
└──────────────────────────────────────────────────────────────────┘
```

### AgentCore Gateway

The Gateway acts as the MCP server connecting the Orchestrator to the Lambda functions:

```
Connect Orchestrator
    ↓ (MCP tool invocation)
AgentCore Gateway (ConnectAsyncMcpGateway)
    ├── submit-query target → Submit Lambda (lambda:InvokeFunction)
    └── check-responses target → Polling Lambda (lambda:InvokeFunction)
```

- Inbound auth: Connect instance OIDC (JWT validation)
- Outbound: IAM-based Lambda invocation
- Tool schemas: `config/schemas/submit_query.json`, `config/schemas/check_responses.json`
- The Gateway invokes Lambdas via the Lambda API, not HTTP — VPC placement of the Lambda is transparent

### MCP Server Registration

Q in Connect requires MCP servers to be registered through the Connect admin console. The CDK creates the infrastructure (Gateway, targets, AppIntegrations Application), but the internal tool catalog is only populated via the console UI. This is a known platform limitation.

## Option A: Lambda Polling Loop + DynamoDB

A contact flow loop invokes the Polling Lambda every ~8 seconds, checks DynamoDB for new responses, speaks them via Polly/SSML, and uses a Lex interrupt bot for caller control.

```
Caller speaks
    ↓
Contact Flow (19 blocks)
    ↓
Welcome → Get Question (Lex) → Submit Lambda → Acknowledgment
    ↓
┌─────────── Polling Loop ──────────────────────────────────┐
│                                                            │
│  Polling Lambda → DynamoDB query                           │
│      ↓                                                     │
│  ┌── results → Play Response (SSML) → loop back ──┐       │
│  ├── waiting → Play Hold Message                   │       │
│  │               ↓                                 │       │
│  │           Lex Interrupt Bot (4s silence)         │       │
│  │           ├── FallbackIntent → loop back ───────┘       │
│  │           ├── CancelQuery → Disconnect → Ask Another    │
│  │           ├── AskNewQuestion → back to Get Question     │
│  │           └── RepeatLastResponse → Play Last → loop     │
│  ├── complete → Completion Message → Ask Another           │
│  └── error → Retry or Escalate                             │
└────────────────────────────────────────────────────────────┘
```

### Lex Bots (CDK-created)

- `AsyncResponseInterruptBot`: Detects cancel, new question, repeat during hold (Option A)
- `NexThinkQuestionCapture`: Captures free-form caller question via `AMAZON.FreeFormInput` slot (Option A)

A third Lex bot with `AMAZON.QinConnectIntent` must be created via the Connect console for Option E.

## Callback Ingestion (Shared)

Both options share the same path for receiving Nexthink responses:

```
Nexthink Agent
    ↓ (HTTPS POST with API key)
API Gateway (ConnectAsyncCallbackApi)
    ↓ (request validation, throttling 100 req/s)
Callback Lambda
    ↓ (validate payload, check session exists)
DynamoDB (AsyncResponseQueue)
    ├── PutItem: response record (SessionID, SequenceNumber, ResponseText)
    ├── UpdateItem: sentinel record (ResponseCount++, LastResponseAt)
    └── If isComplete=true: mark session COMPLETE
```

- API key authentication (`x-api-key` header) — **mock backend only**; disabled in spark mode, see [Known Limitations](#known-limitations)
- Request body validation against JSON schema (`oneOf` native shape / Spark `statusUpdate` shape)
- Idempotent writes via conditional expressions
- Callbacks for abandoned/timed-out sessions are accepted (200) but discarded

## Mock Nexthink Agent

For prototyping without the real Nexthink endpoint. Deployed conditionally via `-c deploy_mock_nexthink=true`.

```
Submit Lambda → HTTP POST → Mock API Gateway (/agent) → Mock Lambda
                                                            ↓
                                                    Phase 1: Return 202 immediately
                                                    Phase 2: Self-invoke async
                                                            ↓
                                                    Bedrock (Claude Haiku)
                                                    → Generate 2-4 response parts
                                                            ↓
                                                    POST each part to callback URL
                                                    (with configurable delay between parts)
```

## Nexthink Spark (A2A) Backend

Selected with `NEXTHINK_BACKEND=spark`. Replaces the mock's simple JSON POST with the Nexthink Spark **Agent-to-Agent (A2A)** protocol. Everything downstream (DynamoDB, polling, Orchestrator) is unchanged — the two protocols never touch each other; DynamoDB is the seam between them.

### Where the protocol code lives

Exactly two places, both small and self-contained:

| Direction | File | Function |
|---|---|---|
| Outbound (Connect → Spark) | `lambdas/submit/handler.py` | `_send_to_spark()` and `_build_spark_payload()` |
| Inbound (Spark → Connect) | `lambdas/callback/handler.py` | `_translate_nexthink_payload()` |

Nothing else in the codebase knows A2A exists. The MCP side (tool schemas, AgentCore Gateway, Orchestrator prompt) is backend-agnostic.

### Outbound: `submit_query` in spark mode

```
Submit Lambda
    │  1. POST NEXTHINK_TOKEN_URL
    │     grant_type=client_credentials&scope=service:integration
    │     Authorization: Basic <NEXTHINK_API_KEY>        ← base64(client_id:client_secret)
    │  ← {access_token}
    │
    │  2. POST NEXTHINK_SPARK_URL  (…/api/spark/a2a/v1/message:send)
    │     Authorization: Bearer <access_token>
    ↓     User-Principal-Name: <NEXTHINK_USER_PRINCIPAL>
Nexthink Spark
```

Request envelope (built by `_build_spark_payload`):

```json
{
  "tenant": "<NEXTHINK_TENANT_ID>",
  "message": {
    "messageId": "<uuid4>",
    "contextId": "<sessionId>",        ← THE correlation key
    "taskId": "<uuid4>",
    "role": "ROLE_USER",
    "content": [{"text": "<caller transcript>"}],
    "metadata": {"UserPrincipalName": "<NEXTHINK_USER_PRINCIPAL>"}
  },
  "configuration": {
    "acceptedOutputModes": ["text"],
    "pushNotification": {
      "id": "<sessionId>",
      "url": "<our callback URL>",     ← where Spark will POST responses
      "token": "<CALLBACK_API_KEY>"    ← NOT replayed as x-api-key (see limitations)
    },
    "historyLength": 1,
    "blocking": false                  ← async; responses arrive via pushNotification
  }
}
```

`message.contextId` **must be a UUID** — Spark rejects anything else — and it is what Spark echoes back on every callback. The Submit Lambda therefore coerces non-UUID session IDs before creating the DynamoDB session (see `_coerce_to_uuid`), so the session key and the callback key are guaranteed to match.

### Inbound: what Spark sends, and how it's mapped

Spark POSTs one `statusUpdate` per response part to `pushNotification.url`:

```json
{
  "statusUpdate": {
    "taskId": "…",
    "contextId": "<sessionId>",
    "status": {
      "state": "TASK_STATE_WORKING",                  ← or TASK_STATE_COMPLETED etc.
      "message": {"content": [{"text": "…"}, …]}
    }
  }
}
```

`_translate_nexthink_payload()` converts this to the native shape and hands it to the unchanged pipeline:

| Native field | Derived from |
|---|---|
| `sessionId` | `statusUpdate.contextId` |
| `responseText` | all `status.message.content[].text` joined with a space |
| `isComplete` | `status.state != "TASK_STATE_WORKING"` |
| `sequenceNumber` | **synthesized** — Spark has no sequence number. Per-session counter in the Lambda (see limitations). |

Detection is by shape: `if "statusUpdate" in body`. Native-format callers (the mock) skip translation entirely, so both backends can hit the same endpoint concurrently.

### Why there is no A2A↔MCP bridge

The mismatch is temporal, not syntactic. MCP is synchronous (one request, one response). A2A here is asynchronous fan-out (one request, N pushes over minutes). No MCP return value can express "four answers later". DynamoDB absorbs the fan-out into rows; `check_responses` drains rows. `check_responses` never touches A2A — Spark could be down and polling still works.

## Known Limitations

The Spark integration is shipped **as it was deployed and verified end-to-end in the prototype environment**, with one correctness fix applied (see "Fixed since deployment" below). One limitation remains open and is marked in the source with a `SECURITY NOTE` comment.

### 1. Callback endpoint is unauthenticated in spark mode

`infrastructure/stack.py` sets `api_key_required = (backend != "spark")`. Spark receives our API key in `pushNotification.token` but does not send it back as an `x-api-key` header, so API Gateway key auth would 403 every callback. Consequence: in spark mode, anyone with the callback URL and a live `sessionId` can inject text that gets spoken to a caller.

Before production, replace with one of:
- A Lambda authorizer that validates however Spark actually transmits the token (confirm header/body placement with Nexthink).
- An API Gateway resource policy restricting `/callback` to Nexthink's egress IP ranges.
- A per-session shared secret embedded in the callback URL path and checked by the Lambda against the session sentinel.

Mock mode is unaffected — the mock sends `x-api-key` and the key is enforced.

### Fixed since deployment: sequence counter moved to DynamoDB

The deployed prototype synthesized Spark sequence numbers with a module-level Python dict. That dict was per-container and reset on cold start, so with more than one concurrent caller (or one long session spanning a container recycle) two callbacks for the same session could both be assigned `sequenceNumber=1` — and the second was silently discarded as a "duplicate" by the conditional `PutItem`. The caller would hear part 1 and part 3, never part 2, with nothing in the logs.

This share replaces it with `_next_spark_seq()`: an atomic `UpdateItem … ADD NextSeq :one` on the session's sentinel record, `ReturnValues=UPDATED_NEW`. DynamoDB performs the increment atomically, so every container sees a consistent, monotonically increasing sequence. The condition `attribute_exists(SessionID)` stops an unknown `contextId` from creating a phantom sentinel (→ 404 instead). Cost: one extra DynamoDB write per Spark callback.

One consequence worth knowing: because every Spark callback now gets a fresh sequence number, the `PutItem` duplicate check never fires for Spark. If Spark ever *retries* a push notification (it would only do so on a non-2xx from us, which is rare), the text would be stored twice. If that becomes observable, dedupe on a content hash stored on the sentinel.

### Design note: deterministic UUID coercion is intentional

`lambdas/submit/handler.py` → `_coerce_to_uuid` uses `uuid5(NAMESPACE_URL, session_id)` when the caller supplies a non-UUID `sessionId` (Spark requires a UUID `contextId`). This is **deterministic on purpose**: if the Orchestrator retries `submit_query` with the same `sessionId` after a transient failure where the first call actually succeeded, the coerced UUID is identical, `create_session`'s conditional write fails, and no second Spark task is created. Switching to `uuid4()` would make every retry a fresh session and a fresh Spark submission — a real double-submit bug.

The design relies on callers polling with the `sessionId` **returned** by `submit_query`, not the one they sent; the Orchestrator prompt's few-shot examples and the `check_responses` schema (`"The session identifier from submit_query"`) both enforce this. Possible refinement: on a `ConditionalCheckFailedException` in spark mode, return `status="submitted"` with the existing session ID instead of an error, so a retry looks like success to the LLM.

### Also worth knowing

- `config/schemas/submit_query.json` declares `callbackUrl` **required**, but the Submit Lambda unconditionally overwrites it with the `CALLBACK_API_URL` env var. The Orchestrator is asked to supply a value that is always discarded.
- `_get_nexthink_token` requests a fresh OAuth token on every submit (no caching). Fine at IVR call volumes.

## Session Lifecycle

```
                create_session()
                     ↓
                  ┌──────┐
                  │ACTIVE│
                  └──┬───┘
                     │
        ┌────────────┼────────────────┐
        ↓            ↓                ↓
  isComplete=true  caller hangs up  timeout exceeded
        ↓            ↓                ↓
   ┌────────┐  ┌─────────┐  ┌──────────┐
   │COMPLETE│  │ABANDONED│  │TIMED_OUT │
   └────────┘  └─────────┘  └──────────┘
```

All transitions are conditional (only from ACTIVE) using DynamoDB conditional expressions. Records auto-expire via TTL after 24 hours.

## Infrastructure (CDK)

Single stack (`ConnectAsyncMultiResponseStack`) with conditional resources:

| Resource Group | Always | With `connect_instance_arn` | With `deploy_orchestrator` | With `deploy_mock_nexthink` |
|---|---|---|---|---|
| DynamoDB + KMS | ✓ | | | |
| 4 App Lambdas + Layer | ✓ | | | |
| Callback API Gateway | ✓ | | | |
| SSM Parameters + Secrets | ✓ | | | |
| Lex V2 Bots (2) | ✓ | | | |
| CloudWatch Log Groups | ✓ | | | |
| VPC + NAT Gateway | ✓ | | | |
| Connect Associations | | ✓ | | |
| Wisdom Assistant + AI Prompt | | | ✓ | |
| AgentCore Gateway + Targets | | | ✓ | |
| MCP Registration (Custom Resource) | | | ✓ | |
| Mock Nexthink Lambda + API GW | | | | ✓ |

See [Deployment Guide](../DEPLOYMENT.md) for the full resource inventory and deploy commands.


---

## See Also

- [Data Model](data-model.md) — DynamoDB schema, API contracts, MCP tool interfaces
- [Networking](networking.md) — VPC setup, NAT Gateway, IP whitelisting
- [Deployment Guide](../DEPLOYMENT.md) — Step-by-step deployment and manual console steps
- [Contact Flow Guide](../CONTACT_FLOW_BUILD_GUIDE.md) — Block-by-block flow build instructions
