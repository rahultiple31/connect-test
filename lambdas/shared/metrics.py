"""Shared CloudWatch metrics publishing for all Lambdas.

Publishes metrics to the ``ConnectAsyncMultiResponse`` namespace with
a ``SessionID`` dimension for per-session drill-down.
"""

from __future__ import annotations

import logging

import boto3

logger = logging.getLogger(__name__)

NAMESPACE = "ConnectAsyncMultiResponse"

_cloudwatch = None


def _get_cloudwatch():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client("cloudwatch")
    return _cloudwatch


def publish_metric(
    metric_name: str,
    session_id: str,
    value: float = 1.0,
    unit: str = "Count",
) -> None:
    """Publish a CloudWatch metric with a ``SessionID`` dimension.

    Parameters
    ----------
    metric_name:
        The metric name (e.g. ``CallbackReceived``, ``PollIteration``).
    session_id:
        Session identifier used as the ``SessionID`` dimension value.
    value:
        Metric value (default ``1.0``).
    unit:
        CloudWatch unit (default ``"Count"``).
    """
    try:
        _get_cloudwatch().put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": "SessionID", "Value": session_id},
                    ],
                    "Value": value,
                    "Unit": unit,
                },
            ],
        )
    except Exception:
        logger.exception("Failed to publish metric %s for session %s", metric_name, session_id)
