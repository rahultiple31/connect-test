"""Unit tests for lambdas/shared/models.py — data models and schema constants."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from lambdas.shared.models import (
    TABLE_NAME,
    PARTITION_KEY,
    SORT_KEY,
    GSI_NAME,
    GSI_PARTITION_KEY,
    GSI_SORT_KEY,
    TTL_ATTRIBUTE,
    TTL_DURATION_SECONDS,
    SENTINEL_SEQUENCE_NUMBER,
    SessionStatus,
    CallbackMetadata,
    CallbackPayload,
    ResponseRecord,
    PollingResult,
    SubmitResult,
)


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

class TestSchemaConstants:
    def test_default_table_name(self):
        assert TABLE_NAME == os.environ.get("RESPONSE_TABLE_NAME", "AsyncResponseQueue")

    def test_key_schema(self):
        assert PARTITION_KEY == "SessionID"
        assert SORT_KEY == "SequenceNumber"

    def test_gsi(self):
        assert GSI_NAME == "SessionStatusIndex"
        assert GSI_PARTITION_KEY == "SessionStatus"
        assert GSI_SORT_KEY == "CreatedAt"

    def test_ttl(self):
        assert TTL_ATTRIBUTE == "TTL"
        assert TTL_DURATION_SECONDS == 86400

    def test_sentinel(self):
        assert SENTINEL_SEQUENCE_NUMBER == 0


# ---------------------------------------------------------------------------
# SessionStatus enum
# ---------------------------------------------------------------------------

class TestSessionStatus:
    def test_values(self):
        assert SessionStatus.ACTIVE.value == "ACTIVE"
        assert SessionStatus.COMPLETE.value == "COMPLETE"
        assert SessionStatus.ABANDONED.value == "ABANDONED"
        assert SessionStatus.TIMED_OUT.value == "TIMED_OUT"

    def test_is_string_enum(self):
        assert isinstance(SessionStatus.ACTIVE, str)


# ---------------------------------------------------------------------------
# CallbackPayload
# ---------------------------------------------------------------------------

class TestCallbackPayload:
    def test_valid_minimal(self):
        p = CallbackPayload(sessionId="s1", sequenceNumber=1, responseText="hello")
        assert p.sessionId == "s1"
        assert p.sequenceNumber == 1
        assert p.responseText == "hello"
        assert p.isComplete is False
        assert p.metadata is None

    def test_valid_with_metadata(self):
        p = CallbackPayload(
            sessionId="s1",
            sequenceNumber=2,
            responseText="world",
            isComplete=True,
            metadata={"sourceAgent": "nexthink", "confidence": 0.95},
        )
        assert p.isComplete is True
        assert p.metadata.sourceAgent == "nexthink"
        assert p.metadata.confidence == 0.95

    def test_rejects_empty_session_id(self):
        with pytest.raises(ValidationError):
            CallbackPayload(sessionId="", sequenceNumber=1, responseText="hi")

    def test_rejects_empty_response_text(self):
        with pytest.raises(ValidationError):
            CallbackPayload(sessionId="s1", sequenceNumber=1, responseText="")

    def test_rejects_zero_sequence_number(self):
        with pytest.raises(ValidationError):
            CallbackPayload(sessionId="s1", sequenceNumber=0, responseText="hi")

    def test_rejects_negative_sequence_number(self):
        with pytest.raises(ValidationError):
            CallbackPayload(sessionId="s1", sequenceNumber=-1, responseText="hi")

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            CallbackPayload(
                sessionId="s1",
                sequenceNumber=1,
                responseText="hi",
                metadata={"confidence": 1.5},
            )
        with pytest.raises(ValidationError):
            CallbackPayload(
                sessionId="s1",
                sequenceNumber=1,
                responseText="hi",
                metadata={"confidence": -0.1},
            )


# ---------------------------------------------------------------------------
# ResponseRecord
# ---------------------------------------------------------------------------

class TestResponseRecord:
    def test_response_record(self):
        r = ResponseRecord(SessionID="s1", SequenceNumber=1, ResponseText="hi", CreatedAt=1000, TTL=87400)
        assert r.IsComplete is False
        assert r.Delivered is False
        assert r.SessionStatus == SessionStatus.ACTIVE

    def test_sentinel_record(self):
        r = ResponseRecord(
            SessionID="s1",
            SequenceNumber=0,
            CreatedAt=1000,
            TTL=87400,
            QueryText="What is X?",
            LastResponseAt=2000,
            ResponseCount=3,
        )
        assert r.ResponseText is None
        assert r.QueryText == "What is X?"


# ---------------------------------------------------------------------------
# PollingResult
# ---------------------------------------------------------------------------

class TestPollingResult:
    def test_defaults(self):
        p = PollingResult(status="processing")
        assert p.responses == []
        assert p.cursor == 0
        assert p.elapsedSeconds == 0.0
        assert p.message is None
        assert p.initialTimeout is False
        assert p.sessionTimeout is False

    def test_waiting_status(self):
        p = PollingResult(status="waiting")
        assert p.status == "waiting"

    def test_with_responses(self):
        p = PollingResult(
            status="results",
            responses=[{"sequenceNumber": 1, "text": "answer"}],
            cursor=1,
            elapsedSeconds=5.2,
        )
        assert len(p.responses) == 1
        assert p.cursor == 1

    def test_timeout_flags(self):
        p = PollingResult(status="error", initialTimeout=True)
        assert p.initialTimeout is True
        assert p.sessionTimeout is False

    def test_invalid_status(self):
        with pytest.raises(ValidationError):
            PollingResult(status="unknown")


# ---------------------------------------------------------------------------
# SubmitResult
# ---------------------------------------------------------------------------

class TestSubmitResult:
    def test_submitted(self):
        s = SubmitResult(status="submitted", sessionId="s1")
        assert s.message is None

    def test_error(self):
        s = SubmitResult(status="error", sessionId="s1", message="boom")
        assert s.message == "boom"

    def test_invalid_status(self):
        with pytest.raises(ValidationError):
            SubmitResult(status="pending", sessionId="s1")
