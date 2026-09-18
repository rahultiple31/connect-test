"""Unit tests for lambdas/callback/handler.py — callback ingestion."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from lambdas.shared.models import (
    PARTITION_KEY,
    SENTINEL_SEQUENCE_NUMBER,
    SORT_KEY,
    TABLE_NAME,
    SessionStatus,
)
from lambdas.shared.session_manager import create_session, mark_session_abandoned
from lambdas.callback.handler import _redact_text, handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_table(table_name: str = TABLE_NAME):
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


def _api_event(body: dict | str | None = None) -> dict:
    """Build a minimal API Gateway proxy event."""
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"body": body}


def _valid_payload(session_id: str = "sess-1", seq: int = 1, **overrides) -> dict:
    base = {
        "sessionId": session_id,
        "sequenceNumber": seq,
        "responseText": "Hello from Nexthink",
        "isComplete": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Valid callback writes record to DynamoDB
# ---------------------------------------------------------------------------


@mock_aws
class TestValidCallback:
    def test_writes_record_to_dynamodb(self):
        table = _create_table()
        create_session("sess-1", "What is X?")

        event = _api_event(_valid_payload())
        resp = handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["message"] == "Response stored"

        # Verify DynamoDB record
        item = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 1}).get("Item")
        assert item is not None
        assert item["ResponseText"] == "Hello from Nexthink"
        assert item["IsComplete"] is False
        assert item["Delivered"] is False

    def test_updates_sentinel_response_count(self):
        table = _create_table()
        create_session("sess-1", "query")

        handler(_api_event(_valid_payload(seq=1)), None)
        handler(_api_event(_valid_payload(seq=2)), None)

        sentinel = table.get_item(
            Key={PARTITION_KEY: "sess-1", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert sentinel["ResponseCount"] == 2
        assert sentinel["LastResponseAt"] is not None

    def test_ttl_based_on_response_timestamp_not_sentinel(self):
        """TTL must be >= now + 24h, not sentinel CreatedAt + 24h."""
        table = _create_table()
        # Create session with an old CreatedAt (simulating a long-running session)
        import time as _time
        old_created = int((_time.time() - 3600) * 1000)  # 1 hour ago
        table.put_item(Item={
            PARTITION_KEY: "sess-old",
            SORT_KEY: SENTINEL_SEQUENCE_NUMBER,
            "SessionStatus": SessionStatus.ACTIVE.value,
            "QueryText": "test",
            "CreatedAt": old_created,
            "TTL": old_created // 1000 + 86400,
            "IsComplete": False,
            "Delivered": False,
            "ResponseCount": 0,
        })

        now_before = int(_time.time())
        handler(_api_event(_valid_payload(session_id="sess-old", seq=1)), None)
        now_after = int(_time.time())

        item = table.get_item(Key={PARTITION_KEY: "sess-old", SORT_KEY: 1}).get("Item")
        # TTL should be based on current time, not the old sentinel CreatedAt
        assert item["TTL"] >= now_before + 86400
        assert item["TTL"] <= now_after + 86400 + 1


# ---------------------------------------------------------------------------
# Missing / invalid fields returns 400
# ---------------------------------------------------------------------------


@mock_aws
class TestValidationErrors:
    def test_missing_session_id(self):
        _create_table()
        payload = {"sequenceNumber": 1, "responseText": "hi", "isComplete": False}
        resp = handler(_api_event(payload), None)
        assert resp["statusCode"] == 400

    def test_missing_response_text(self):
        _create_table()
        payload = {"sessionId": "s1", "sequenceNumber": 1, "isComplete": False}
        resp = handler(_api_event(payload), None)
        assert resp["statusCode"] == 400

    def test_invalid_sequence_number(self):
        _create_table()
        payload = {"sessionId": "s1", "sequenceNumber": 0, "responseText": "hi", "isComplete": False}
        resp = handler(_api_event(payload), None)
        assert resp["statusCode"] == 400

    def test_empty_body(self):
        resp = handler(_api_event(None), None)
        assert resp["statusCode"] == 400

    def test_invalid_json(self):
        resp = handler({"body": "not-json{"}, None)
        assert resp["statusCode"] == 400

    def test_empty_session_id(self):
        _create_table()
        payload = {"sessionId": "", "sequenceNumber": 1, "responseText": "hi", "isComplete": False}
        resp = handler(_api_event(payload), None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Unknown session returns 404
# ---------------------------------------------------------------------------


@mock_aws
class TestUnknownSession:
    def test_returns_404(self):
        _create_table()
        event = _api_event(_valid_payload(session_id="nonexistent"))
        resp = handler(event, None)
        assert resp["statusCode"] == 404
        body = json.loads(resp["body"])
        assert "not found" in body["error"].lower()


# ---------------------------------------------------------------------------
# Abandoned session returns 200 but doesn't write
# ---------------------------------------------------------------------------


@mock_aws
class TestAbandonedSession:
    def test_returns_200_but_no_record(self):
        table = _create_table()
        create_session("sess-1", "query")
        mark_session_abandoned("sess-1")

        event = _api_event(_valid_payload())
        resp = handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "discard" in body["message"].lower()

        # Verify no response record written (only sentinel at seq=0)
        result = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 1})
        assert result.get("Item") is None


# ---------------------------------------------------------------------------
# Duplicate sequence number returns 200 (idempotent)
# ---------------------------------------------------------------------------


@mock_aws
class TestDuplicateSequence:
    def test_returns_200_idempotent(self):
        _create_table()
        create_session("sess-1", "query")

        event = _api_event(_valid_payload())
        resp1 = handler(event, None)
        assert resp1["statusCode"] == 200

        # Send same payload again
        resp2 = handler(event, None)
        assert resp2["statusCode"] == 200
        body = json.loads(resp2["body"])
        assert "idempotent" in body["message"].lower()


# ---------------------------------------------------------------------------
# isComplete=true transitions session to COMPLETE
# ---------------------------------------------------------------------------


@mock_aws
class TestCompletionFlag:
    def test_transitions_session_to_complete(self):
        table = _create_table()
        create_session("sess-1", "query")

        event = _api_event(_valid_payload(isComplete=True))
        resp = handler(event, None)
        assert resp["statusCode"] == 200

        sentinel = table.get_item(
            Key={PARTITION_KEY: "sess-1", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert sentinel["SessionStatus"] == SessionStatus.COMPLETE.value

    def test_non_complete_keeps_session_active(self):
        table = _create_table()
        create_session("sess-1", "query")

        event = _api_event(_valid_payload(isComplete=False))
        handler(event, None)

        sentinel = table.get_item(
            Key={PARTITION_KEY: "sess-1", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert sentinel["SessionStatus"] == SessionStatus.ACTIVE.value


# ---------------------------------------------------------------------------
# Response text is redacted in logs
# ---------------------------------------------------------------------------


class TestRedactText:
    def test_short_text_unchanged(self):
        assert _redact_text("hello") == "hello"

    def test_exact_boundary(self):
        text = "a" * 50
        assert _redact_text(text) == text

    def test_long_text_truncated(self):
        text = "a" * 100
        result = _redact_text(text)
        assert result == "a" * 50 + "..."
        assert len(result) == 53

    def test_custom_max_len(self):
        result = _redact_text("abcdefghij", max_len=5)
        assert result == "abcde..."


@mock_aws
class TestLogRedaction:
    def test_response_text_not_in_logs(self, caplog):
        _create_table()
        create_session("sess-1", "query")

        long_text = "This is a very long response text that should be redacted " * 5
        event = _api_event(_valid_payload(responseText=long_text))

        with caplog.at_level(logging.INFO, logger="lambdas.callback.handler"):
            handler(event, None)

        # Full text should NOT appear in any log record
        for record in caplog.records:
            assert long_text not in record.getMessage()
