"""Unit tests for lambdas/polling/handler.py — polling / check_responses."""

from __future__ import annotations

import time
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from lambdas.shared.models import (
    PARTITION_KEY,
    SENTINEL_SEQUENCE_NUMBER,
    SORT_KEY,
    TABLE_NAME,
    SessionStatus,
)
from lambdas.shared.session_manager import (
    create_session,
    mark_session_complete,
)
from lambdas.polling.handler import (
    _to_contact_attributes,
    check_responses,
    handler,
)
from lambdas.shared.models import PollingResult


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


def _put_response(table, session_id: str, seq: int, text: str = "resp", delivered: bool = False):
    """Insert a response record into the table."""
    table.put_item(Item={
        PARTITION_KEY: session_id,
        SORT_KEY: seq,
        "ResponseText": text,
        "IsComplete": False,
        "Delivered": delivered,
        "SessionStatus": SessionStatus.ACTIVE.value,
        "CreatedAt": int(time.time() * 1000),
        "TTL": int(time.time()) + 86400,
    })


# ---------------------------------------------------------------------------
# Returns "results" with correct responses when undelivered records exist
# ---------------------------------------------------------------------------


@mock_aws
class TestResultsWithUndelivered:
    def test_returns_results_with_responses(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "First answer")
        _put_response(table, "sess-1", 2, "Second answer")

        result = check_responses("sess-1", 0)

        assert result.status == "results"
        assert len(result.responses) == 2
        assert result.responses[0]["sequenceNumber"] == 1
        assert result.responses[0]["text"] == "First answer"
        assert result.responses[1]["sequenceNumber"] == 2
        assert result.responses[1]["text"] == "Second answer"
        assert result.cursor == 2

    def test_cursor_filters_already_seen(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Old", delivered=True)
        _put_response(table, "sess-1", 2, "New")

        # cursor=1 means we already saw seq 1
        result = check_responses("sess-1", 1)

        assert result.status == "results"
        assert len(result.responses) == 1
        assert result.responses[0]["sequenceNumber"] == 2


# ---------------------------------------------------------------------------
# Returns "processing" when no undelivered records and session ACTIVE
# ---------------------------------------------------------------------------


@mock_aws
class TestLimitedDelivery:
    """Option A uses limit=1 to deliver one response per poll cycle."""

    def test_limit_one_returns_only_first_response(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "First")
        _put_response(table, "sess-1", 2, "Second")
        _put_response(table, "sess-1", 3, "Third")

        result = check_responses("sess-1", 0, limit=1)

        assert result.status == "results"
        assert len(result.responses) == 1
        assert result.responses[0]["sequenceNumber"] == 1
        assert result.responses[0]["text"] == "First"
        assert result.cursor == 1

    def test_limit_one_leaves_rest_undelivered(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "First")
        _put_response(table, "sess-1", 2, "Second")
        _put_response(table, "sess-1", 3, "Third")

        check_responses("sess-1", 0, limit=1)

        # seq 1 should be delivered, seq 2 and 3 should NOT
        item1 = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 1}).get("Item")
        item2 = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 2}).get("Item")
        item3 = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 3}).get("Item")
        assert item1["Delivered"] is True
        assert item2["Delivered"] is False
        assert item3["Delivered"] is False

    def test_no_limit_returns_all(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "First")
        _put_response(table, "sess-1", 2, "Second")

        result = check_responses("sess-1", 0)

        assert len(result.responses) == 2
        assert result.cursor == 2

    def test_option_a_handler_delivers_one_at_a_time(self):
        """Full handler test: Option A format gets one response per call."""
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "First")
        _put_response(table, "sess-1", 2, "Second")

        event = {
            "Details": {
                "Parameters": {
                    "sessionId": "sess-1",
                    "cursor": "0",
                }
            }
        }

        # First call — gets response 1
        result1 = handler(event, None)
        assert result1["status"] == "results"
        assert "First" in result1["responseText"]
        assert result1["cursor"] == "1"

        # Second call with updated cursor — gets response 2
        event["Details"]["Parameters"]["cursor"] = "1"
        result2 = handler(event, None)
        assert result2["status"] == "results"
        assert "Second" in result2["responseText"]
        assert result2["cursor"] == "2"


# ---------------------------------------------------------------------------
# Returns "processing" when no undelivered records and session ACTIVE
# ---------------------------------------------------------------------------


@mock_aws
class TestProcessingStatus:
    def test_returns_processing_when_active_no_responses(self):
        _create_table()
        create_session("sess-1", "query")

        result = check_responses("sess-1", 0)

        assert result.status == "processing"
        assert result.cursor == 0
        assert result.elapsedSeconds >= 0

    def test_returns_processing_when_all_delivered(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Already read", delivered=True)

        result = check_responses("sess-1", 0)

        assert result.status == "processing"


# ---------------------------------------------------------------------------
# Returns "complete" when session COMPLETE and no undelivered records
# ---------------------------------------------------------------------------


@mock_aws
class TestCompleteStatus:
    def test_returns_complete_when_session_done(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Final", delivered=True)
        mark_session_complete("sess-1")

        result = check_responses("sess-1", 1)

        assert result.status == "complete"
        assert result.elapsedSeconds >= 0


# ---------------------------------------------------------------------------
# Returns "error" for nonexistent session
# ---------------------------------------------------------------------------


@mock_aws
class TestErrorForMissingSession:
    def test_returns_error_for_unknown_session(self):
        _create_table()

        result = check_responses("nonexistent", 0)

        assert result.status == "error"
        assert "not found" in result.message.lower()


# ---------------------------------------------------------------------------
# Marks delivered records correctly
# ---------------------------------------------------------------------------


@mock_aws
class TestDeliveredMarking:
    def test_marks_records_as_delivered(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Answer 1")
        _put_response(table, "sess-1", 2, "Answer 2")

        check_responses("sess-1", 0)

        # Verify both records are now marked delivered
        item1 = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 1}).get("Item")
        item2 = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 2}).get("Item")
        assert item1["Delivered"] is True
        assert item2["Delivered"] is True

    def test_preserves_original_fields_after_delivery(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Important answer")

        check_responses("sess-1", 0)

        item = table.get_item(Key={PARTITION_KEY: "sess-1", SORT_KEY: 1}).get("Item")
        assert item["ResponseText"] == "Important answer"
        assert item[PARTITION_KEY] == "sess-1"
        assert item[SORT_KEY] == 1


# ---------------------------------------------------------------------------
# Returns correct elapsed time
# ---------------------------------------------------------------------------


@mock_aws
class TestElapsedTime:
    def test_elapsed_seconds_is_positive(self):
        _create_table()
        create_session("sess-1", "query")

        result = check_responses("sess-1", 0)

        assert result.elapsedSeconds >= 0

    def test_elapsed_seconds_in_results(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Answer")

        result = check_responses("sess-1", 0)

        assert result.status == "results"
        assert result.elapsedSeconds >= 0


# ---------------------------------------------------------------------------
# Sets initialTimeout when no responses within threshold
# ---------------------------------------------------------------------------


@mock_aws
class TestInitialTimeout:
    def test_initial_timeout_when_no_responses_and_elapsed_exceeds_threshold(self):
        _create_table()
        session_id = "sess-timeout"
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        tbl = dynamodb.Table(TABLE_NAME)
        old_created_at = int((time.time() - 60) * 1000)  # 60 seconds ago
        tbl.put_item(Item={
            PARTITION_KEY: session_id,
            SORT_KEY: SENTINEL_SEQUENCE_NUMBER,
            "SessionStatus": SessionStatus.ACTIVE.value,
            "QueryText": "test",
            "CreatedAt": old_created_at,
            "TTL": int(time.time()) + 86400,
            "IsComplete": False,
            "Delivered": False,
            "ResponseCount": 0,
        })

        with patch("lambdas.polling.handler.get_config") as mock_cfg:
            cfg = mock_cfg.return_value
            cfg.INITIAL_RESPONSE_TIMEOUT_S = 30
            cfg.MAX_SESSION_DURATION_S = 300

            result = check_responses(session_id, 0)

        assert result.status == "processing"
        assert result.initialTimeout is True
        assert result.sessionTimeout is False

    def test_no_initial_timeout_when_cursor_nonzero(self):
        """initialTimeout only applies when cursor=0 (no responses ever delivered)."""
        table = _create_table()
        session_id = "sess-timeout2"
        old_created_at = int((time.time() - 60) * 1000)
        table.put_item(Item={
            PARTITION_KEY: session_id,
            SORT_KEY: SENTINEL_SEQUENCE_NUMBER,
            "SessionStatus": SessionStatus.ACTIVE.value,
            "QueryText": "test",
            "CreatedAt": old_created_at,
            "TTL": int(time.time()) + 86400,
            "IsComplete": False,
            "Delivered": False,
            "ResponseCount": 0,
        })

        with patch("lambdas.polling.handler.get_config") as mock_cfg:
            cfg = mock_cfg.return_value
            cfg.INITIAL_RESPONSE_TIMEOUT_S = 30
            cfg.MAX_SESSION_DURATION_S = 300

            result = check_responses(session_id, 1)

        assert result.initialTimeout is False


# ---------------------------------------------------------------------------
# Sets sessionTimeout when session exceeds max duration
# ---------------------------------------------------------------------------


@mock_aws
class TestSessionTimeout:
    def test_session_timeout_when_elapsed_exceeds_max(self):
        table = _create_table()
        session_id = "sess-max-timeout"
        very_old = int((time.time() - 400) * 1000)  # 400 seconds ago
        table.put_item(Item={
            PARTITION_KEY: session_id,
            SORT_KEY: SENTINEL_SEQUENCE_NUMBER,
            "SessionStatus": SessionStatus.ACTIVE.value,
            "QueryText": "test",
            "CreatedAt": very_old,
            "TTL": int(time.time()) + 86400,
            "IsComplete": False,
            "Delivered": False,
            "ResponseCount": 0,
        })

        with patch("lambdas.polling.handler.get_config") as mock_cfg:
            cfg = mock_cfg.return_value
            cfg.INITIAL_RESPONSE_TIMEOUT_S = 30
            cfg.MAX_SESSION_DURATION_S = 300

            result = check_responses(session_id, 0)

        assert result.status == "processing"
        assert result.sessionTimeout is True


# ---------------------------------------------------------------------------
# Handles DynamoDB errors gracefully (returns "retry")
# ---------------------------------------------------------------------------


@mock_aws
class TestDynamoDBErrors:
    def test_returns_retry_on_session_read_error(self):
        _create_table()

        with patch("lambdas.polling.handler.get_session_status") as mock_get:
            mock_get.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                "GetItem",
            )
            result = check_responses("sess-1", 0)

        assert result.status == "retry"
        assert result.message is not None

    def test_returns_retry_on_query_error(self):
        _create_table()
        create_session("sess-1", "query")

        with patch("lambdas.polling.handler._get_table") as mock_table:
            mock_table.return_value.query.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "query failed"}},
                "Query",
            )
            result = check_responses("sess-1", 0)

        assert result.status == "retry"
        assert result.message is not None


# ---------------------------------------------------------------------------
# Option A format returns flat string map with "waiting" status
# ---------------------------------------------------------------------------


class TestOptionAFormat:
    def test_contact_attributes_from_results_with_ssml(self):
        result = PollingResult(
            status="results",
            responses=[{"sequenceNumber": 1, "text": "Hello"}],
            cursor=1,
            elapsedSeconds=5.0,
        )
        attrs = _to_contact_attributes(result)

        assert attrs["status"] == "results"
        assert attrs["cursor"] == "1"
        assert attrs["elapsedSeconds"] == "5.0"
        # Response text is now SSML-wrapped
        assert "<speak>" in attrs["responseText"]
        assert "Hello" in attrs["responseText"]
        assert attrs["isComplete"] == "false"

    def test_contact_attributes_from_complete(self):
        result = PollingResult(status="complete", cursor=3, elapsedSeconds=120.0)
        attrs = _to_contact_attributes(result)

        assert attrs["status"] == "complete"
        assert attrs["isComplete"] == "true"
        assert "responseText" not in attrs

    def test_contact_attributes_with_error(self):
        result = PollingResult(status="error", message="Session not found")
        attrs = _to_contact_attributes(result)

        assert attrs["status"] == "error"
        assert attrs["errorMessage"] == "Session not found"

    def test_processing_maps_to_waiting_for_option_a(self):
        """Option A contact flow expects 'waiting', not 'processing'."""
        result = PollingResult(
            status="processing",
            elapsedSeconds=5.0,
        )
        attrs = _to_contact_attributes(result)

        assert attrs["status"] == "waiting"

    def test_waiting_includes_hold_message(self):
        """Option A 'waiting' status should include a holdMessage."""
        result = PollingResult(
            status="processing",
            elapsedSeconds=5.0,
        )
        attrs = _to_contact_attributes(result)

        assert "holdMessage" in attrs
        assert len(attrs["holdMessage"]) > 0

    def test_contact_attributes_with_timeouts(self):
        result = PollingResult(
            status="processing",
            initialTimeout=True,
            sessionTimeout=True,
            elapsedSeconds=400.0,
        )
        attrs = _to_contact_attributes(result)

        assert attrs["initialTimeout"] == "true"
        assert attrs["sessionTimeout"] == "true"
        assert attrs["status"] == "waiting"

    def test_no_timeout_keys_when_false(self):
        result = PollingResult(status="processing")
        attrs = _to_contact_attributes(result)

        assert "initialTimeout" not in attrs
        assert "sessionTimeout" not in attrs


# ---------------------------------------------------------------------------
# Option E format returns structured JSON
# ---------------------------------------------------------------------------


@mock_aws
class TestOptionEFormat:
    def test_handler_returns_structured_json(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Answer")

        event = {"sessionId": "sess-1", "cursor": 0}
        result = handler(event, None)

        assert result["status"] == "results"
        assert isinstance(result["responses"], list)
        assert result["cursor"] == 1
        assert "elapsedSeconds" in result

    def test_handler_option_e_missing_session(self):
        _create_table()

        event = {"sessionId": "nope", "cursor": 0}
        result = handler(event, None)

        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Option A handler routing
# ---------------------------------------------------------------------------


@mock_aws
class TestOptionAHandlerRouting:
    def test_handler_detects_contact_flow_format(self):
        table = _create_table()
        create_session("sess-1", "query")
        _put_response(table, "sess-1", 1, "Answer")

        event = {
            "Details": {
                "Parameters": {
                    "sessionId": "sess-1",
                    "cursor": "0",
                }
            }
        }
        result = handler(event, None)

        # Option A returns flat string map
        assert isinstance(result["status"], str)
        assert result["status"] == "results"
        assert isinstance(result["cursor"], str)
        # Response text is now SSML-wrapped
        assert "<speak>" in result["responseText"]
        assert "Answer" in result["responseText"]

    def test_handler_waiting_status_for_option_a(self):
        """When no responses, Option A handler returns 'waiting' not 'processing'."""
        _create_table()
        create_session("sess-1", "query")

        event = {
            "Details": {
                "Parameters": {
                    "sessionId": "sess-1",
                    "cursor": "0",
                }
            }
        }
        result = handler(event, None)

        assert result["status"] == "waiting"
        assert "holdMessage" in result

    def test_handler_missing_session_id_returns_error(self):
        _create_table()

        event = {"sessionId": "", "cursor": 0}
        result = handler(event, None)

        assert result["status"] == "error"
        assert "Missing" in result.get("message", "")


# ---------------------------------------------------------------------------
# Gateway flat-map event format compatibility
# ---------------------------------------------------------------------------


@mock_aws
class TestGatewayEventFormat:
    """Tests for AgentCore Gateway flat-map event format compatibility."""

    def test_flat_map_event_with_context_key(self):
        """Gateway events may include a context metadata object — handler should ignore it."""
        table = _create_table()
        create_session("gw-sess-001", "query")
        _put_response(table, "gw-sess-001", 1, "First answer")

        event = {
            "sessionId": "gw-sess-001",
            "cursor": 0,
            "context": {
                "messageVersion": "1.0",
                "gatewayId": "gw-789",
                "toolName": "check-responses_check_responses",
            },
        }
        result = handler(event, None)

        assert result["status"] in ("processing", "results", "complete", "error")

    def test_flat_map_event_with_arbitrary_extra_keys(self):
        """Handler should ignore any unrecognized keys in the event."""
        _create_table()
        create_session("gw-sess-002", "query")

        event = {
            "sessionId": "gw-sess-002",
            "cursor": 0,
            "unknownKey": "value",
            "extra": True,
        }
        result = handler(event, None)

        assert "status" in result

    def test_flat_map_response_is_json_serializable(self):
        """Gateway expects Lambda to return a JSON-serializable dict."""
        import json

        _create_table()
        create_session("gw-sess-003", "query")

        event = {
            "sessionId": "gw-sess-003",
            "cursor": 0,
        }
        result = handler(event, None)

        serialized = json.dumps(result)
        assert isinstance(serialized, str)
