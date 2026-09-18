"""Unit tests for lambdas/submit/handler.py — submit_query MCP tool."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

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
from lambdas.submit.handler import (
    _to_contact_attributes,
    handler,
    submit_query,
)
from lambdas.shared.models import SubmitResult


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


def _mock_urlopen_success():
    """Return a context-manager mock that simulates a 202 response."""
    resp = MagicMock()
    resp.status = 202
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Successful submission creates session and returns "submitted"
# ---------------------------------------------------------------------------


@mock_aws
class TestSuccessfulSubmission:
    def test_creates_session_and_returns_submitted(self):
        table = _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "test-api-key"

            result = submit_query("How do I reset my password?", "sess-1", "https://callback.example.com")

        assert result.status == "submitted"
        assert result.sessionId == "sess-1"

        # Verify session sentinel was created in DynamoDB
        item = table.get_item(
            Key={PARTITION_KEY: "sess-1", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is not None
        assert item["SessionStatus"] == SessionStatus.ACTIVE.value

    def test_posts_correct_payload_to_nexthink(self):
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "my-secret-key"

            submit_query("My laptop is slow", "sess-2", "https://cb.example.com")

        # Verify the request was constructed correctly
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.full_url == "https://nexthink.example.com/agent"
        assert req.get_header("X-api-key") == "my-secret-key"
        assert req.get_header("Content-type") == "application/json"

        body = json.loads(req.data.decode("utf-8"))
        assert body["query"] == "My laptop is slow"
        assert body["sessionId"] == "sess-2"
        assert body["callbackUrl"] == "https://cb.example.com"


# ---------------------------------------------------------------------------
# Empty transcript returns error without creating session
# ---------------------------------------------------------------------------


@mock_aws
class TestEmptyTranscript:
    def test_empty_string_returns_error(self):
        table = _create_table()

        result = submit_query("", "sess-1", "https://callback.example.com")

        assert result.status == "error"
        assert "transcript" in result.message.lower()

        # Verify no session was created
        item = table.get_item(
            Key={PARTITION_KEY: "sess-1", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is None

    def test_whitespace_only_returns_error(self):
        table = _create_table()

        result = submit_query("   ", "sess-1", "https://callback.example.com")

        assert result.status == "error"

        item = table.get_item(
            Key={PARTITION_KEY: "sess-1", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is None

    def test_empty_session_id_auto_generates(self):
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "test-api-key"

            result = submit_query("valid query", "", "https://callback.example.com")

        assert result.status == "submitted"
        assert len(result.sessionId) > 0  # auto-generated UUID

    def test_whitespace_session_id_auto_generates(self):
        table = _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "test-api-key"

            result = submit_query("valid query", "   ", "https://callback.example.com")

        assert result.status == "submitted"
        assert len(result.sessionId.strip()) > 0

        # Verify session was created in DynamoDB with the auto-generated ID
        item = table.get_item(
            Key={PARTITION_KEY: result.sessionId, SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is not None
        assert item["SessionStatus"] == SessionStatus.ACTIVE.value

    def test_empty_callback_url_returns_error(self):
        _create_table()
        result = submit_query("valid query", "sess-1", "")
        assert result.status == "error"
        assert "callbackurl" in result.message.lower()


# ---------------------------------------------------------------------------
# Nexthink failure cleans up orphaned session
# ---------------------------------------------------------------------------


@mock_aws
class TestNexthinkFailureCleanup:
    def test_http_error_cleans_up_session(self):
        table = _create_table()

        import urllib.error
        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.side_effect = urllib.error.HTTPError(
                "https://nexthink.example.com", 500, "Internal Server Error", {}, None
            )
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "key"

            result = submit_query("query", "sess-fail", "https://cb.example.com")

        assert result.status == "error"
        assert "500" in result.message

        # Verify orphaned session was cleaned up
        item = table.get_item(
            Key={PARTITION_KEY: "sess-fail", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is None

    def test_connection_error_cleans_up_session(self):
        table = _create_table()

        import urllib.error
        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.side_effect = urllib.error.URLError("Connection refused")
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "key"

            result = submit_query("query", "sess-conn", "https://cb.example.com")

        assert result.status == "error"
        assert "connection" in result.message.lower()

        # Verify orphaned session was cleaned up
        item = table.get_item(
            Key={PARTITION_KEY: "sess-conn", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is None


# ---------------------------------------------------------------------------
# DynamoDB failure prevents Nexthink call
# ---------------------------------------------------------------------------


@mock_aws
class TestDynamoDBFailure:
    def test_session_creation_failure_does_not_call_nexthink(self):
        _create_table()

        with patch("lambdas.submit.handler.create_session") as mock_create, \
             patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open:
            mock_create.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "DDB down"}},
                "PutItem",
            )

            result = submit_query("query", "sess-ddb", "https://cb.example.com")

        assert result.status == "error"
        assert "session creation failed" in result.message.lower()

        # Nexthink should NOT have been called
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# Option A format returns flat string map
# ---------------------------------------------------------------------------


class TestOptionAFormat:
    def test_submitted_result_to_contact_attributes(self):
        result = SubmitResult(status="submitted", sessionId="sess-1")
        attrs = _to_contact_attributes(result)

        assert attrs["status"] == "submitted"
        assert attrs["sessionId"] == "sess-1"
        assert "message" not in attrs

    def test_error_result_to_contact_attributes(self):
        result = SubmitResult(status="error", sessionId="sess-1", message="Something broke")
        attrs = _to_contact_attributes(result)

        assert attrs["status"] == "error"
        assert attrs["sessionId"] == "sess-1"
        assert attrs["message"] == "Something broke"


# ---------------------------------------------------------------------------
# Option E format returns structured JSON
# ---------------------------------------------------------------------------


@mock_aws
class TestOptionEFormat:
    def test_handler_returns_structured_json(self):
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "key"

            event = {
                "transcript": "How do I reset my password?",
                "sessionId": "sess-e",
                "callbackUrl": "https://cb.example.com",
            }
            result = handler(event, None)

        assert result["status"] == "submitted"
        assert result["sessionId"] == "sess-e"
        assert isinstance(result, dict)

    def test_handler_option_e_error(self):
        _create_table()

        event = {"transcript": "", "sessionId": "sess-e", "callbackUrl": "https://cb.example.com"}
        result = handler(event, None)

        assert result["status"] == "error"
        assert "message" in result


# ---------------------------------------------------------------------------
# Option A handler routing
# ---------------------------------------------------------------------------


@mock_aws
class TestOptionAHandlerRouting:
    def test_handler_detects_contact_flow_format(self):
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "key"

            event = {
                "Details": {
                    "Parameters": {
                        "transcript": "My laptop is slow",
                        "sessionId": "sess-a",
                        "callbackUrl": "https://cb.example.com",
                    }
                }
            }
            result = handler(event, None)

        # Option A returns flat string map
        assert result["status"] == "submitted"
        assert result["sessionId"] == "sess-a"
        # All values should be strings
        for v in result.values():
            assert isinstance(v, str)

    def test_handler_option_a_empty_session_id_auto_generates(self):
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "key"

            event = {
                "Details": {
                    "Parameters": {
                        "transcript": "My laptop is slow",
                        "sessionId": "",
                        "callbackUrl": "https://cb.example.com",
                    }
                }
            }
            result = handler(event, None)

        assert result["status"] == "submitted"
        assert len(result["sessionId"]) > 0  # auto-generated UUID

    def test_handler_option_a_missing_transcript(self):
        _create_table()

        event = {
            "Details": {
                "Parameters": {
                    "transcript": "",
                    "sessionId": "sess-a",
                    "callbackUrl": "https://cb.example.com",
                }
            }
        }
        result = handler(event, None)

        assert result["status"] == "error"
        assert "message" in result


# ---------------------------------------------------------------------------
# Gateway flat-map event format compatibility
# ---------------------------------------------------------------------------


@mock_aws
class TestGatewayEventFormat:
    """Tests for AgentCore Gateway flat-map event format compatibility."""

    def test_flat_map_event_with_context_key(self):
        """Gateway events may include a context metadata object — handler should ignore it."""
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "test-api-key"

            event = {
                "transcript": "My laptop is slow",
                "sessionId": "gw-test-001",
                "callbackUrl": "https://example.com/callback",
                "context": {
                    "messageVersion": "1.0",
                    "awsRequestId": "req-123",
                    "mcpMessageId": "mcp-456",
                    "gatewayId": "gw-789",
                    "targetId": "target-abc",
                    "toolName": "submit-query_submit_query",
                },
            }
            result = handler(event, None)

        assert result["status"] == "submitted"
        assert result["sessionId"] == "gw-test-001"

    def test_flat_map_event_with_arbitrary_extra_keys(self):
        """Handler should ignore any unrecognized keys in the event."""
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "test-api-key"

            event = {
                "transcript": "VPN not working",
                "sessionId": "gw-test-002",
                "callbackUrl": "https://example.com/callback",
                "unknownKey": "some value",
                "anotherExtra": 42,
            }
            result = handler(event, None)

        assert result["status"] == "submitted"

    def test_flat_map_response_is_json_serializable(self):
        """Gateway expects Lambda to return a JSON-serializable dict."""
        _create_table()

        with patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler.get_config") as mock_cfg:
            mock_open.return_value = _mock_urlopen_success()
            cfg = mock_cfg.return_value
            cfg.NEXTHINK_AGENT_URL = "https://nexthink.example.com/agent"
            cfg.NEXTHINK_API_KEY = "test-api-key"

            event = {
                "transcript": "Outlook crashes",
                "sessionId": "gw-test-003",
                "callbackUrl": "https://example.com/callback",
            }
            result = handler(event, None)

        # Should not raise
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
