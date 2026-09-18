"""Unit tests for lambdas/mock_nexthink/handler.py — mock Nexthink agent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from lambdas.mock_nexthink.handler import handler, _invoke_bedrock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_context():
    """Build a mock Lambda context with function_name."""
    ctx = MagicMock()
    ctx.function_name = "ConnectAsync-MockNexThink"
    return ctx


# ---------------------------------------------------------------------------
# Handler input validation (Phase 1 — missing fields return 400 before
# context is accessed, so context=None is fine here)
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_query_returns_400(self):
        event = {"sessionId": "s1", "callbackUrl": "https://cb.example.com"}
        result = handler(event, None)
        assert result["statusCode"] == 400

    def test_missing_session_id_returns_400(self):
        event = {"query": "help", "callbackUrl": "https://cb.example.com"}
        result = handler(event, None)
        assert result["statusCode"] == 400

    def test_missing_callback_url_returns_400(self):
        event = {"query": "help", "sessionId": "s1"}
        result = handler(event, None)
        assert result["statusCode"] == 400

    def test_api_gateway_proxy_format(self):
        body = json.dumps({"query": "help", "sessionId": "s1", "callbackUrl": "https://cb.example.com"})
        event = {"body": body}

        with patch("lambdas.mock_nexthink.handler.boto3.client") as mock_boto:
            result = handler(event, _mock_context())

        assert result["statusCode"] == 202

    def test_invalid_json_body_returns_400(self):
        event = {"body": "not-json{"}
        result = handler(event, None)
        assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Phase 1: valid request self-invokes and returns 202
# ---------------------------------------------------------------------------


class TestPhase1SelfInvoke:
    def test_returns_202_and_triggers_async(self):
        event = {"query": "My laptop is slow", "sessionId": "sess-1", "callbackUrl": "https://cb.example.com"}

        with patch("lambdas.mock_nexthink.handler.boto3.client") as mock_boto:
            mock_lambda = MagicMock()
            mock_boto.return_value = mock_lambda

            result = handler(event, _mock_context())

        assert result["statusCode"] == 202
        mock_lambda.invoke.assert_called_once()
        call_kwargs = mock_lambda.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == "ConnectAsync-MockNexThink"
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"])
        assert payload["_async"] is True
        assert payload["query"] == "My laptop is slow"
        assert payload["sessionId"] == "sess-1"


# ---------------------------------------------------------------------------
# Phase 2: async callback delivery (invoked with _async=True)
# ---------------------------------------------------------------------------


class TestPhase2AsyncCallbacks:
    @patch("lambdas.mock_nexthink.handler._send_callback")
    @patch("lambdas.mock_nexthink.handler._invoke_bedrock")
    @patch("lambdas.mock_nexthink.handler.RESPONSE_DELAY_S", 0)
    def test_sends_callbacks_for_each_part(self, mock_bedrock, mock_callback):
        mock_bedrock.return_value = ["Part 1", "Part 2", "Part 3"]

        event = {
            "_async": True,
            "query": "My laptop is slow",
            "sessionId": "sess-1",
            "callbackUrl": "https://cb.example.com/callback",
        }
        result = handler(event, None)

        assert result["status"] == "done"
        assert result["parts"] == 3

        # Verify 3 callbacks were sent
        assert mock_callback.call_count == 3

        # Verify sequence numbers and completion flags
        calls = mock_callback.call_args_list
        assert calls[0].args == ("https://cb.example.com/callback", "sess-1", 1, "Part 1", False)
        assert calls[1].args == ("https://cb.example.com/callback", "sess-1", 2, "Part 2", False)
        assert calls[2].args == ("https://cb.example.com/callback", "sess-1", 3, "Part 3", True)

    @patch("lambdas.mock_nexthink.handler._send_callback")
    @patch("lambdas.mock_nexthink.handler._invoke_bedrock")
    @patch("lambdas.mock_nexthink.handler.RESPONSE_DELAY_S", 0)
    def test_single_part_response(self, mock_bedrock, mock_callback):
        mock_bedrock.return_value = ["Only one part"]

        event = {
            "_async": True,
            "query": "quick question",
            "sessionId": "s1",
            "callbackUrl": "https://cb.example.com",
        }
        result = handler(event, None)

        assert result["status"] == "done"
        assert mock_callback.call_count >= 1
        # Last callback should be marked as complete
        last_call = mock_callback.call_args_list[-1]
        assert last_call.args[4] is True


# ---------------------------------------------------------------------------
# Phase 2: Bedrock failure
# ---------------------------------------------------------------------------


class TestBedrockFailure:
    @patch("lambdas.mock_nexthink.handler._invoke_bedrock")
    def test_bedrock_error_returns_error(self, mock_bedrock):
        mock_bedrock.side_effect = Exception("Bedrock unavailable")

        event = {
            "_async": True,
            "query": "help",
            "sessionId": "s1",
            "callbackUrl": "https://cb.example.com",
        }
        result = handler(event, None)

        assert "error" in result


class TestParseParts:
    """Bedrock reply → response parts. Observed live: Claude fences the JSON."""

    def test_bare_json_array(self):
        from lambdas.mock_nexthink.handler import _parse_parts
        assert _parse_parts('["a", "b", "c"]') == ["a", "b", "c"]

    def test_json_fenced_array_is_unwrapped(self):
        from lambdas.mock_nexthink.handler import _parse_parts
        raw = '```json\n[\n  "Part one.",\n  "Part two."\n]\n```'
        assert _parse_parts(raw) == ["Part one.", "Part two."]

    def test_plain_fence_without_language_tag(self):
        from lambdas.mock_nexthink.handler import _parse_parts
        assert _parse_parts('```\n["x", "y"]\n```') == ["x", "y"]

    def test_non_json_falls_back_to_paragraph_split(self):
        from lambdas.mock_nexthink.handler import _parse_parts
        assert _parse_parts("First para.\n\nSecond para.") == ["First para.", "Second para."]

    def test_fenced_output_never_leaks_syntax_to_tts(self):
        from lambdas.mock_nexthink.handler import _parse_parts
        raw = '```json\n["Close Outlook.", "Delete the .ost file."]\n```'
        for part in _parse_parts(raw):
            assert "```" not in part and "[" not in part and '"' not in part
