# Data Model

## DynamoDB Schema

Table: `AsyncResponseQueue` — on-demand billing, KMS encryption, 24h TTL.

### Primary Key

| Key | Type | Description |
|-----|------|-------------|
| `SessionID` (PK) | String | UUID identifying the caller session |
| `SequenceNumber` (SK) | Number | 0 = sentinel, 1+ = response records |

### Sentinel Record (SequenceNumber = 0)

Created by the Submit Lambda when a query is submitted. Tracks session metadata.

| Attribute | Type | Description |
|-----------|------|-------------|
| SessionID | S | Partition key |
| SequenceNumber | N | Always `0` |
| SessionStatus | S | `ACTIVE` / `COMPLETE` / `ABANDONED` / `TIMED_OUT` |
| QueryText | S | Original caller question |
| CreatedAt | N | Epoch milliseconds |
| LastResponseAt | N | Updated on each callback |
| ResponseCount | N | Incremented on each callback |
| TTL | N | Epoch seconds (CreatedAt + 24h) |

### Response Records (SequenceNumber >= 1)

Written by the Callback Lambda when Nexthink posts a response.

| Attribute | Type | Description |
|-----------|------|-------------|
| SessionID | S | Partition key |
| SequenceNumber | N | 1-based, monotonically increasing |
| ResponseText | S | AI agent response content |
| IsComplete | BOOL | `true` on the final response |
| Delivered | BOOL | `true` after read by Polling Lambda |
| CreatedAt | N | Epoch milliseconds |
| TTL | N | Epoch seconds (CreatedAt + 24h) |

### Global Secondary Index

`SessionStatusIndex` — for operational monitoring.

| Key | Type |
|-----|------|
| SessionStatus (PK) | String |
| CreatedAt (SK) | Number |

Projection: ALL.

## API Contracts

### Callback Endpoint (Nexthink → API Gateway)

`POST /callback` with `x-api-key` header.

```json
{
  "sessionId": "uuid-string",
  "sequenceNumber": 1,
  "responseText": "Response content here.",
  "isComplete": false,
  "metadata": {
    "sourceAgent": "nexthink-agent-v1",
    "confidence": 0.95
  }
}
```

| Status | Meaning |
|--------|---------|
| 200 | Stored (or discarded for abandoned session) |
| 400 | Missing/invalid fields |
| 404 | Unknown session |
| 401 | Invalid API key |

### MCP Tool: submit_query

Input (via AgentCore Gateway):

```json
{
  "transcript": "My laptop keeps freezing when I open Outlook.",
  "sessionId": "abc-123",
  "callbackUrl": "https://xxx.execute-api.region.amazonaws.com/prod/callback"
}
```

Output:

```json
{
  "status": "submitted",
  "sessionId": "abc-123",
  "message": null
}
```

### MCP Tool: check_responses

Input:

```json
{
  "sessionId": "abc-123",
  "cursor": 0
}
```

Output:

```json
{
  "status": "results",
  "responses": [
    {"sequenceNumber": 1, "text": "First response..."},
    {"sequenceNumber": 2, "text": "Second response..."}
  ],
  "cursor": 2,
  "elapsedSeconds": 12.5,
  "message": null,
  "initialTimeout": false,
  "sessionTimeout": false
}
```

Status values: `processing` (no new responses), `results` (new responses available), `complete` (session done), `error`, `retry`.

### Polling Lambda Return (Option A — Contact Flow)

Contact flow Lambda integrations return flat string key-value maps:

```json
{
  "status": "results",
  "responseText": "<speak>Response in SSML format<break time='300ms'/>with pauses.</speak>",
  "cursor": "2",
  "elapsedSeconds": "12",
  "holdMessage": "Still working on that for you.",
  "isComplete": "false"
}
```

Note: Option A maps `processing` → `waiting` for contact flow branching.

### Lambda Event Formats

Both Submit and Polling Lambdas detect the caller format automatically:

| Format | Detection | Source |
|--------|-----------|--------|
| Option E (MCP) | `"Details" not in event` | AgentCore Gateway (flat map) |
| Option A (Contact Flow) | `"Details" in event` | Connect Lambda integration (nested) |

Option A events arrive as:
```json
{
  "Details": {
    "Parameters": {
      "transcript": "...",
      "sessionId": "...",
      "callbackUrl": "..."
    }
  }
}
```

## Configuration

SSM Parameter Store prefix: `/connect-async-multi-response/`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `polling-interval-ms` | `3000` | Delay between poll cycles (Option A) |
| `max-session-duration-s` | `300` | Max session duration before timeout |
| `initial-response-timeout-s` | `30` | Timeout for first Nexthink response |
| `hold-message-pool` | 3 messages (JSON) | Hold messages rotated during polling |
| `max-response-length` | `3000` | Max chars before TTS splitting |
| `max-poll-iterations` | `10` | Consecutive "processing" polls before warning |
| `silence-timeout-s` | `4` | Lex silence detection timeout |
| `polly-voice-id` | `Matthew` | Polly voice (Option A) |
| `nova-sonic-voice` | `Matthew` | Nova Sonic voice (Option E) |
| `nexthink-agent-url` | placeholder / mock URL | Nexthink endpoint (mock backend) |
| `nexthink-backend` | `mock` | `mock` or `spark` — selects the outbound adapter in the Submit Lambda |
| `nexthink-tenant-id` | *(spark only)* | Spark tenant UUID; from `.env` `NEXTHINK_TENANT_ID` |
| `nexthink-token-url` | *(spark only)* | OAuth2 token endpoint; from `.env` `NEXTHINK_TOKEN_URL` |
| `nexthink-spark-url` | *(spark only)* | A2A `message:send` endpoint; from `.env` `NEXTHINK_SPARK_URL` |
| `nexthink-user-principal` | *(spark only)* | `User-Principal-Name` header / message metadata; from `.env` `NEXTHINK_USER_PRINCIPAL` |
| `lex-bot-alias` | auto-wired | Interrupt bot alias ARN |
| `question-capture-bot-alias` | auto-wired | Question capture bot alias ARN |
| `wisdom-assistant-arn` | auto-wired | Q in Connect assistant ARN |
| `agentcore-gateway-arn` | auto-wired | AgentCore Gateway ARN |

Priority: environment variable > SSM parameter > dataclass default.

Secrets (AWS Secrets Manager):
- `connect-async/NEXTHINK_API_KEY` — API key for Nexthink agent
- `connect-async/CALLBACK_API_KEY` — API key for callback endpoint auth


---

## See Also

- [Architecture](architecture.md) — System overview, component interactions, sequence diagrams
- [Deployment Guide](../DEPLOYMENT.md) — SSM parameter setup, secrets configuration
