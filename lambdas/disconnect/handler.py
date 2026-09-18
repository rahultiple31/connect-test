"""Disconnect handler Lambda.

Triggered when a caller disconnects during the polling loop.
Marks the session as ABANDONED and logs the disconnect event.
"""

from __future__ import annotations

import logging
import time

from lambdas.shared.metrics import publish_metric
from lambdas.shared.observability import build_observability_attributes
from lambdas.shared.session_manager import get_session_status, mark_session_abandoned

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for caller disconnect events.

    Reads ``sessionId`` from the contact attributes in the event,
    marks the session as abandoned, and logs the disconnect.

    Gracefully handles missing sessionId or already-abandoned sessions.
    """
    # Extract sessionId from contact attributes
    session_id = _extract_session_id(event)

    if not session_id:
        logger.warning("Disconnect event with no sessionId: %s", event)
        return {"status": "ignored", "reason": "no sessionId"}

    # Read session to compute elapsed time
    sentinel = get_session_status(session_id)

    if sentinel is None:
        logger.warning("Disconnect for unknown session: %s", session_id)
        return {"status": "ignored", "reason": "session not found"}

    elapsed = (time.time() * 1000 - sentinel.CreatedAt) / 1000.0

    # Attempt to mark session as abandoned
    transitioned = mark_session_abandoned(session_id)

    if transitioned:
        logger.info(
            "Caller disconnected: session_id=%s elapsed_s=%.1f status=ABANDONED",
            session_id,
            elapsed,
        )
        publish_metric("SessionAbandoned", session_id)
    else:
        # Session was already in a terminal state (COMPLETE, ABANDONED, TIMED_OUT)
        logger.info(
            "Disconnect for non-active session: session_id=%s "
            "current_status=%s elapsed_s=%.1f",
            session_id,
            sentinel.SessionStatus.value
            if hasattr(sentinel.SessionStatus, "value")
            else sentinel.SessionStatus,
            elapsed,
        )

    # Build observability attributes
    responses_delivered = sentinel.ResponseCount or 0
    final_status = "ABANDONED" if transitioned else (
        sentinel.SessionStatus.value
        if hasattr(sentinel.SessionStatus, "value")
        else str(sentinel.SessionStatus)
    )

    obs_attrs = build_observability_attributes(
        session_id=session_id,
        responses_delivered=responses_delivered,
        session_duration_s=elapsed,
        final_status=final_status,
    )

    return {"status": "processed", **obs_attrs}


def _extract_session_id(event: dict) -> str | None:
    """Extract sessionId from the Lambda event.

    Supports two formats:
    - Contact flow: ``event["Details"]["ContactData"]["Attributes"]["sessionId"]``
    - Direct invocation: ``event["sessionId"]``
    """
    # Contact flow format
    try:
        return event["Details"]["ContactData"]["Attributes"]["sessionId"] or None
    except (KeyError, TypeError):
        pass

    # Direct / test invocation
    return event.get("sessionId") or None
