"""Observability helpers for contact attribute reporting.

Builds the contact-attribute dict that the Polling Lambda (Option A)
and the disconnect handler attach to the call for post-call analytics.
"""

from __future__ import annotations


def build_observability_attributes(
    session_id: str,
    responses_delivered: int,
    session_duration_s: float,
    final_status: str,
) -> dict:
    """Build a contact-attributes dict for observability.

    The returned dict contains string values suitable for Amazon Connect
    contact attributes (flat key-value map).

    Parameters
    ----------
    session_id:
        Unique session identifier.
    responses_delivered:
        Total number of responses delivered to the caller.
    session_duration_s:
        Total session duration in seconds.
    final_status:
        Final session status (e.g. ``COMPLETE``, ``ABANDONED``, ``TIMED_OUT``).
    """
    return {
        "sessionId": session_id,
        "responsesDelivered": str(responses_delivered),
        "sessionDurationSeconds": str(round(session_duration_s, 1)),
        "finalStatus": final_status,
    }
