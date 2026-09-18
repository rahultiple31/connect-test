# Contact Flow Build Guide

Step-by-step instructions for building the Option E and Option A contact flows in the Amazon Connect flow editor. Follow these exactly.

## Prerequisites

Before starting, confirm you have:

- [ ] CDK stack deployed with `connect_instance_arn` (Lambdas, Lex bots, API Gateway all created)
- [ ] The following ARNs/values ready (from CDK outputs or AWS console):

| Value | Where to find it |
|-------|-----------------|
| Submit Lambda ARN | Lambda console → `ConnectAsync-Submit` → copy ARN |
| Polling Lambda ARN | Lambda console → `ConnectAsync-Polling` → copy ARN |
| Disconnect Lambda ARN | Lambda console → `ConnectAsync-Disconnect` → copy ARN |
| Interrupt Bot name | `AsyncResponseInterruptBot` (CDK-created) |
| Interrupt Bot alias | `live` (CDK-created) |
| Question Capture Bot name | `NexThinkQuestionCapture` (CDK-created) |
| Question Capture Bot alias | `live` (CDK-created) |
| Callback API URL | API Gateway console → `ConnectAsyncCallbackApi` → Stages → `prod` → Invoke URL |
| Wisdom Assistant ARN | SSM: `/connect-async-multi-response/wisdom-assistant-arn` (if Option E deployed) |
| Option E Lex Bot | **Must be created via Connect admin console** — see [DEPLOYMENT.md Step 6e](DEPLOYMENT.md#step-6e-create-the-lex-bot-with-qinconnectintent-connect-console-only). NOT the CDK-created bots. |
| Escalation Queue ARN | Your existing Connect queue for human agent transfer |

- [ ] All three Lambdas are visible in Connect console under "AWS Lambda" (if you deployed with `connect_instance_arn`, this is automatic; otherwise add them manually: Connect console → Contact flows → AWS Lambda → Add Lambda Function)
- [ ] Both Lex bots are visible in Connect console under "Amazon Lex" (automatic if deployed with `connect_instance_arn`)
- [ ] Contact Lens is enabled on your Connect instance (Connect console → Analytics tools → Contact Lens → Enable)

---

## Option E: Orchestrator AI Agent Flow (8 blocks)

This is the simpler flow. The Orchestrator AI Agent handles all conversation logic internally.

### Block 1: Set Recording & Analytics

1. Open the Connect console → Contact flows → Create contact flow
2. Name the flow: `Nexthink-Async-MultiResponse-OptionE`
3. Drag a **Set recording and analytics behavior** block onto the canvas
4. Click the block to open its settings
5. Under **Contact Lens real-time analytics**: toggle ON
6. Under **Contact Lens post-call analytics**: toggle ON
7. Under **Call recording**: set both Agent and Customer to ON
8. Click **Save**
9. Connect the **Entry point** to this block

> This block MUST be first. Without Contact Lens real-time enabled, the AI agent will not function on voice calls.

### Block 2: Set Voice

Nova Sonic requires configuration at TWO levels: the Lex bot level (Speech-to-Speech model) AND the contact flow level (Set Voice block). Both must be done.

**Part A: Configure Nova Sonic on the Lex Bot (do this ONCE, before building the flow)**

1. Sign in to the Amazon Connect admin website
2. Go to **Bots** → select the bot that will be used with the Orchestrator (the one with `AMAZON.QinConnectIntent`)
3. Click the **Configuration** tab
4. Select the locale you want to configure (e.g., `en_US`)
5. In the **Speech model** section, click **Edit**
6. In the Speech model modal, open the **Model type** dropdown and select **Speech-to-Speech**
7. Open **Voice provider** and select **Amazon Nova Sonic**
8. Click **Confirm**
9. The Speech model card should now show "Speech-to-Speech: Amazon Nova Sonic" with a warning to select a compatible voice in your Set voice block
10. If the locale shows **Unbuilt changes**, click **Build language** and wait for the build to complete

> Nova Sonic is only available in `us-east-1` and `us-west-2`. If your instance is in another region, skip this and use the Neural engine with Matthew voice as fallback.

**Part B: Configure the Set Voice block in the flow**

1. Drag a **Set voice** block onto the canvas
2. Click to open settings
3. Set **Voice provider**: Amazon
4. Set **Language**: English, US (en-US)
5. Set **Voice**: Matthew
6. Under **Other settings**, check **Override speaking style**
7. Set the speaking style to **Generative**
8. The block should now display "Voice: Matthew (Generative)"
9. Click **Save**
10. Connect Block 1 → Block 2

The supported Nova Sonic voices at launch are:

| Voice | Language | Gender |
|-------|----------|--------|
| Matthew | en-US | Masculine |
| Amy | en-GB | Feminine |
| Olivia | en-AU | Feminine |
| Lupe | es-US | Feminine |

If you need a voice not in this list (e.g., a Nova Sonic beta voice), you can override it via a session attribute in the Get Customer Input block:
- Key: `x-amz-lex:audio:speaker-model-voice-override`
- Value: the exact voice ID (case-sensitive, e.g., `tiffany`)

> When using Nova Sonic, any Play Prompt blocks in the same flow that use Amazon Polly will sound different (Polly doesn't use Nova Sonic). For Option E this is fine since the Orchestrator speaks via Nova Sonic directly. For Option A, the Play Prompt blocks (welcome, hold messages, responses) will use Polly — only the Orchestrator's speech uses Nova Sonic.

### Block 3: Connect Assistant

1. Drag a **Amazon Q in Connect** block (also labeled "Connect Assistant" in some UI versions) onto the canvas
2. Click to open settings
3. Under **Assistant**: select the Wisdom assistant domain created by CDK (name: `NexthinkAsyncMultiResponse`)
4. If the assistant doesn't appear in the dropdown, verify it was created: check SSM parameter `/connect-async-multi-response/wisdom-assistant-arn`
5. Click **Save**
6. Connect Block 2 → Block 3

### Block 4: Get Customer Input

1. Drag a **Get customer input** block onto the canvas
2. Click to open settings
3. Set **Input type**: Speech
4. Under **Amazon Lex**:
   - Select the Lex bot that has `AMAZON.QinConnectIntent` enabled
   - **This bot MUST have been created through the Connect admin website** (Routing → Bots → Create Conversational AI bot). See [DEPLOYMENT.md Step 6e](DEPLOYMENT.md#step-6e-create-the-lex-bot-with-qinconnectintent-connect-console-only) for the full procedure.
   - Bots created via the Lex API, CLI, or CloudFormation will NOT work — Connect returns `BadRequestException` at runtime.
   - It is NOT the `AsyncResponseInterruptBot` or `NexThinkQuestionCapture` — those are CDK-created Option A bots.
   - Select the appropriate alias (typically `TestBotAlias` for Connect-created bots)
5. Under **Intents**: ensure `AMAZON.QinConnectIntent` is listed
6. Click **Save**
7. Connect Block 3 → Block 4

> This is where the Orchestrator takes over. The caller speaks, the Orchestrator reasons, invokes MCP tools, and speaks responses back. The block only exits when the Orchestrator selects a terminal tool (COMPLETE, ESCALATION) or an error/timeout occurs.

### Block 5: Check Contact Attribute

1. Drag a **Check contact attributes** block onto the canvas
2. Click to open settings
3. Set **Attribute to check**: type = `Lex`, key = `toolName`
4. Add condition: **Equals** `COMPLETE` → will route to End Call (Block 8)
5. Add condition: **Equals** `ESCALATION` → will route to Transfer (Block 7)
6. Set **Default (No match)** → will route to Error Handler (Block 6)
7. Click **Save**

### Block 6: Error Handler (Play Prompt)

1. Drag a **Play prompt** block onto the canvas
2. Click to open settings
3. Set **Text to speech**: `I'm sorry, we encountered an issue processing your request. Let me try again.`
4. Set **Interpret as**: Text
5. Click **Save**
6. Connect this block's output → back to Block 4 (Get Customer Input) for retry

> For production: add a Set Contact Attributes block before this to track retry count, and branch to End Call after 2 retries to avoid infinite loops.

### Block 7: Transfer to Queue

1. Drag a **Transfer to queue** block onto the canvas
2. Click to open settings
3. Under **Queue**: select your escalation queue
4. Click **Save**
5. Connect the **Error** and **At capacity** outputs → Block 8 (End Call)

### Block 8: End Call

1. Drag a **Disconnect / hang up** block onto the canvas
2. No configuration needed
3. Connect:
   - Block 5 "COMPLETE" condition → Block 8
   - Block 5 "ESCALATION" condition → Block 7
   - Block 5 "No match" → Block 6
   - Block 6 output → Block 4 (retry loop)
   - Block 7 error/at capacity → Block 8

### Final wiring summary (Option E)

```
Entry → [1: Set Recording] → [2: Set Voice] → [3: Connect Assistant] → [4: Get Customer Input]
                                                                              ↓
                                                                    [5: Check Attribute]
                                                                    ├── COMPLETE → [8: End Call]
                                                                    ├── ESCALATION → [7: Transfer] → [8: End Call]
                                                                    └── No match → [6: Error] → back to [4]
```

### Save and Publish

1. Click **Save** on the flow
2. Click **Publish**
3. Note the Contact Flow ID (visible in the URL or flow details)

---

## Option A: Lambda Polling Loop Flow (19 blocks)

This is the more complex flow with an explicit polling loop. Build it in sections.

### Section 1: Entry and Question Capture (Blocks 1-6)

#### Block 1: Set Recording & Analytics

1. Create a new contact flow: `Nexthink-Async-MultiResponse-OptionA`
2. Drag a **Set recording and analytics behavior** block
3. Enable Contact Lens real-time analytics, post-call analytics, and call recording (Agent + Customer)
4. Click **Save**
5. Connect **Entry point** → Block 1

#### Block 2: Play Welcome

1. Drag a **Play prompt** block
2. Set text: `Welcome to Nexthink IT support. Please describe your question or issue, and I'll look into it for you.`
3. Set **Interpret as**: Text
4. Click **Save**
5. Connect Block 1 → Block 2

#### Block 3: Get Customer Input (Question Capture)

1. Drag a **Get customer input** block
2. Set **Input type**: Speech
3. Under **Amazon Lex**: select bot `NexThinkQuestionCapture`, alias `live`
4. Under **Intents**: `CaptureQuestion` should appear automatically
5. Click **Save**
6. Connect Block 2 → Block 3
7. Leave the **Error** and **Timeout** outputs unconnected for now (will connect to Block 14 later)

#### Block 4: Invoke Submit Lambda

1. Drag an **Invoke AWS Lambda function** block
2. Select function: `ConnectAsync-Submit`
3. Under **Function input parameters**, add these key-value pairs:
   - Key: `transcript`, Value: set to **Use attribute**, Type: `Lex`, Attribute: `question` (this is the slot from the CaptureQuestion intent)
   - Key: `sessionId`, Value: set to **Use text**, leave empty (the Lambda generates its own)
   - Key: `callbackUrl`, Value: set to **Use text**, enter your API Gateway callback URL: `https://XXXXXXXX.execute-api.REGION.amazonaws.com/prod/callback`
4. Click **Save**
5. Connect Block 3 "CaptureQuestion" intent → Block 4
6. Leave **Error** output unconnected for now (will connect to Block 14)

> The Submit Lambda generates a `sessionId` internally and returns it as a contact attribute. The `callbackUrl` must be the full API Gateway URL including `/prod/callback`.

#### Block 5: Check Submission Status

1. Drag a **Check contact attributes** block
2. Set **Attribute to check**: Type = `External`, Attribute = `status`
3. Add condition: **Equals** `submitted`
4. Click **Save**
5. Connect Block 4 "Success" → Block 5
6. Connect Block 5 "submitted" → Block 6 (next)
7. Connect Block 5 "No match" (default) → Block 14 (fatal error, will create later)

#### Block 6: Play Acknowledgment

1. Drag a **Play prompt** block
2. Set text: `I'm looking into that for you. Please hold while I get your answer.`
3. Set **Interpret as**: Text
4. Click **Save**
5. Connect Block 5 "submitted" → Block 6

### Section 2: Polling Loop Core (Blocks 7-10)

#### Block 7: Invoke Polling Lambda (LOOP ENTRY POINT)

1. Drag an **Invoke AWS Lambda function** block
2. Select function: `ConnectAsync-Polling`
3. Under **Function input parameters**, add:
   - Key: `sessionId`, Value: **Use attribute**, Type: `User Defined`, Attribute: `sessionId`
   - Key: `cursor`, Value: **Use attribute**, Type: `User Defined`, Attribute: `cursor`
4. Click **Save**
5. Connect Block 6 → Block 7

> This block is the entry point of the polling loop. Multiple downstream blocks will route back here. Position it centrally on the canvas for clarity.

#### Block 8: Check Poll Status (MAIN BRANCH POINT)

1. Drag a **Check contact attributes** block
2. Set **Attribute to check**: Type = `External`, Attribute = `status`
3. Add these conditions in order:
   - **Equals** `results` → will route to Block 9
   - **Equals** `waiting` → will route to Block 10
   - **Equals** `complete` → will route to Block 13
   - **Equals** `retry` → will route to Block 12
4. Set **Default (No match)** → will route to Block 12
5. Click **Save**
6. Connect Block 7 "Success" → Block 8
7. Connect Block 7 "Error" → Block 12 (error handler, will create later)

#### Block 9: Play Response

1. Drag a **Play prompt** block
2. Set **Text to speech**: select **Use attribute**, Type = `External`, Attribute = `responseText`
3. Set **Interpret as**: SSML (the Polling Lambda returns pre-formatted SSML)
4. Click **Save**
5. Connect Block 8 "results" → Block 9
6. Connect Block 9 output → **back to Block 7** (this creates the polling loop)

> After playing the response, we loop back to poll for more. The cursor was already updated by the Lambda.

#### Block 9a: Set Contact Attributes (after response)

Insert a **Set contact attributes** block between Block 9 and the loop-back to Block 7:

1. Drag a **Set contact attributes** block
2. Add attribute: Key = `last_response_text`, Value = **Use attribute**, Type = `External`, Attribute = `responseText`
3. Add attribute: Key = `error_count`, Value = **Use text**, enter `0`
4. Click **Save**
5. Rewire: Block 9 → Block 9a → Block 7

#### Block 10: Play Hold Message

1. Drag a **Play prompt** block
2. Set **Text to speech**: select **Use attribute**, Type = `External`, Attribute = `holdMessage`
3. Set **Interpret as**: Text
4. Click **Save**
5. Connect Block 8 "waiting" → Block 10

### Section 3: Lex Interrupt Bot (Block 11 + sub-blocks)

#### Block 11: Get Customer Input (Interrupt Bot)

1. Drag a **Get customer input** block
2. Set **Input type**: Speech
3. Under **Amazon Lex**: select bot `AsyncResponseInterruptBot`, alias `live`
4. Under **Session attributes**, add:
   - Key: `x-amz-lex:audio:start-timeout-ms`, Value: `4000`
5. Under **Intents**: you should see `CancelQuery`, `AskNewQuestion`, `RepeatLastResponse`, `FallbackIntent`
6. Click **Save**
7. Connect Block 10 → Block 11
8. Wire the intent outputs:
   - `CancelQuery` → Block 11a
   - `AskNewQuestion` → Block 11b
   - `RepeatLastResponse` → Block 11c
   - `FallbackIntent` → **Block 7** (back to polling loop)
   - `Error` → **Block 7** (back to polling loop)
   - `Timeout` → **Block 7** (back to polling loop)

> The 4-second silence timeout means: if the caller says nothing for 4 seconds, Lex returns FallbackIntent and the flow continues polling. This is the "wait" period between poll cycles.

#### Block 11a: Handle Cancel

Two sub-blocks:

1. Drag an **Invoke AWS Lambda function** block
   - Select: `ConnectAsync-Disconnect`
   - Input parameters: Key = `sessionId`, Value = **Use attribute**, Type = `User Defined`, Attribute = `sessionId`
   - Connect Block 11 "CancelQuery" → this block

2. Drag a **Play prompt** block
   - Text: `I've cancelled that request.`
   - Connect the Lambda block → this Play Prompt → Block 13a (Ask Another Question)

#### Block 11b: Handle New Question

Two sub-blocks:

1. Drag an **Invoke AWS Lambda function** block
   - Select: `ConnectAsync-Disconnect`
   - Input parameters: Key = `sessionId`, Value = **Use attribute**, Type = `User Defined`, Attribute = `sessionId`
   - Connect Block 11 "AskNewQuestion" → this block

2. Drag a **Play prompt** block
   - Text: `Sure, go ahead and ask your new question.`
   - Connect the Lambda block → this Play Prompt → **Block 3** (Get Customer Input — starts a new session)

#### Block 11c: Handle Repeat

1. Drag a **Play prompt** block
2. Set **Text to speech**: select **Use attribute**, Type = `User Defined`, Attribute = `last_response_text`
3. Set **Interpret as**: SSML
4. Click **Save**
5. Connect Block 11 "RepeatLastResponse" → this block → **Block 7** (back to polling loop)

> If `last_response_text` is empty (no response delivered yet), the Play Prompt block will play silence. Consider adding a Check Contact Attributes block before this to detect the empty case and play a fallback message instead.

### Section 4: Error Handling (Blocks 12-12c)

#### Block 12: Set Error Count

1. Drag a **Set contact attributes** block
2. Add attribute: Key = `error_count`, Value = **Use text**, enter a value
3. Click **Save**

> Connect does not support arithmetic in Set Contact Attributes natively. To increment `error_count`, you have two options:
> - Use a Lambda function that reads the current value, increments it, and returns the new value
> - Use a series of Check Contact Attributes blocks (if error_count = 0 → set to 1, if 1 → set to 2, if 2 → set to 3, if >= 3 → escalate)
>
> The simplest approach for prototyping: skip the counter and just route all errors to Block 12b (retry). Add the counter logic later.

For the simple approach:
1. Connect Block 8 "retry" → Block 12b directly
2. Connect Block 8 "No match" → Block 12b directly
3. Connect Block 7 "Error" → Block 12b directly

For the full approach with error counting, use a Lambda:
1. Create a small inline Lambda or reuse the Polling Lambda with an `action=increment_error` parameter
2. Route Block 8 "retry" / "No match" / Block 7 "Error" → Invoke Lambda (increment) → Check if >= 3 → Block 12a or Block 12b

#### Block 12a: Error Escalation

1. Drag a **Play prompt** block
2. Text: `I'm sorry, I'm having trouble retrieving your results right now. Would you like me to keep trying, or would you prefer to end the call?`
3. Click **Save**
4. Connect → Block 12c

#### Block 12b: Retry Poll

1. Drag a **Play prompt** block
2. Text: `One moment please.`
3. Click **Save**
4. Connect → **Block 7** (back to polling loop)

#### Block 12c: Error Decision

1. Drag a **Get customer input** block
2. Set **Input type**: Speech
3. Under **Amazon Lex**: select `AsyncResponseInterruptBot`, alias `live`
4. Under **Session attributes**: Key = `x-amz-lex:audio:start-timeout-ms`, Value = `8000` (longer timeout — give caller time to decide)
5. Wire intents:
   - `CancelQuery` → Block 15 (End Call)
   - `FallbackIntent` → **Block 7** (retry polling — caller said something other than cancel)
   - `Error` → Block 15 (End Call)
   - `Timeout` → Block 15 (End Call — caller didn't respond)

### Section 5: Completion and End (Blocks 13-15)

#### Block 13: Play Completion

1. Drag a **Play prompt** block
2. Text: `That's everything I have for you on that question.`
3. Click **Save**
4. Connect Block 8 "complete" → Block 13

#### Block 13a: Ask Another Question

1. Drag a **Play prompt** block
2. Text: `Would you like to ask another question, or is there anything else I can help with?`
3. Click **Save**
4. Connect Block 13 → Block 13a

#### Block 13b: Capture Decision

1. Drag a **Get customer input** block
2. Set **Input type**: Speech
3. Under **Amazon Lex**: select `AsyncResponseInterruptBot`, alias `live`
4. Under **Session attributes**: Key = `x-amz-lex:audio:start-timeout-ms`, Value = `8000`
5. Wire intents:
   - `AskNewQuestion` → **Block 3** (Get Customer Input — new question)
   - `CancelQuery` → Block 15 (End Call)
   - `FallbackIntent` → **Block 3** (treat unrecognized speech as wanting to ask a new question)
   - `Error` → Block 15 (End Call)
   - `Timeout` → Block 15 (End Call — caller didn't respond, assume done)
6. Connect Block 13a → Block 13b

#### Block 14: Fatal Error

1. Drag a **Play prompt** block
2. Text: `I'm sorry, I'm unable to process your request at this time. Please try again later or contact support directly.`
3. Click **Save**
4. Now connect all the deferred error outputs:
   - Block 3 "Error" → Block 14
   - Block 3 "Timeout" → Block 14
   - Block 4 "Error" → Block 14
   - Block 5 "No match" → Block 14
5. Connect Block 14 → Block 15

#### Block 15: End Call

1. Drag a **Disconnect / hang up** block
2. No configuration needed
3. Connect Block 14 → Block 15

### Final wiring summary (Option A)

```
Entry → [1: Set Recording] → [2: Welcome] → [3: Get Question (Lex)]
                                                    ↓ CaptureQuestion
                                              [4: Submit Lambda]
                                                    ↓ Success
                                              [5: Check Status]
                                                    ↓ submitted
                                              [6: Acknowledgment]
                                                    ↓
    ┌─────────────────────────────────────→ [7: Polling Lambda] ←──────────────────────┐
    │                                            ↓ Success                              │
    │                                      [8: Check Status]                            │
    │                                       ├── results → [9: Play Response] → [9a: Set Attrs] ──→ back to 7
    │                                       ├── waiting → [10: Hold Message]            │
    │                                       │                  ↓                        │
    │                                       │            [11: Lex Interrupt]             │
    │                                       │             ├── FallbackIntent ────────────┘
    │                                       │             ├── CancelQuery → [11a] → [13a: Ask Another]
    │                                       │             ├── AskNewQuestion → [11b] → back to 3
    │                                       │             └── Repeat → [11c] → back to 7
    │                                       ├── complete → [13: Completion] → [13a] → [13b: Decision]
    │                                       │                                          ├── AskNew → back to 3
    │                                       │                                          └── Cancel/Timeout → 15
    │                                       └── retry/error → [12b: Retry] ─────────────┘
    │
    └── (from 12b, 11c, 11 FallbackIntent/Error/Timeout)

Errors: Block 3/4/5 errors → [14: Fatal Error] → [15: End Call]
```

### Save and Publish

1. Review all connections — especially the loop-backs to Block 7
2. Click **Save**
3. Click **Publish**

---

## Post-Flow Setup: Disconnect Handler

This applies to BOTH Option A and Option E.

1. Create a separate contact flow named `Nexthink-Disconnect-Handler`
2. Add a single **Invoke AWS Lambda function** block
3. Select function: `ConnectAsync-Disconnect`
4. No input parameters needed — the Lambda reads `sessionId` from the contact attributes automatically
5. Connect Entry → Lambda block → Disconnect block
6. Save and Publish this flow
7. Go back to your main flow (Option A or E)
8. In the flow settings (gear icon or flow properties), set **Disconnect flow** to `Nexthink-Disconnect-Handler`

> This ensures that when a caller hangs up mid-session, the session is marked ABANDONED in DynamoDB and no further callbacks are stored.

---

## Post-Flow Setup: Assign Phone Number

1. Go to Connect console → Phone numbers
2. Select the phone number (CDK-claimed or manually claimed)
3. Under **Contact flow / IVR**, select your flow (`Nexthink-Async-MultiResponse-OptionE` or `OptionA`)
4. Click **Save**

---

## Verification Checklist

After building and publishing, verify:

- [ ] Flow is published (not just saved as draft)
- [ ] Phone number is assigned to the flow
- [ ] Disconnect flow is set
- [ ] Contact Lens is enabled at the instance level
- [ ] All Lambda functions appear in the Lambda function dropdown (Connect console → Contact flows → AWS Lambda)
- [ ] Both Lex bots appear in the Lex bot dropdown (Connect console → Contact flows → Amazon Lex)
- [ ] For Option E: Wisdom assistant is associated with the instance
- [ ] For Option E: Lex bot was created via Connect admin console (NOT via API/CDK) with QinConnectIntent enabled and Wisdom ARN configured
- [ ] For Option E: If using Nova Sonic, Speech-to-Speech model configured on bot locale and rebuilt
- [ ] For Option E: BOT_MANAGEMENT toggled off/on if you see "could not access Q In Connect Assistant" errors
- [ ] Call the phone number and verify you hear the welcome message (Option A) or the AI agent responds (Option E)
