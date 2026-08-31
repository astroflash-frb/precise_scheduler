"""Tests for precise_scheduler.alerts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from precise_scheduler.alerts import (
    calendar_read_failure,
    deadline_alert,
    git_push_failure,
    planobs_failure,
    post_alert,
    upload_failure,
)


# ---------------------------------------------------------------------------
# post_alert
# ---------------------------------------------------------------------------


class TestPostAlert:
    def test_post_alert_calls_slack(self) -> None:
        """chat_postMessage must be called with the message in the text argument."""
        client = MagicMock()
        channel = "C123456"
        message = "Test alert message"

        post_alert(message, client, channel)

        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args
        assert call_kwargs.kwargs.get("text") == message or (
            len(call_kwargs.args) >= 1 and message in str(call_kwargs.args)
        )
        # Verify positional-or-keyword 'channel' and 'text' are correct
        assert client.chat_postMessage.call_args.kwargs["text"] == message
        assert client.chat_postMessage.call_args.kwargs["channel"] == channel

    def test_post_alert_does_not_raise_on_slack_error(self) -> None:
        """When the Slack client raises SlackApiError, post_alert must return silently."""
        client = MagicMock()
        client.chat_postMessage.side_effect = SlackApiError(
            message="channel_not_found",
            response={"error": "channel_not_found"},
        )

        # Must not propagate
        post_alert("some alert", client, "C_BAD")

    def test_post_alert_does_not_raise_on_unexpected_error(self) -> None:
        """Any other exception from the client must also be swallowed."""
        client = MagicMock()
        client.chat_postMessage.side_effect = RuntimeError("network timeout")

        post_alert("some alert", client, "C123")

    def test_post_alert_logs_error_on_slack_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        """A Slack failure should be logged at ERROR level."""
        import logging

        client = MagicMock()
        client.chat_postMessage.side_effect = SlackApiError(
            message="invalid_auth",
            response={"error": "invalid_auth"},
        )

        with caplog.at_level(logging.ERROR, logger="precise_scheduler.alerts"):
            post_alert("alert text", client, "C_FAIL")

        assert any("invalid_auth" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# calendar_read_failure
# ---------------------------------------------------------------------------


class TestCalendarReadFailure:
    def test_message_contains_calendar(self) -> None:
        msg = calendar_read_failure()
        assert "calendar" in msg.lower()

    def test_message_contains_details_when_provided(self) -> None:
        msg = calendar_read_failure(details="HTTP 403 Forbidden")
        assert "HTTP 403 Forbidden" in msg

    def test_message_without_details(self) -> None:
        msg = calendar_read_failure()
        assert isinstance(msg, str)
        assert len(msg) > 0


# ---------------------------------------------------------------------------
# planobs_failure
# ---------------------------------------------------------------------------


class TestPlanobsFailure:
    def test_message_contains_source_names(self) -> None:
        sources = ["FRB20201124A", "FRB20190520B"]
        msg = planobs_failure(sources)
        assert "FRB20201124A" in msg
        assert "FRB20190520B" in msg

    def test_message_with_single_source(self) -> None:
        msg = planobs_failure(["FRB20201124A"])
        assert "FRB20201124A" in msg

    def test_message_with_empty_source_list(self) -> None:
        msg = planobs_failure([])
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_message_contains_details_when_provided(self) -> None:
        msg = planobs_failure(["FRB20201124A"], details="exit code 1")
        assert "exit code 1" in msg


# ---------------------------------------------------------------------------
# upload_failure
# ---------------------------------------------------------------------------


class TestUploadFailure:
    def test_message_contains_project_name(self) -> None:
        msg = upload_failure("RP036A")
        assert "RP036A" in msg

    def test_message_mentions_vlbeer(self) -> None:
        msg = upload_failure("RP036A")
        assert "vlbeer" in msg.lower()

    def test_message_contains_details_when_provided(self) -> None:
        msg = upload_failure("RP036A", details="Connection refused")
        assert "Connection refused" in msg

    def test_message_without_details(self) -> None:
        msg = upload_failure("RP036A")
        assert isinstance(msg, str)
        assert len(msg) > 0


# ---------------------------------------------------------------------------
# git_push_failure
# ---------------------------------------------------------------------------


class TestGitPushFailure:
    def test_message_contains_project_name(self) -> None:
        msg = git_push_failure("RP036A")
        assert "RP036A" in msg

    def test_message_mentions_git(self) -> None:
        msg = git_push_failure("RP036A")
        assert "git" in msg.lower() or "push" in msg.lower()

    def test_message_contains_details_when_provided(self) -> None:
        msg = git_push_failure("RP036A", details="authentication failed")
        assert "authentication failed" in msg


# ---------------------------------------------------------------------------
# deadline_alert
# ---------------------------------------------------------------------------


class TestDeadlineAlert:
    def test_message_contains_project_name(self) -> None:
        msg = deadline_alert("RP036A", days_remaining=2)
        assert "RP036A" in msg

    def test_message_contains_days_remaining(self) -> None:
        msg = deadline_alert("RP036A", days_remaining=3)
        assert "3" in msg

    def test_message_with_one_day(self) -> None:
        msg = deadline_alert("RP036A", days_remaining=1)
        assert "1" in msg
        assert isinstance(msg, str)

    def test_message_with_zero_days(self) -> None:
        msg = deadline_alert("RP036A", days_remaining=0)
        assert "RP036A" in msg
        assert "0" in msg

    def test_message_contains_warning_indicator(self) -> None:
        """The deadline alert should include a visible warning (emoji or keyword)."""
        msg = deadline_alert("RP036A", days_remaining=2)
        assert "⚠️" in msg or "warn" in msg.lower() or "not been confirmed" in msg.lower()
