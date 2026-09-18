"""Polling Lambda / check_responses MCP tool handler.

Queries DynamoDB for undelivered responses, marks them as delivered,
and returns status to the contact flow or Orchestrator.
"""

from __future__ import annotations

import logging
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from lambdas.shared.config import get_config
from lambdas.shared.metrics import publish_metric
from lambdas.shared.models import (
    PARTITION_KEY,
    SORT_KEY,
    TABLE_NAME,
    PollingResult,
    SessionStatus,
)
from lambdas.shared.session_manager import get_session_status
from lambdas.shared.tts import format_ssml, select_hold_message, split_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _get_table(table_name: str | None = None):
    """Return a DynamoDB Table resource."""
    name = table_name or TABLE_NAME
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(name)


# ---------------------------------------------------------------------------
# Core polling logic
# ---------------------------------------------------------------------------


def check_responses(
    session_id: str,
    cursor: int,
    table_name: str | None = None,
    limit: int | None = None,
) -> PollingResult:
    """Query DynamoDB for undelivered responses after *cursor*.

    Parameters
    ----------
    limit:
        Maximum number of responses to mark as delivered and return.
        ``None`` returns all undelivered responses (Option E).
        ``1`` returns one at a time (Option A — contact flow can only
        play one response per cycle).

    Returns a :class:`PollingResult` with the appropriate status.
    """
    try:
        sentinel = get_session_status(session_id, table_name)
    except ClientError as exc:
        logger.exception("DynamoDB error reading session %s", session_id)
        return PollingResult(status="retry", message=str(exc))

    if sentinel is None:
        return PollingResult(status="error", message="Session not found")

    # Calculate elapsed seconds since session creation
    elapsed = (time.time() * 1000 - sentinel.CreatedAt) / 1000.0

    # Query for records with SequenceNumber > cursor
    try:
        table = _get_table(table_name)
        response = table.query(
            KeyConditionExpression=(
                Key(PARTITION_KEY).eq(session_id)
                & Key(SORT_KEY).gt(max(cursor, 0))
            ),
        )
    except ClientError as exc:
        logger.exception("DynamoDB query error for session %s", session_id)
        return PollingResult(status="retry", message=str(exc), elapsedSeconds=elapsed)

    items = response.get("Items", [])

    # Filter for undelivered records only
    undelivered = [
        item for item in items
        if not item.get("Delivered", False)
    ]

    # Publish PollIteration metric
    publish_metric("PollIteration", session_id)

    if undelivered:
        return _handle_results(session_id, undelivered, elapsed, table_name, limit)

    # No undelivered responses
    session_status = SessionStatus(sentinel.SessionStatus)

    if session_status == SessionStatus.COMPLETE:
        return PollingResult(
            status="complete",
            cursor=cursor,
            elapsedSeconds=elapsed,
        )

    # Session still ACTIVE — check timeout conditions
    config = get_config()
    initial_timeout = (
        elapsed > config.INITIAL_RESPONSE_TIMEOUT_S and cursor == 0
    )
    session_timeout = elapsed > config.MAX_SESSION_DURATION_S

    if session_timeout:
        publish_metric("SessionTimeout", session_id)

    return PollingResult(
        status="processing",
        cursor=cursor,
        elapsedSeconds=elapsed,
        initialTimeout=initial_timeout,
        sessionTimeout=session_timeout,
    )


def _handle_results(
    session_id: str,
    undelivered: list[dict],
    elapsed: float,
    table_name: str | None,
    limit: int | None = None,
) -> PollingResult:
    """Mark undelivered items as delivered and return them.

    When *limit* is set, only the first *limit* items are marked
    delivered and returned.  The rest remain undelivered for the
    next poll cycle.
    """
    table = _get_table(table_name)

    # Sort by sequence number
    undelivered.sort(key=lambda item: int(item[SORT_KEY]))

    # Apply limit — only process the first N items
    to_deliver = undelivered[:limit] if limit else undelivered

    responses: list[dict] = []
    max_seq = 0

    for item in to_deliver:
        seq = int(item[SORT_KEY])
        responses.append({
            "sequenceNumber": seq,
            "text": item.get("ResponseText", ""),
        })
        if seq > max_seq:
            max_seq = seq

        # Mark as delivered
        try:
            table.update_item(
                Key={PARTITION_KEY: session_id, SORT_KEY: seq},
                UpdateExpression="SET Delivered = :d",
                ExpressionAttributeValues={":d": True},
            )
        except ClientError:
            logger.exception(
                "Failed to mark delivered: session=%s seq=%d", session_id, seq
            )

    # Publish ResponseDelivered metric for each response
    for _ in responses:
        publish_metric("ResponseDelivered", session_id)

    logger.info(
        "Polling results: session_id=%s responses=%d cursor=%d elapsed=%.1f",
        session_id,
        len(responses),
        max_seq,
        elapsed,
    )

    return PollingResult(
        status="results",
        responses=responses,
        cursor=max_seq,
        elapsedSeconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Option A format converter
# ---------------------------------------------------------------------------

# Tracks the last hold message per session to avoid consecutive repeats.
# Reset each cold start — acceptable since hold message variation is
# best-effort across invocations.
_last_hold_message: str | None = None


def _to_contact_attributes(result: PollingResult) -> dict:
    """Convert a :class:`PollingResult` to a flat string key-value map.

    Contact flow Lambda integrations require all values to be strings.

    Key adaptations for Option A:
    - ``"processing"`` status is mapped to ``"waiting"`` for the contact flow
    - Response text is split and SSML-formatted when it exceeds the max length
    - A ``holdMessage`` is included when status is ``"waiting"``
    """
    global _last_hold_message

    # Map "processing" → "waiting" for Option A contact flow compatibility
    status = "waiting" if result.status == "processing" else result.status

    attrs: dict[str, str] = {
        "status": status,
        "cursor": str(result.cursor),
        "elapsedSeconds": str(result.elapsedSeconds),
        "isComplete": str(result.status == "complete").lower(),
    }

    if result.responses:
        # Format the first response with SSML splitting for TTS delivery
        raw_text = result.responses[0].get("text", "")
        config = get_config()
        segments = split_text(raw_text, config.MAX_RESPONSE_LENGTH)
        attrs["responseText"] = format_ssml(segments)

    if status == "waiting":
        config = get_config()
        hold_msg = select_hold_message(config.HOLD_MESSAGE_POOL, _last_hold_message)
        _last_hold_message = hold_msg
        attrs["holdMessage"] = hold_msg

    if result.message:
        attrs["errorMessage"] = result.message

    if result.initialTimeout:
        attrs["initialTimeout"] = "true"

    if result.sessionTimeout:
        attrs["sessionTimeout"] = "true"

    return attrs


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for polling / check_responses.

    Supports two invocation formats:
    - Option E (MCP tool): ``{ "sessionId": "...", "cursor": 0 }``
    - Option A (contact flow): ``{ "Details": { "Parameters": { "sessionId": "...", "cursor": "0" } } }``
    """
    logger.info("Polling handler invoked: event_keys=%s", list(event.keys()))

    # Detect format and extract parameters
    if "Details" in event:
        # Option A — contact flow Lambda invocation
        params = event["Details"].get("Parameters", {})
        session_id = params.get("sessionId", "")
        cursor = int(params.get("cursor", "0"))
        logger.info("Option A format: session_id=%s cursor=%d", session_id, cursor)
    else:
        # Option E — MCP tool direct invocation
        session_id = event.get("sessionId", "")
        cursor = int(event.get("cursor", 0))
        logger.info("Option E format: session_id=%s cursor=%d", session_id, cursor)

    if not session_id:
        result = PollingResult(status="error", message="Missing sessionId")
    elif "Details" in event:
        # Option A — one response per cycle
        result = check_responses(session_id, cursor, limit=1)
    else:
        # Option E — all responses at once
        result = check_responses(session_id, cursor)

    logger.info("Polling result: status=%s responses=%d cursor=%s elapsed=%.1f",
                result.status, len(result.responses or []),
                result.cursor, result.elapsedSeconds or 0)

    # Route response format
    if "Details" in event:
        return _to_contact_attributes(result)
    return result.model_dump()
