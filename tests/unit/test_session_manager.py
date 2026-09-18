"""Unit tests for lambdas/shared/session_manager.py — session lifecycle and ID generation."""

from __future__ import annotations

import uuid

import boto3
import pytest
from moto import mock_aws

from lambdas.shared.models import (
    PARTITION_KEY,
    SENTINEL_SEQUENCE_NUMBER,
    SORT_KEY,
    TABLE_NAME,
    TTL_DURATION_SECONDS,
    SessionStatus,
)
from lambdas.shared.session_manager import (
    create_session,
    generate_session_id,
    get_session_status,
    is_session_active,
    mark_session_abandoned,
    mark_session_complete,
    mark_session_timed_out,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_table(table_name: str = TABLE_NAME):
    """Create the DynamoDB table used by session_manager in the moto mock."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": PARTITION_KEY, "KeyType": "HASH"},
            {"AttributeName": SORT_KEY, "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": PARTITION_KEY, "AttributeType": "S"},
            {"AttributeName": SORT_KEY, "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(table_name)


# ---------------------------------------------------------------------------
# generate_session_id (Task 2.2)
# ---------------------------------------------------------------------------

class TestGenerateSessionId:
    def test_returns_valid_uuid4(self):
        sid = generate_session_id()
        parsed = uuid.UUID(sid, version=4)
        assert str(parsed) == sid

    def test_returns_unique_ids(self):
        ids = {generate_session_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------

@mock_aws
class TestCreateSession:
    def test_creates_sentinel_record(self):
        _create_table()
        sid = "test-session-1"
        record = create_session(sid, "What is X?")

        assert record.SessionID == sid
        assert record.SequenceNumber == SENTINEL_SEQUENCE_NUMBER
        assert record.SessionStatus == SessionStatus.ACTIVE
        assert record.QueryText == "What is X?"
        assert record.ResponseCount == 0
        assert record.CreatedAt > 0
        assert record.TTL == record.CreatedAt // 1000 + TTL_DURATION_SECONDS

    def test_prevents_duplicate_session(self):
        _create_table()
        sid = "dup-session"
        create_session(sid, "first")

        with pytest.raises(Exception) as exc_info:
            create_session(sid, "second")
        assert "ConditionalCheckFailedException" in str(type(exc_info.value).__name__)

    def test_custom_table_name(self):
        table_name = "CustomTable"
        _create_table(table_name)
        record = create_session("s1", "query", table_name=table_name)
        assert record.SessionID == "s1"


# ---------------------------------------------------------------------------
# get_session_status
# ---------------------------------------------------------------------------

@mock_aws
class TestGetSessionStatus:
    def test_returns_none_for_missing_session(self):
        _create_table()
        result = get_session_status("nonexistent")
        assert result is None

    def test_returns_sentinel_record(self):
        _create_table()
        create_session("s1", "hello")
        record = get_session_status("s1")

        assert record is not None
        assert record.SessionID == "s1"
        assert record.SessionStatus == SessionStatus.ACTIVE
        assert record.QueryText == "hello"
        assert record.SequenceNumber == SENTINEL_SEQUENCE_NUMBER


# ---------------------------------------------------------------------------
# mark_session_complete
# ---------------------------------------------------------------------------

@mock_aws
class TestMarkSessionComplete:
    def test_transitions_active_to_complete(self):
        _create_table()
        create_session("s1", "q")
        assert mark_session_complete("s1") is True

        record = get_session_status("s1")
        assert record.SessionStatus == SessionStatus.COMPLETE

    def test_fails_if_not_active(self):
        _create_table()
        create_session("s1", "q")
        mark_session_complete("s1")
        # Already COMPLETE — second call should fail
        assert mark_session_complete("s1") is False


# ---------------------------------------------------------------------------
# mark_session_abandoned
# ---------------------------------------------------------------------------

@mock_aws
class TestMarkSessionAbandoned:
    def test_transitions_active_to_abandoned(self):
        _create_table()
        create_session("s1", "q")
        assert mark_session_abandoned("s1") is True

        record = get_session_status("s1")
        assert record.SessionStatus == SessionStatus.ABANDONED

    def test_fails_if_already_complete(self):
        _create_table()
        create_session("s1", "q")
        mark_session_complete("s1")
        assert mark_session_abandoned("s1") is False


# ---------------------------------------------------------------------------
# mark_session_timed_out
# ---------------------------------------------------------------------------

@mock_aws
class TestMarkSessionTimedOut:
    def test_transitions_active_to_timed_out(self):
        _create_table()
        create_session("s1", "q")
        assert mark_session_timed_out("s1") is True

        record = get_session_status("s1")
        assert record.SessionStatus == SessionStatus.TIMED_OUT

    def test_fails_if_already_abandoned(self):
        _create_table()
        create_session("s1", "q")
        mark_session_abandoned("s1")
        assert mark_session_timed_out("s1") is False


# ---------------------------------------------------------------------------
# is_session_active
# ---------------------------------------------------------------------------

@mock_aws
class TestIsSessionActive:
    def test_true_for_active_session(self):
        _create_table()
        create_session("s1", "q")
        assert is_session_active("s1") is True

    def test_false_for_completed_session(self):
        _create_table()
        create_session("s1", "q")
        mark_session_complete("s1")
        assert is_session_active("s1") is False

    def test_false_for_nonexistent_session(self):
        _create_table()
        assert is_session_active("nope") is False

    def test_false_for_abandoned_session(self):
        _create_table()
        create_session("s1", "q")
        mark_session_abandoned("s1")
        assert is_session_active("s1") is False

    def test_false_for_timed_out_session(self):
        _create_table()
        create_session("s1", "q")
        mark_session_timed_out("s1")
        assert is_session_active("s1") is False
