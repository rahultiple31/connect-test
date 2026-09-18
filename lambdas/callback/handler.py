"""Callback Lambda handler.

Receives async responses from the Nexthink agent via API Gateway,
validates the payload, and writes to DynamoDB.

Accepts two payload shapes and auto-detects which one arrived:

- **Native** (mock agent / simple-JSON backend)::

      {"sessionId": "...", "sequenceNumber": 1, "responseText": "...", "isComplete": false}

- **Nexthink Spark A2A** ``statusUpdate`` envelope — translated by
  :func:`_translate_nexthink_payload` into the native shape, given a
  sequence number by :func:`_next_spark_seq` (DynamoDB atomic counter on the
  session sentinel), then processed by the exact same pipeline. No backend
  switch is needed on this side.
"""

from __future__ import annotations

import json
import logging
import time

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from lambdas.shared.metrics import publish_metric
from lambdas.shared.models import (
    PARTITION_KEY,
    SENTINEL_SEQUENCE_NUMBER,
    SORT_KEY,
    TABLE_NAME,
    TTL_DURATION_SECONDS,
    CallbackPayload,
    SessionStatus,
)
from lambdas.shared.session_manager import get_session_status, mark_session_complete

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _get_table():
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(TABLE_NAME)


# ---------------------------------------------------------------------------
# Nexthink Spark (A2A) payload translation
# ---------------------------------------------------------------------------

_SPARK_STATE_WORKING = "TASK_STATE_WORKING"


def _translate_nexthink_payload(body: dict) -> dict | None:
    """Translate a Spark A2A ``statusUpdate`` envelope into the native shape.

    Pure function — no I/O. Spark carries no sequence number, so the result
    has NO ``sequenceNumber``; the handler allocates one with
    :func:`_next_spark_seq` once the session is known to exist.

    Spark sends::

        {"statusUpdate": {
            "taskId": "...",
            "contextId": "<uuid we sent in message.contextId>",
            "status": {
                "state": "TASK_STATE_WORKING" | "TASK_STATE_COMPLETED" | ...,
                "message": {"content": [{"text": "..."}, ...]}
            }
        }}

    We produce::

        {"sessionId": "<contextId>", "responseText": "<joined text>", "isComplete": bool}

    ``contextId`` is the correlation key — it is the ``sessionId`` the submit
    Lambda put into the outbound request. ``isComplete`` is derived from the
    task state: anything other than ``TASK_STATE_WORKING`` ends the session.

    Returns ``None`` if the envelope is missing ``contextId`` or has no text.
    """
    try:
        update = body["statusUpdate"]
        context_id = update.get("contextId", "")
        status = update.get("status", {})
        state = status.get("state", "")
        content_parts = status.get("message", {}).get("content", [])
        text = " ".join(part.get("text", "") for part in content_parts).strip()

        if not context_id or not text:
            logger.warning("Nexthink payload missing contextId or text")
            return None

        logger.info(
            "Nexthink callback: contextId=%s state=%s textLen=%d",
            context_id, state, len(text),
        )
        return {
            "sessionId": context_id,
            "responseText": text,
            "isComplete": state != _SPARK_STATE_WORKING,
        }
    except Exception as exc:  # noqa: BLE001 — malformed envelope → 400, never 500
        logger.error("Failed to translate Nexthink payload: %s", exc)
        return None


def _next_spark_seq(table, session_id: str) -> int | None:
    """Atomically allocate the next sequence number for a Spark session.

    Increments ``NextSeq`` on the session's sentinel record. DynamoDB performs
    the ``ADD`` atomically, so concurrent Lambda containers handling callbacks
    for the same session always receive distinct, monotonically increasing
    values — no in-process state, nothing lost on cold start.

    The condition ``attribute_exists(SessionID)`` prevents the update from
    creating a phantom sentinel for an unknown session.

    Returns ``None`` if the session does not exist.
    """
    try:
        resp = table.update_item(
            Key={PARTITION_KEY: session_id, SORT_KEY: SENTINEL_SEQUENCE_NUMBER},
            UpdateExpression="ADD NextSeq :one",
            ConditionExpression="attribute_exists(#pk)",
            ExpressionAttributeNames={"#pk": PARTITION_KEY},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return int(resp["Attributes"]["NextSeq"])


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _build_response(status_code: int, body: dict) -> dict:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _redact_text(text: str, max_len: int = 50) -> str:
    """Truncate text for safe logging."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for callback ingestion."""
    logger.info("Callback handler invoked: httpMethod=%s path=%s",
                event.get("httpMethod", "?"), event.get("path", "?"))

    # Parse body
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        logger.error("Invalid JSON body")
        return _build_response(400, {"error": "Invalid JSON body"})

    # --- Detect Nexthink Spark A2A format and translate to native shape ---
    if "statusUpdate" in body:
        body = _translate_nexthink_payload(body)
        if body is None:
            return _build_response(400, {"error": "Invalid Nexthink payload"})
        # Spark has no sequence number — allocate one atomically on the sentinel.
        seq = _next_spark_seq(_get_table(), body["sessionId"])
        if seq is None:
            logger.warning("Unknown session: %s", body["sessionId"])
            return _build_response(404, {"error": "Session not found"})
        body["sequenceNumber"] = seq

    logger.info("Callback payload: sessionId=%s seq=%s isComplete=%s textLen=%d",
                body.get("sessionId", "?"), body.get("sequenceNumber", "?"),
                body.get("isComplete", "?"), len(body.get("responseText", "")))

    # Validate payload
    try:
        payload = CallbackPayload(**body)
    except ValidationError as exc:
        logger.error("Validation error: %s", str(exc)[:200])
        return _build_response(400, {"error": str(exc)})

    session_id = payload.sessionId
    seq = payload.sequenceNumber

    # Check session existence
    sentinel = get_session_status(session_id)
    if sentinel is None:
        logger.warning("Unknown session: %s", session_id)
        return _build_response(404, {"error": "Session not found"})

    # Publish CallbackReceived metric (always)
    publish_metric("CallbackReceived", session_id)

    # If session is ABANDONED or TIMED_OUT — accept but discard
    if sentinel.SessionStatus in (SessionStatus.ABANDONED, SessionStatus.TIMED_OUT):
        logger.info(
            "Discarding callback for %s session %s seq=%d",
            sentinel.SessionStatus.value,
            session_id,
            seq,
        )
        publish_metric("CallbackDiscarded", session_id)
        return _build_response(200, {"message": "Callback discarded — session inactive"})

    # Write response record to DynamoDB
    table = _get_table()
    now_ms = int(time.time() * 1000)
    ttl = now_ms // 1000 + TTL_DURATION_SECONDS

    item = {
        PARTITION_KEY: session_id,
        SORT_KEY: seq,
        "ResponseText": payload.responseText,
        "IsComplete": payload.isComplete,
        "Delivered": False,
        "SessionStatus": sentinel.SessionStatus.value,
        "CreatedAt": now_ms,
        "TTL": ttl,
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#pk) OR attribute_not_exists(#sk)",
            ExpressionAttributeNames={"#pk": PARTITION_KEY, "#sk": SORT_KEY},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Duplicate sequence number — idempotent success
            logger.info(
                "Duplicate callback ignored: session=%s seq=%d",
                session_id,
                seq,
            )
            return _build_response(200, {"message": "Duplicate accepted (idempotent)"})
        # Unexpected DynamoDB error
        logger.exception("DynamoDB write failed for session=%s seq=%d", session_id, seq)
        publish_metric("CallbackError", session_id)
        return _build_response(500, {"error": "Internal server error"})

    # If isComplete, transition session to COMPLETE (only if still ACTIVE)
    if payload.isComplete:
        try:
            table.update_item(
                Key={PARTITION_KEY: session_id, SORT_KEY: 0},
                UpdateExpression=(
                    "SET SessionStatus = :status, LastResponseAt = :ts, "
                    "ResponseCount = if_not_exists(ResponseCount, :zero) + :inc"
                ),
                ConditionExpression="SessionStatus = :active",
                ExpressionAttributeValues={
                    ":status": SessionStatus.COMPLETE.value,
                    ":active": SessionStatus.ACTIVE.value,
                    ":ts": now_ms,
                    ":zero": 0,
                    ":inc": 1,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info(
                    "Session %s not ACTIVE — skipping completion transition",
                    session_id,
                )
            else:
                logger.exception("Failed to mark session complete: %s", session_id)
    else:
        # Update sentinel metadata (LastResponseAt, ResponseCount)
        try:
            table.update_item(
                Key={PARTITION_KEY: session_id, SORT_KEY: 0},
                UpdateExpression=(
                    "SET LastResponseAt = :ts, "
                    "ResponseCount = if_not_exists(ResponseCount, :zero) + :inc"
                ),
                ExpressionAttributeValues={
                    ":ts": now_ms,
                    ":zero": 0,
                    ":inc": 1,
                },
            )
        except ClientError:
            logger.exception("Failed to update sentinel for session: %s", session_id)

    # Structured log (redact response text)
    logger.info(
        "Callback processed: session_id=%s sequence_number=%d payload_size=%d "
        "processing_result=stored response_text=%s",
        session_id,
        seq,
        len(payload.responseText),
        _redact_text(payload.responseText),
    )

    return _build_response(200, {"message": "Response stored"})
