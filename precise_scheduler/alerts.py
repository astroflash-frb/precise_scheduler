"""
Slack alert helper for PRECISE Scheduler.

All failure-mode alerts funnel through post_alert().
It never raises even if Slack is down — it logs and returns.
"""

from __future__ import annotations

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)


class AlertError(Exception):
    """Raised only when the caller explicitly needs to know about an alert failure."""


def post_alert(message: str, client: WebClient, channel_id: str) -> None:
    """Post a plain-text alert to the Slack scheduling channel.

    Never raises — on Slack failure, logs at ERROR and returns silently.
    """
    try:
        client.chat_postMessage(channel=channel_id, text=message)
    except SlackApiError as exc:
        log.error(
            "Failed to post Slack alert (channel=%s): %s — original message: %s",
            channel_id,
            exc.response["error"],
            message,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Unexpected error posting Slack alert (channel=%s): %s — original message: %s",
            channel_id,
            exc,
            message,
        )


# ---------------------------------------------------------------------------
# Convenience constructors — return formatted message strings
# ---------------------------------------------------------------------------


def calendar_read_failure(details: str = "") -> str:
    """Return an alert message for a Google Sheets calendar read failure."""
    msg = "Could not read Google Sheets calendar."
    if details:
        msg = f"{msg} Details: {details}"
    return msg


def planobs_failure(source_list: list[str], details: str = "") -> str:
    """Return an alert message for a planobs failure."""
    sources = ", ".join(source_list) if source_list else "<none>"
    msg = f"planobs failed for sources: {sources}."
    if details:
        msg = f"{msg} Details: {details}"
    return msg


def upload_failure(project: str, details: str = "") -> str:
    """Return an alert message for an SCP upload failure to vlbeer."""
    msg = f"SCP upload to vlbeer failed for project {project}."
    if details:
        msg = f"{msg} Details: {details}"
    return msg


def git_push_failure(project: str, details: str = "") -> str:
    """Return an alert message for a git push failure."""
    msg = f"Git push failed for project {project}."
    if details:
        msg = f"{msg} Details: {details}"
    return msg


def deadline_alert(project: str, days_remaining: int) -> str:
    """Return an alert message warning that a schedule has not been confirmed."""
    return (
        f"⚠️ Schedule for {project} has not been confirmed — "
        f"observation in {days_remaining} day{'s' if days_remaining != 1 else ''}."
    )
