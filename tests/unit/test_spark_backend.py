"""Nexthink Spark (A2A) backend — outbound envelope and inbound translation.

Covers the two functions that own the A2A protocol, plus the backend switch
in ``submit_query`` and the format sniff in the callback ``handler``.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from lambdas.callback import handler as callback_mod
from lambdas.callback.handler import _next_spark_seq, _translate_nexthink_payload
from lambdas.shared.config import Config
from lambdas.shared.models import (
    PARTITION_KEY,
    SENTINEL_SEQUENCE_NUMBER,
    SORT_KEY,
    TABLE_NAME,
    SessionStatus,
)
from lambdas.shared.session_manager import create_session
from lambdas.submit.handler import (
    _build_spark_payload,
    _coerce_to_uuid,
    submit_query,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spark_config(**overrides) -> Config:
    cfg = Config(
        NEXTHINK_BACKEND="spark",
        NEXTHINK_TENANT_ID="tenant-uuid",
        NEXTHINK_TOKEN_URL="https://login.example/token",
        NEXTHINK_SPARK_URL="https://api.example/message:send",
        NEXTHINK_USER_PRINCIPAL="svc@example.com",
    )
    cfg.NEXTHINK_API_KEY = "YmFzaWM="  # base64("basic")
    cfg.CALLBACK_API_KEY = "cb-key"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _spark_status_update(context_id: str, state: str, *texts: str) -> dict:
    return {
        "statusUpdate": {
            "taskId": "task-1",
            "contextId": context_id,
            "status": {
                "state": state,
                "message": {"content": [{"text": t} for t in texts]},
            },
        }
    }


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE_NAME,
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
    return ddb.Table(TABLE_NAME)


# ---------------------------------------------------------------------------
# Inbound: _translate_nexthink_payload (pure — no sequence number)
# ---------------------------------------------------------------------------


class TestTranslateNexthinkPayload:
    def test_maps_fields_and_joins_content_parts(self):
        out = _translate_nexthink_payload(
            _spark_status_update("ctx-1", "TASK_STATE_WORKING", "Hello", "world")
        )
        assert out == {
            "sessionId": "ctx-1",
            "responseText": "Hello world",
            "isComplete": False,
        }

    def test_does_not_assign_sequence_number(self):
        # Sequence allocation is _next_spark_seq's job (DynamoDB), not the translator's.
        out = _translate_nexthink_payload(_spark_status_update("ctx", "TASK_STATE_WORKING", "t"))
        assert "sequenceNumber" not in out

    @pytest.mark.parametrize(
        "state,expected",
        [
            ("TASK_STATE_WORKING", False),
            ("TASK_STATE_COMPLETED", True),
            ("TASK_STATE_FAILED", True),
            ("", True),
        ],
    )
    def test_is_complete_is_anything_but_working(self, state, expected):
        out = _translate_nexthink_payload(_spark_status_update("ctx", state, "t"))
        assert out["isComplete"] is expected

    def test_missing_context_id_returns_none(self):
        assert _translate_nexthink_payload(_spark_status_update("", "TASK_STATE_WORKING", "t")) is None

    def test_empty_text_returns_none(self):
        assert _translate_nexthink_payload(_spark_status_update("ctx", "TASK_STATE_WORKING")) is None
        assert _translate_nexthink_payload(_spark_status_update("ctx", "TASK_STATE_WORKING", "  ")) is None

    def test_malformed_envelope_returns_none_not_raise(self):
        assert _translate_nexthink_payload({"statusUpdate": "not-a-dict"}) is None


# ---------------------------------------------------------------------------
# Inbound: _next_spark_seq — DynamoDB atomic counter on the sentinel
# ---------------------------------------------------------------------------


@mock_aws
class TestNextSparkSeq:
    def test_monotonic_per_session_and_isolated_across_sessions(self):
        table = _create_table()
        create_session("A", "q")
        create_session("B", "q")

        a = [_next_spark_seq(table, "A") for _ in range(3)]
        b = [_next_spark_seq(table, "B")]
        assert a == [1, 2, 3]
        assert b == [1]

    def test_survives_fresh_module_state(self):
        # The whole point of the fix: numbering lives in DynamoDB, not in the
        # Lambda process. A "new container" (fresh module import) must continue
        # the sequence, not restart it at 1.
        import importlib
        table = _create_table()
        create_session("S", "q")
        assert _next_spark_seq(table, "S") == 1

        fresh = importlib.reload(callback_mod)  # simulate a cold start
        assert fresh._next_spark_seq(table, "S") == 2

    def test_unknown_session_returns_none_and_creates_nothing(self):
        table = _create_table()
        assert _next_spark_seq(table, "ghost") is None
        # Condition must prevent a phantom sentinel from being created
        assert "Item" not in table.get_item(
            Key={PARTITION_KEY: "ghost", SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        )

    def test_counter_persisted_on_sentinel(self):
        table = _create_table()
        create_session("P", "q")
        _next_spark_seq(table, "P")
        _next_spark_seq(table, "P")
        sentinel = table.get_item(Key={PARTITION_KEY: "P", SORT_KEY: SENTINEL_SEQUENCE_NUMBER})["Item"]
        assert int(sentinel["NextSeq"]) == 2
        # Existing sentinel fields untouched
        assert sentinel["SessionStatus"] == SessionStatus.ACTIVE.value


# ---------------------------------------------------------------------------
# Inbound: handler dispatch — Spark envelope lands in DynamoDB via native path
# ---------------------------------------------------------------------------


@mock_aws
class TestCallbackHandlerAcceptsSparkFormat:
    def test_spark_envelope_is_stored_and_completes_session(self):
        table = _create_table()
        sid = str(uuid.uuid4())
        create_session(sid, "q")

        r1 = callback_mod.handler(
            {"body": json.dumps(_spark_status_update(sid, "TASK_STATE_WORKING", "part one"))}, None
        )
        r2 = callback_mod.handler(
            {"body": json.dumps(_spark_status_update(sid, "TASK_STATE_COMPLETED", "part two"))}, None
        )
        assert r1["statusCode"] == 200 and r2["statusCode"] == 200

        items = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key(PARTITION_KEY).eq(sid)
        )["Items"]
        by_seq = {int(i[SORT_KEY]): i for i in items}
        assert by_seq[1]["ResponseText"] == "part one"
        assert by_seq[2]["ResponseText"] == "part two"
        assert by_seq[2]["IsComplete"] is True
        assert by_seq[SENTINEL_SEQUENCE_NUMBER]["SessionStatus"] == SessionStatus.COMPLETE.value

    def test_invalid_spark_envelope_is_400(self):
        _create_table()
        resp = callback_mod.handler(
            {"body": json.dumps(_spark_status_update("", "TASK_STATE_WORKING", "t"))}, None
        )
        assert resp["statusCode"] == 400
        assert "Nexthink" in json.loads(resp["body"])["error"]

    def test_spark_envelope_for_unknown_session_is_404(self):
        table = _create_table()
        resp = callback_mod.handler(
            {"body": json.dumps(_spark_status_update("nope", "TASK_STATE_WORKING", "t"))}, None
        )
        assert resp["statusCode"] == 404
        # And nothing was written for the unknown session
        assert table.scan()["Count"] == 0

    def test_native_format_still_bypasses_translation(self):
        _create_table()
        create_session("native-1", "q")
        with patch.object(callback_mod, "_translate_nexthink_payload") as spy:
            resp = callback_mod.handler(
                {"body": json.dumps({
                    "sessionId": "native-1", "sequenceNumber": 1,
                    "responseText": "hi", "isComplete": False,
                })}, None,
            )
        assert resp["statusCode"] == 200
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound: _build_spark_payload / _coerce_to_uuid
# ---------------------------------------------------------------------------


class TestBuildSparkPayload:
    def test_context_id_is_session_id_and_push_url_is_callback(self):
        cfg = _spark_config()
        p = _build_spark_payload(cfg, "reset my password", "sess-uuid", "https://cb.example/callback")

        assert p["tenant"] == "tenant-uuid"
        assert p["message"]["contextId"] == "sess-uuid"
        assert p["message"]["content"] == [{"text": "reset my password"}]
        assert p["message"]["metadata"]["UserPrincipalName"] == "svc@example.com"
        push = p["configuration"]["pushNotification"]
        assert push == {"id": "sess-uuid", "url": "https://cb.example/callback", "token": "cb-key"}
        assert p["configuration"]["blocking"] is False

    def test_message_and_task_ids_are_fresh_uuids(self):
        cfg = _spark_config()
        p1 = _build_spark_payload(cfg, "q", "s", "u")
        p2 = _build_spark_payload(cfg, "q", "s", "u")
        uuid.UUID(p1["message"]["messageId"])
        uuid.UUID(p1["message"]["taskId"])
        assert p1["message"]["messageId"] != p2["message"]["messageId"]


class TestCoerceToUuid:
    def test_valid_uuid_passes_through(self):
        u = str(uuid.uuid4())
        assert _coerce_to_uuid(u) == u

    def test_non_uuid_becomes_uuid_and_is_deterministic(self):
        # Determinism is intentional: a retried submit with the same sessionId must
        # map to the same session so it does NOT create a second Spark task.
        a = _coerce_to_uuid("contact-123")
        b = _coerce_to_uuid("contact-123")
        uuid.UUID(a)
        assert a == b
        assert a != _coerce_to_uuid("contact-124")


# ---------------------------------------------------------------------------
# Outbound: submit_query backend switch
# ---------------------------------------------------------------------------


def _resp(status: int, body: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.status = status
    m.read.return_value = json.dumps(body or {}).encode()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


@mock_aws
class TestSubmitQuerySparkSwitch:
    def test_spark_mode_does_token_then_send_and_keys_session_by_uuid(self):
        table = _create_table()
        cfg = _spark_config()

        with patch("lambdas.submit.handler.get_config", return_value=cfg), \
             patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [_resp(200, {"access_token": "tok"}), _resp(202)]
            result = submit_query("q", "contact-abc", "https://cb.example/callback")

        assert result.status == "submitted"
        uuid.UUID(result.sessionId)  # coerced

        # Two HTTP calls: token, then message:send with bearer
        assert mock_open.call_count == 2
        token_req, send_req = (c.args[0] for c in mock_open.call_args_list)
        assert token_req.full_url == cfg.NEXTHINK_TOKEN_URL
        assert token_req.get_header("Authorization") == "Basic YmFzaWM="
        assert send_req.full_url == cfg.NEXTHINK_SPARK_URL
        assert send_req.get_header("Authorization") == "Bearer tok"
        assert json.loads(send_req.data)["message"]["contextId"] == result.sessionId

        # Session sentinel exists under the coerced UUID
        item = table.get_item(
            Key={PARTITION_KEY: result.sessionId, SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        ).get("Item")
        assert item is not None and item["SessionStatus"] == SessionStatus.ACTIVE.value

    def test_spark_failure_cleans_up_orphaned_session(self):
        table = _create_table()
        cfg = _spark_config()

        with patch("lambdas.submit.handler.get_config", return_value=cfg), \
             patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [_resp(200, {"access_token": "tok"}), RuntimeError("boom")]
            result = submit_query("q", "contact-abc", "https://cb.example/callback")

        assert result.status == "error"
        assert "boom" in result.message
        assert "Item" not in table.get_item(
            Key={PARTITION_KEY: result.sessionId, SORT_KEY: SENTINEL_SEQUENCE_NUMBER}
        )

    def test_mock_mode_is_untouched_by_spark_code(self):
        _create_table()
        cfg = Config(NEXTHINK_BACKEND="mock", NEXTHINK_AGENT_URL="https://mock.example/agent")
        cfg.NEXTHINK_API_KEY = "k"

        with patch("lambdas.submit.handler.get_config", return_value=cfg), \
             patch("lambdas.submit.handler.urllib.request.urlopen") as mock_open, \
             patch("lambdas.submit.handler._send_to_spark") as spark_spy:
            mock_open.return_value = _resp(202)
            result = submit_query("q", "contact-abc", "https://cb.example/callback")

        assert result.status == "submitted"
        assert result.sessionId == "contact-abc"  # NOT coerced in mock mode
        spark_spy.assert_not_called()
        assert mock_open.call_count == 1
        assert json.loads(mock_open.call_args.args[0].data) == {
            "query": "q", "sessionId": "contact-abc", "callbackUrl": "https://cb.example/callback",
        }
