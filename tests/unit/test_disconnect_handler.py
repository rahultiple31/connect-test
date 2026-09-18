"""Unit tests for lambdas/disconnect/handler.py."""

from __future__ import annotations

import time
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
from lambdas.shared.session_manager import create_session, mark_session_complete
from lambdas.disconnect.handler import handler, _extract_session_id


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


def _contact_flow_event(session_id: str) -> dict:
    """Build a Connect contact-flow disconnect event."""
    return {
        "Details": {
            "ContactData": {
                "Attributes": {"sessionId": session_id},
            },
        },
    }


# ---------------------------------------------------------------------------
# _extract_session_id
# ---------------------------------------------------------------------------


class TestExtractSessionId:
    def test_contact_flow_format(self):
        event = _contact_flow_event("sess-1")
        assert _extract_session_id(event) == "sess-1"

    def test_direct_invocation_format(self):
        event = {"sessionId": "sess-2"}
        assert _extract_session_id(event) == "sess-2"

    def test_missing_session_id_returns_none(self):
        assert _extract_session_id({}) is None

    def test_empty_session_id_returns_none(self):
        event = {"sessionId": ""}
        assert _extract_session_id(event) is None

    def test_empty_contact_attribute_returns_none(self):
        event = {"Details": {"ContactData": {"Attributes": {"sessionId": ""}}}}
        assert _extract_session_id(event) is None


# ---------------------------------------------------------------------------
# Disconnect handler — active session
# ---------------------------------------------------------------------------


@mock_aws
class TestDisconnectActiveSession:
    @patch("lambdas.disconnect.handler.publish_metric")
    def test_marks_session_abandoned(self, mock_metric):
        table = _create_table()
        create_session("sess-active", "test query")

        result = handler(_contact_flow_event("sess-active"), None)

        assert result["status"] == "processed"
        assert result["finalStatus"] == "ABANDONED"

        # Verify DynamoDB was updated
        item = table.get_item(
            Key={PARTITION_KEY: "sess-active", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item["SessionStatus"] == SessionStatus.ABANDONED.value

    @patch("lambdas.disconnect.handler.publish_metric")
    def test_publishes_abandoned_metric(self, mock_metric):
        _create_table()
        create_session("sess-metric", "test query")

        handler(_contact_flow_event("sess-metric"), None)

        mock_metric.assert_called_once_with("SessionAbandoned", "sess-metric")

    @patch("lambdas.disconnect.handler.publish_metric")
    def test_returns_observability_attributes(self, mock_metric):
        _create_table()
        create_session("sess-obs", "test query")

        result = handler(_contact_flow_event("sess-obs"), None)

        assert result["sessionId"] == "sess-obs"
        assert result["responsesDelivered"] == "0"
        assert result["finalStatus"] == "ABANDONED"
        assert "sessionDurationSeconds" in result


# ---------------------------------------------------------------------------
# Disconnect handler — already terminal session
# ---------------------------------------------------------------------------


@mock_aws
class TestDisconnectTerminalSession:
    @patch("lambdas.disconnect.handler.publish_metric")
    def test_already_complete_session(self, mock_metric):
        _create_table()
        create_session("sess-done", "test query")
        mark_session_complete("sess-done")

        result = handler(_contact_flow_event("sess-done"), None)

        assert result["status"] == "processed"
        assert result["finalStatus"] == "COMPLETE"
        # Should NOT publish SessionAbandoned metric
        mock_metric.assert_not_called()

    @patch("lambdas.disconnect.handler.publish_metric")
    def test_already_abandoned_session(self, mock_metric):
        from lambdas.shared.session_manager import mark_session_abandoned as abandon

        _create_table()
        create_session("sess-aban", "test query")
        abandon("sess-aban")

        result = handler(_contact_flow_event("sess-aban"), None)

        assert result["status"] == "processed"
        assert result["finalStatus"] == "ABANDONED"
        mock_metric.assert_not_called()


# ---------------------------------------------------------------------------
# Disconnect handler — edge cases
# ---------------------------------------------------------------------------


@mock_aws
class TestDisconnectEdgeCases:
    def test_missing_session_id(self):
        _create_table()
        result = handler({}, None)
        assert result["status"] == "ignored"
        assert result["reason"] == "no sessionId"

    def test_unknown_session_id(self):
        _create_table()
        result = handler(_contact_flow_event("nonexistent"), None)
        assert result["status"] == "ignored"
        assert result["reason"] == "session not found"

    @patch("lambdas.disconnect.handler.publish_metric")
    def test_direct_invocation_format(self, mock_metric):
        _create_table()
        create_session("sess-direct", "test query")

        result = handler({"sessionId": "sess-direct"}, None)

        assert result["status"] == "processed"
        assert result["finalStatus"] == "ABANDONED"
