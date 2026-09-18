"""Mock Nexthink agent backed by Amazon Bedrock.

Simulates the Nexthink AI agent for prototyping. Receives a query,
calls Bedrock to generate a multi-part IT support response, then
POSTs each part to the callback URL with delays — mimicking the
real Nexthink agent's asynchronous callback behavior.

Works with both Option E (Orchestrator) and Option A (Lambda polling)
since it feeds the shared callback ingestion layer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
RESPONSE_DELAY_S = float(os.environ.get("RESPONSE_DELAY_S", "2"))

_callback_api_key: str | None = None


def _get_callback_api_key() -> str:
    """Load the callback API key from Secrets Manager (cached)."""
    global _callback_api_key
    if _callback_api_key is not None:
        return _callback_api_key

    secret_name = os.environ.get("MOCK_CALLBACK_API_KEY_SECRET_NAME")
    if not secret_name:
        return os.environ.get("MOCK_CALLBACK_API_KEY", "")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    _callback_api_key = response["SecretString"]
    return _callback_api_key

SYSTEM_PROMPT = """\
You are simulating an IT support AI agent (Nexthink) for prototyping purposes.
Given a user's IT question, generate a realistic multi-part response.

Rules:
- Split your answer into 2-4 separate response parts.
- Each part should be a self-contained paragraph (3-5 sentences).
- Part 1: acknowledge the issue and provide initial diagnosis.
- Part 2+: provide step-by-step resolution or additional findings.
- Final part: summarize and confirm resolution.
- Keep each part under 500 characters.
- Return ONLY a JSON array of strings, one per response part.

Example output:
["Based on your description, it appears your Outlook cache may be corrupted. This is a common issue that can cause freezing when the application starts. Let me look into the specific steps to resolve this.",
"To fix this, please close Outlook completely. Then navigate to %localappdata%\\\\Microsoft\\\\Outlook and delete the .ost file. This file contains your cached mailbox data and will be rebuilt automatically.",
"After deleting the cache file, restart Outlook. It will take a few minutes to rebuild the cache and re-sync your mailbox. If the freezing persists after this, please let us know and we can investigate further."]
"""


def _invoke_bedrock(query: str) -> list[str]:
    """Call Bedrock to generate a multi-part response."""
    client = boto3.client("bedrock-runtime")

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": query},
        ],
    })

    response = client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )

    result = json.loads(response["body"].read())
    return _parse_parts(result["content"][0]["text"])


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _parse_parts(text: str) -> list[str]:
    """Turn the model's reply into a list of response parts.

    Claude frequently wraps the requested JSON array in a ```json fence even
    when told to return ONLY JSON. Without stripping it, json.loads fails and
    the fallback splitter hands raw brackets/quotes/backslashes to TTS — the
    caller literally hears "backtick backtick backtick json open bracket".
    """
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1)

    try:
        parts = json.loads(text)
        if isinstance(parts, list) and all(isinstance(p, str) for p in parts):
            return [p.strip() for p in parts if p.strip()]
    except json.JSONDecodeError:
        pass

    # Fallback: if Bedrock didn't return valid JSON, split on double newlines
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return parts if parts else [text]

MAX_PART_CHARS = int(os.environ.get("MAX_PART_CHARS", "500"))
MIN_PARTS = int(os.environ.get("MIN_PARTS", "2"))


def _ensure_multi_part(parts: list[str]) -> list[str]:
    """Post-process Bedrock output to guarantee multiple response parts.

    If Bedrock returns a single long response, split it into chunks at
    sentence boundaries (~MAX_PART_CHARS each). This ensures the async
    multi-response pattern is always exercised.
    """
    if len(parts) >= MIN_PARTS:
        return parts

    # Join everything and re-split at sentence boundaries
    full_text = " ".join(parts)
    if len(full_text) <= MAX_PART_CHARS:
        # Too short to split meaningfully — duplicate with a summary
        return [
            full_text,
            "To summarize: " + full_text[:200] + ("..." if len(full_text) > 200 else "")
            + " Please let me know if you need any further assistance.",
        ]

    # Split on sentence endings (. ! ?) respecting the char limit
    import re
    sentences = re.split(r'(?<=[.!?])\s+', full_text)
    result: list[str] = []
    current = ""

    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > MAX_PART_CHARS:
            result.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip() if current else sentence

    if current.strip():
        result.append(current.strip())

    # If we still ended up with 1 part, force-split at the midpoint
    if len(result) < MIN_PARTS:
        mid = len(full_text) // 2
        # Find nearest sentence boundary near midpoint
        for i in range(mid, min(mid + 100, len(full_text))):
            if full_text[i] in '.!?' and i + 1 < len(full_text):
                result = [full_text[:i + 1].strip(), full_text[i + 1:].strip()]
                break
        else:
            result = [full_text[:mid].strip(), full_text[mid:].strip()]

    return result



def _send_callback(
    callback_url: str,
    session_id: str,
    sequence_number: int,
    response_text: str,
    is_complete: bool,
) -> None:
    """POST a single callback to the callback endpoint."""
    payload = json.dumps({
        "sessionId": session_id,
        "sequenceNumber": sequence_number,
        "responseText": response_text,
        "isComplete": is_complete,
        "metadata": {
            "sourceAgent": "mock-nexthink-bedrock",
            "confidence": 0.85,
        },
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
    }
    api_key = _get_callback_api_key()
    if api_key:
        headers["x-api-key"] = api_key

    req = urllib.request.Request(
        callback_url,
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            logger.info(
                "Callback sent: session=%s seq=%d status=%d",
                session_id, sequence_number, resp.status,
            )
    except urllib.error.HTTPError as exc:
        logger.error(
            "Callback failed: session=%s seq=%d status=%d",
            session_id, sequence_number, exc.code,
        )
    except urllib.error.URLError as exc:
        logger.error(
            "Callback connection error: session=%s seq=%d error=%s",
            session_id, sequence_number, exc,
        )


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for the mock Nexthink agent.

    Two modes:
    1. API Gateway / direct: receives query, self-invokes async, returns 202 immediately
    2. Async (_async=true): generates response via Bedrock and sends callbacks with delays

    This two-phase approach lets the caller (Submit Lambda) get 202 back
    instantly while callbacks arrive asynchronously — matching real Nexthink behavior.
    """
    # Phase 2: async callback delivery (self-invoked)
    if event.get("_async"):
        return _handle_async_callbacks(event)

    # Phase 1: receive request, self-invoke async, return 202
    if "body" in event:
        try:
            body = json.loads(event.get("body") or "{}")
        except (json.JSONDecodeError, TypeError):
            return _api_response(400, {"error": "Invalid JSON body"})
    else:
        body = event

    query = body.get("query", "")
    session_id = body.get("sessionId", "")
    callback_url = body.get("callbackUrl", "")

    if not query or not session_id or not callback_url:
        return _api_response(400, {"error": "Missing query, sessionId, or callbackUrl"})

    logger.info(
        "Mock Nexthink received query: session=%s query_len=%d",
        session_id, len(query),
    )

    # Self-invoke asynchronously for callback delivery
    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=context.function_name,
        InvocationType="Event",  # async — returns immediately
        Payload=json.dumps({
            "_async": True,
            "query": query,
            "sessionId": session_id,
            "callbackUrl": callback_url,
        }),
    )
    logger.info("Async self-invocation triggered for session %s", session_id)

    return _api_response(202, {"message": "Accepted"})


def _handle_async_callbacks(event: dict) -> dict:
    """Phase 2: generate response via Bedrock and send callbacks with delays."""
    query = event["query"]
    session_id = event["sessionId"]
    callback_url = event["callbackUrl"]

    logger.info("Async handler started: session=%s", session_id)

    # Generate response parts via Bedrock
    try:
        parts = _invoke_bedrock(query)
    except Exception:
        logger.exception("Bedrock invocation failed for session %s", session_id)
        return {"error": "Bedrock failed"}

    logger.info("Bedrock generated %d parts for session %s", len(parts), session_id)

    # Post-process: ensure multiple parts
    parts = _ensure_multi_part(parts)
    logger.info("After split: %d parts (delay=%.1fs)", len(parts), RESPONSE_DELAY_S)

    # Send callbacks with delays
    for i, part in enumerate(parts):
        seq = i + 1
        is_last = seq == len(parts)

        if i > 0:
            time.sleep(RESPONSE_DELAY_S)

        _send_callback(callback_url, session_id, seq, part, is_last)

    logger.info("All callbacks sent for session %s", session_id)
    return {"status": "done", "parts": len(parts)}


def _api_response(status_code: int, body: dict) -> dict:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
