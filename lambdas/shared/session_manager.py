"""Session lifecycle manager for DynamoDB-backed sessions.

Provides create, read, and state-transition operations for
async response sessions using DynamoDB conditional expressions.
"""

from __future__ import annotations

import logging
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr

from lambdas.shared.models import (
    PARTITION_KEY,
    SENTINEL_SEQUENCE_NUMBER,
    SORT_KEY,
    TABLE_NAME,
    TTL_DURATION_SECONDS,
    ResponseRecord,
    SessionStatus,
)

logger = logging.getLogger(__name__)


def _get_table(table_name: str | None = None):
    """Return a DynamoDB Table resource."""
    name = table_name or TABLE_NAME
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(name)


# ---------------------------------------------------------------------------
# Session ID generation (Task 2.2)
# ---------------------------------------------------------------------------


def generate_session_id() -> str:
    """Generate a unique session ID using UUID4."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Session lifecycle (Task 2.1)
# ---------------------------------------------------------------------------


def create_session(
    session_id: str,
    query_text: str,
    table_name: str | None = None,
) -> ResponseRecord:
    """Create a sentinel record (SequenceNumber=0) for a new session.

    Uses a conditional expression to prevent overwriting an existing session.

    Returns the created ``ResponseRecord``.

    Raises ``botocore.exceptions.ClientError`` with code
    ``ConditionalCheckFailedException`` if the session already exists.
    """
    table = _get_table(table_name)
    created_at = int(time.time() * 1000)  # epoch milliseconds
    ttl = created_at // 1000 + TTL_DURATION_SECONDS

    record = ResponseRecord(
        SessionID=session_id,
        SequenceNumber=SENTINEL_SEQUENCE_NUMBER,
        SessionStatus=SessionStatus.ACTIVE,
        QueryText=query_text,
        CreatedAt=created_at,
        TTL=ttl,
        ResponseCount=0,
    )

    item = {
        PARTITION_KEY: record.SessionID,
        SORT_KEY: record.SequenceNumber,
        "SessionStatus": record.SessionStatus.value,
        "QueryText": record.QueryText,
        "CreatedAt": record.CreatedAt,
        "TTL": record.TTL,
        "IsComplete": record.IsComplete,
        "Delivered": record.Delivered,
        "ResponseCount": record.ResponseCount,
    }

    table.put_item(
        Item=item,
        ConditionExpression=Attr(PARTITION_KEY).not_exists(),
    )

    logger.info("Session created: %s", session_id)
    return record


def get_session_status(
    session_id: str,
    table_name: str | None = None,
) -> ResponseRecord | None:
    """Read the sentinel record for *session_id*.

    Returns ``None`` if the session does not exist.
    """
    table = _get_table(table_name)
    response = table.get_item(
        Key={PARTITION_KEY: session_id, SORT_KEY: SENTINEL_SEQUENCE_NUMBER},
    )
    item = response.get("Item")
    if item is None:
        return None

    return ResponseRecord(
        SessionID=item[PARTITION_KEY],
        SequenceNumber=item[SORT_KEY],
        SessionStatus=SessionStatus(item.get("SessionStatus", "ACTIVE")),
        QueryText=item.get("QueryText"),
        CreatedAt=item["CreatedAt"],
        TTL=item["TTL"],
        IsComplete=item.get("IsComplete", False),
        Delivered=item.get("Delivered", False),
        LastResponseAt=item.get("LastResponseAt"),
        ResponseCount=item.get("ResponseCount"),
    )


def _transition_session(
    session_id: str,
    target_status: SessionStatus,
    table_name: str | None = None,
) -> bool:
    """Conditionally update session status from ACTIVE to *target_status*.

    Returns ``True`` if the update succeeded, ``False`` if the condition
    failed (session not ACTIVE).
    """
    table = _get_table(table_name)
    try:
        table.update_item(
            Key={PARTITION_KEY: session_id, SORT_KEY: SENTINEL_SEQUENCE_NUMBER},
            UpdateExpression="SET SessionStatus = :new_status",
            ConditionExpression=Attr("SessionStatus").eq(SessionStatus.ACTIVE.value),
            ExpressionAttributeValues={":new_status": target_status.value},
        )
        logger.info("Session %s transitioned to %s", session_id, target_status.value)
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning(
            "Session %s transition to %s failed — not ACTIVE",
            session_id,
            target_status.value,
        )
        return False


def mark_session_complete(
    session_id: str,
    table_name: str | None = None,
) -> bool:
    """Set session status to COMPLETE (only if currently ACTIVE)."""
    return _transition_session(session_id, SessionStatus.COMPLETE, table_name)


def mark_session_abandoned(
    session_id: str,
    table_name: str | None = None,
) -> bool:
    """Set session status to ABANDONED (only if currently ACTIVE)."""
    return _transition_session(session_id, SessionStatus.ABANDONED, table_name)


def mark_session_timed_out(
    session_id: str,
    table_name: str | None = None,
) -> bool:
    """Set session status to TIMED_OUT (only if currently ACTIVE)."""
    return _transition_session(session_id, SessionStatus.TIMED_OUT, table_name)


def is_session_active(
    session_id: str,
    table_name: str | None = None,
) -> bool:
    """Return ``True`` if the session exists and its status is ACTIVE."""
    record = get_session_status(session_id, table_name)
    if record is None:
        return False
    return record.SessionStatus == SessionStatus.ACTIVE
