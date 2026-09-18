"""Pydantic data models and DynamoDB table schema constants for the async multi-response pattern.

Implements models matching the DynamoDB schema and API contracts
defined in the design document.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# DynamoDB Table Schema Constants
# ---------------------------------------------------------------------------

TABLE_NAME: str = os.environ.get("RESPONSE_TABLE_NAME", "AsyncResponseQueue")
PARTITION_KEY: str = "SessionID"
SORT_KEY: str = "SequenceNumber"
GSI_NAME: str = "SessionStatusIndex"
GSI_PARTITION_KEY: str = "SessionStatus"
GSI_SORT_KEY: str = "CreatedAt"
TTL_ATTRIBUTE: str = "TTL"
TTL_DURATION_SECONDS: int = 86400  # 24 hours
SENTINEL_SEQUENCE_NUMBER: int = 0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SessionStatus(str, Enum):
    """Session lifecycle states."""

    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"
    ABANDONED = "ABANDONED"
    TIMED_OUT = "TIMED_OUT"


# ---------------------------------------------------------------------------
# Callback Payload (Nexthink → Callback Endpoint)
# ---------------------------------------------------------------------------

class CallbackMetadata(BaseModel):
    """Optional metadata attached to a callback payload."""

    sourceAgent: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CallbackPayload(BaseModel):
    """Incoming payload from the Nexthink callback endpoint.

    Matches the request schema defined in the design document:
    sessionId, sequenceNumber (>= 1), responseText, isComplete, and
    optional metadata.
    """

    sessionId: str = Field(..., min_length=1)
    sequenceNumber: int = Field(..., ge=1)
    responseText: str = Field(..., min_length=1)
    isComplete: bool = False
    metadata: Optional[CallbackMetadata] = None


# ---------------------------------------------------------------------------
# DynamoDB Response Record
# ---------------------------------------------------------------------------

class ResponseRecord(BaseModel):
    """DynamoDB item model for the AsyncResponseQueue table.

    SequenceNumber = 0 is the session sentinel record containing session-level
    metadata (QueryText, LastResponseAt, ResponseCount).  SequenceNumber >= 1
    are individual response records.
    """

    SessionID: str
    SequenceNumber: int
    ResponseText: Optional[str] = None  # present for seq >= 1
    IsComplete: bool = False
    Delivered: bool = False
    SessionStatus: SessionStatus = SessionStatus.ACTIVE
    CreatedAt: int  # epoch milliseconds
    LastResponseAt: Optional[int] = None  # sentinel only
    ResponseCount: Optional[int] = None  # sentinel only
    QueryText: Optional[str] = None  # sentinel only
    TTL: int  # epoch seconds


# ---------------------------------------------------------------------------
# Lambda Return Types
# ---------------------------------------------------------------------------

class PollingResult(BaseModel):
    """Return type for the polling / check_responses Lambda.

    ``status`` indicates the current session state from the polling
    perspective:
    - ``processing`` – no new responses yet, session still active (Option E)
    - ``waiting``    – no new responses yet, session still active (Option A)
    - ``results``    – one or more new responses returned
    - ``complete``   – session complete, all responses delivered
    - ``error``      – an error occurred
    - ``retry``      – transient error, caller should retry
    """

    status: Literal["processing", "waiting", "results", "complete", "error", "retry"]
    responses: list[dict] = Field(default_factory=list)
    cursor: int = 0
    elapsedSeconds: float = 0.0
    message: Optional[str] = None
    initialTimeout: bool = False
    sessionTimeout: bool = False


class SubmitResult(BaseModel):
    """Return type for the submit_query Lambda."""

    status: Literal["submitted", "error"]
    sessionId: str
    message: Optional[str] = None
