"""Submit Lambda / submit_query MCP tool handler.

Creates a session in DynamoDB and submits the caller's query to the
Nexthink agent with the callback URL.

Two outbound backends, selected by ``Config.NEXTHINK_BACKEND``:

- ``mock`` (default): plain JSON POST ``{"query", "sessionId", "callbackUrl"}``
  to ``NEXTHINK_AGENT_URL`` — the Bedrock-backed mock agent, or any
  endpoint that speaks that simple contract.
- ``spark``: Nexthink Spark **A2A** API. Two-step: OAuth2 client-credentials
  token, then ``message:send`` with our callback URL in
  ``configuration.pushNotification``. See :func:`_build_spark_payload`.

The inbound side (callback Lambda) needs no switch — it sniffs the payload
shape and translates Spark's ``statusUpdate`` envelope automatically.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from botocore.exceptions import ClientError

from lambdas.shared.config import Config, get_config
from lambdas.shared.metrics import publish_metric
from lambdas.shared.models import SubmitResult
from lambdas.shared.session_manager import create_session, generate_session_id

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Nexthink Spark (A2A) backend
# ---------------------------------------------------------------------------


def _is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _coerce_to_uuid(session_id: str) -> str:
    """Spark requires ``contextId`` to be a UUID; derive one if needed.

    ``uuid5`` is deterministic BY DESIGN. If the Orchestrator retries
    ``submit_query`` with the same non-UUID ``sessionId`` after a transient
    failure where the first call actually went through, the coerced UUID is
    identical, ``create_session``'s conditional write fails, and no second
    Spark task is created. ``uuid4()`` here would turn every retry into a
    duplicate submission. Callers must poll with the ``sessionId`` returned in
    :class:`SubmitResult` — the Orchestrator prompt enforces this.
    """
    if _is_valid_uuid(session_id):
        return session_id
    coerced = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id))
    logger.info("Coerced non-UUID session_id to %s", coerced)
    return coerced


def _get_nexthink_token(config: Config) -> str:
    """OAuth2 client-credentials exchange → bearer token.

    ``config.NEXTHINK_API_KEY`` must hold the HTTP Basic credential, i.e.
    ``base64("<client_id>:<client_secret>")``.

    ponytail: no token caching — one token request per submit. Fine for
    IVR call volumes; add expiry-aware caching if this ever runs hot.
    """
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "service:integration",
    }).encode("utf-8")
    req = urllib.request.Request(
        config.NEXTHINK_TOKEN_URL,
        data=data,
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {config.NEXTHINK_API_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    token = body.get("access_token")
    if not token:
        raise RuntimeError("No access_token in OAuth response")
    logger.info("Nexthink OAuth token acquired")
    return token


def _build_spark_payload(
    config: Config,
    transcript: str,
    session_id: str,
    callback_url: str,
) -> dict:
    """Build the A2A ``message:send`` envelope.

    ``message.contextId`` is the correlation key: Spark echoes it as
    ``statusUpdate.contextId`` in every callback, and the callback Lambda maps
    it back to our ``sessionId``. ``configuration.pushNotification.url`` is
    where Spark will POST those callbacks.
    """
    return {
        "tenant": config.NEXTHINK_TENANT_ID,
        "message": {
            "messageId": str(uuid.uuid4()),
            "contextId": session_id,
            "taskId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "content": [{"text": transcript}],
            "metadata": {"UserPrincipalName": config.NEXTHINK_USER_PRINCIPAL},
            "extensions": [],
            "referenceTaskIds": [],
        },
        "configuration": {
            "acceptedOutputModes": ["text"],
            "pushNotification": {
                "id": session_id,
                "url": callback_url,
                # NOTE: Spark does NOT replay this as an ``x-api-key`` header
                # on callbacks, so API Gateway key auth cannot rely on it.
                "token": config.CALLBACK_API_KEY,
            },
            "historyLength": 1,
            "blocking": False,
        },
    }


def _send_to_spark(
    config: Config,
    transcript: str,
    session_id: str,
    callback_url: str,
) -> None:
    """Token → ``message:send``. Raises on any failure."""
    token = _get_nexthink_token(config)
    payload = _build_spark_payload(config, transcript, session_id, callback_url)
    req = urllib.request.Request(
        config.NEXTHINK_SPARK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept-Language": "en",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Timezone": "GMT",
            "User-Principal-Name": config.NEXTHINK_USER_PRINCIPAL,
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        logger.info("Spark API accepted: session_id=%s status=%d", session_id, resp.status)


def _submit_via_spark(
    config: Config,
    transcript: str,
    session_id: str,
    callback_url: str,
    table_name: str | None,
) -> SubmitResult:
    """Spark backend wrapper — same outcomes/cleanup contract as the mock path.

    Catches broadly on purpose: whatever fails (auth, network, malformed token
    response), the orphaned ACTIVE session must be removed so the caller is
    not left polling a session nothing will ever answer.
    """
    try:
        _send_to_spark(config, transcript, session_id, callback_url)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:500]
        logger.error(
            "Spark API HTTP error: session_id=%s status=%d body=%s",
            session_id, exc.code, error_body,
        )
        _cleanup_orphaned_session(session_id, table_name)
        publish_metric("QueryFailed", session_id)
        return SubmitResult(
            status="error",
            sessionId=session_id,
            message=f"Nexthink returned HTTP {exc.code}",
        )
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.exception("Spark submit failed: session_id=%s", session_id)
        _cleanup_orphaned_session(session_id, table_name)
        publish_metric("QueryFailed", session_id)
        return SubmitResult(
            status="error",
            sessionId=session_id,
            message=f"Nexthink error: {exc}",
        )

    publish_metric("QuerySubmitted", session_id)
    return SubmitResult(status="submitted", sessionId=session_id)


# ---------------------------------------------------------------------------
# Core submit logic
# ---------------------------------------------------------------------------


def submit_query(
    transcript: str,
    session_id: str,
    callback_url: str,
    table_name: str | None = None,
) -> SubmitResult:
    """Create a session and POST the query to the Nexthink agent.

    1. Validate inputs (non-empty transcript, sessionId, callbackUrl).
    2. Create session sentinel record in DynamoDB.
       (spark backend: sessionId is coerced to a UUID first — see
       :func:`_coerce_to_uuid` — because Spark requires a UUID ``contextId``
       and that is the key its callbacks will carry.)
    3. Send to the selected backend (``Config.NEXTHINK_BACKEND``).
    4. On Nexthink success (2xx): return status="submitted".
    5. On Nexthink failure: clean up orphaned session, return status="error".
    6. On DynamoDB failure: do NOT call Nexthink, return status="error".
    """
    # --- Input validation ---
    if not transcript or not transcript.strip():
        return SubmitResult(
            status="error",
            sessionId=session_id or "",
            message="Empty transcript",
        )
    if not session_id or not session_id.strip():
        session_id = generate_session_id()
    if not callback_url or not callback_url.strip():
        return SubmitResult(
            status="error",
            sessionId=session_id,
            message="Empty callbackUrl",
        )

    config = get_config()
    use_spark = config.NEXTHINK_BACKEND == "spark"

    if use_spark:
        session_id = _coerce_to_uuid(session_id)

    # --- Create session in DynamoDB ---
    try:
        create_session(session_id, transcript, table_name)
    except ClientError as exc:
        logger.exception("DynamoDB error creating session %s", session_id)
        return SubmitResult(
            status="error",
            sessionId=session_id,
            message=f"Session creation failed: {exc}",
        )

    if use_spark:
        return _submit_via_spark(config, transcript, session_id, callback_url, table_name)

    # --- POST to Nexthink agent (mock / simple-JSON backend) ---
    nexthink_url = config.NEXTHINK_AGENT_URL
    api_key = config.NEXTHINK_API_KEY

    payload = json.dumps({
        "query": transcript,
        "sessionId": session_id,
        "callbackUrl": callback_url,
    }).encode("utf-8")

    req = urllib.request.Request(
        nexthink_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            status_code = resp.status
            if 200 <= status_code < 300:
                logger.info(
                    "Nexthink accepted query: session_id=%s status=%d",
                    session_id,
                    status_code,
                )
                publish_metric("QuerySubmitted", session_id)
                return SubmitResult(status="submitted", sessionId=session_id)
            # Non-2xx — should not normally reach here since urlopen raises
            # on non-2xx, but handle defensively.
            _cleanup_orphaned_session(session_id, table_name)
            publish_metric("QueryFailed", session_id)
            return SubmitResult(
                status="error",
                sessionId=session_id,
                message=f"Nexthink returned HTTP {status_code}",
            )
    except urllib.error.HTTPError as exc:
        logger.exception(
            "Nexthink HTTP error: session_id=%s status=%d", session_id, exc.code
        )
        _cleanup_orphaned_session(session_id, table_name)
        publish_metric("QueryFailed", session_id)
        return SubmitResult(
            status="error",
            sessionId=session_id,
            message=f"Nexthink returned HTTP {exc.code}",
        )
    except (urllib.error.URLError, OSError) as exc:
        logger.exception(
            "Nexthink connection error: session_id=%s", session_id
        )
        _cleanup_orphaned_session(session_id, table_name)
        publish_metric("QueryFailed", session_id)
        return SubmitResult(
            status="error",
            sessionId=session_id,
            message=f"Connection error: {exc}",
        )


def _cleanup_orphaned_session(
    session_id: str,
    table_name: str | None = None,
) -> None:
    """Delete the sentinel record for an orphaned session.

    Called when the Nexthink POST fails after session creation,
    so we don't leave an ACTIVE session with no pending query.
    """
    from lambdas.shared.models import (
        PARTITION_KEY,
        SENTINEL_SEQUENCE_NUMBER,
        SORT_KEY,
        TABLE_NAME,
    )
    import boto3

    name = table_name or TABLE_NAME
    table = boto3.resource("dynamodb").Table(name)
    try:
        table.delete_item(
            Key={PARTITION_KEY: session_id, SORT_KEY: SENTINEL_SEQUENCE_NUMBER},
        )
        logger.info("Cleaned up orphaned session: %s", session_id)
    except ClientError:
        logger.exception("Failed to clean up orphaned session: %s", session_id)


# ---------------------------------------------------------------------------
# Option A format converter
# ---------------------------------------------------------------------------


def _to_contact_attributes(result: SubmitResult) -> dict:
    """Convert a :class:`SubmitResult` to a flat string key-value map.

    Contact flow Lambda integrations require all values to be strings.
    """
    attrs: dict[str, str] = {
        "status": result.status,
        "sessionId": result.sessionId,
    }
    if result.message:
        attrs["message"] = result.message
    return attrs


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for submit_query.

    Supports two invocation formats:
    - Option E (MCP tool): ``{ "transcript": "...", "sessionId": "...", "callbackUrl": "..." }``
    - Option A (contact flow): ``{ "Details": { "Parameters": { "transcript": "...", "sessionId": "...", "callbackUrl": "..." } } }``
    """
    logger.info("Submit handler invoked: event_keys=%s", list(event.keys()))

    # Detect format and extract parameters
    if "Details" in event:
        params = event["Details"].get("Parameters", {})
        transcript = params.get("transcript", "")
        session_id = params.get("sessionId", "")
        callback_url = params.get("callbackUrl", "")
        logger.info("Option A format: transcript_len=%d session_id=%s", len(transcript), session_id)
    else:
        transcript = event.get("transcript", "")
        session_id = event.get("sessionId", "")
        callback_url = event.get("callbackUrl", "")
        logger.info("Option E format: transcript_len=%d session_id=%s callbackUrl_from_event=%s",
                     len(transcript), session_id, callback_url[:80] if callback_url else "EMPTY")

    # Always use the CALLBACK_API_URL from environment — the Orchestrator
    # doesn't know the correct callback URL
    env_callback_url = os.environ.get("CALLBACK_API_URL", "")
    if env_callback_url:
        logger.info("Using CALLBACK_API_URL from env: %s", env_callback_url)
        callback_url = env_callback_url

    result = submit_query(transcript, session_id, callback_url)
    logger.info("Submit result: status=%s session_id=%s message=%s",
                result.status, result.sessionId, result.message or "none")

    # Route response format
    if "Details" in event:
        return _to_contact_attributes(result)
    return result.model_dump()
