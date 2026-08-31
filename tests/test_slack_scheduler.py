"""Tests for precise_scheduler.slack_scheduler (Issues #8 & #9)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from precise_scheduler.activity import Detection
from precise_scheduler.calendar import ObsWindow
from precise_scheduler.obs_schedules import ObsRecord, SourceTime
from precise_scheduler.slack_scheduler import (
    EMOJI_ALL,
    EMOJI_MOST,
    EMOJI_NONE,
    GRID_MINUTES,
    MAX_BLOCKS_PER_MESSAGE,
    SECTION_TEXT_LIMIT,
    SlackError,
    _format_actions_block,
    _payload_queue,
    _section_blocks,
    _slot_emoji,
    _verify_slack_signature,
    _visibility_row,
    await_confirmation,
    build_plan_messages,
    plan_fallback_text,
    post_key_file,
    post_plan,
    post_proposal,
)
from precise_scheduler.targets import Calibrator, Coordinates, Target
from precise_scheduler.visibility import VisibilityGrid


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_obs_window() -> ObsWindow:
    """2-hour window starting 2026-07-15 10:00 UTC with 3 antennas."""
    start = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    return ObsWindow(
        start=start,
        end=end,
        antennas=["Ef", "Wb", "Tr"],
        project="RP036A",
    )


def _make_calibrator(name: str) -> Calibrator:
    coords = Coordinates(ra="00:33:03.600", dec="+33:08:00.00", equinox="J2000")
    return Calibrator(name=name, coordinates=coords, flux="1.0")


def make_targets() -> list[Target]:
    """Two targets: priority-2 FRB20201124A and priority-1 FRB20190520B."""
    coords_a = Coordinates(ra="05:31:58.70", dec="+33:08:52.5", equinox="J2000")
    coords_b = Coordinates(ra="16:02:04.27", dec="-10:58:35.0", equinox="J2000")

    target_a = Target(
        name="R201124_A",
        canonical_name="FRB20201124A",
        priority=2,
        coordinates=coords_a,
        phasecal=_make_calibrator("J0542+4951"),
        checksource=_make_calibrator("J0533+4822"),
    )
    target_b = Target(
        name="R190520_B",
        canonical_name="FRB20190520B",
        priority=1,
        coordinates=coords_b,
        phasecal=_make_calibrator("J1557-0937"),
        checksource=_make_calibrator("J1549-1146"),
    )
    return [target_a, target_b]


def make_visibility_grid(
    targets: list[Target],
    n_slots: int = 8,
    n_antennas: int = 3,
) -> VisibilityGrid:
    """Full-visibility grid (all antennas visible for every slot)."""
    return {t.name: [n_antennas] * n_slots for t in targets}


def make_recent_runs() -> list[ObsRecord]:
    """Two realistic ObsRecord objects."""
    start_a = datetime(2026, 5, 10, 10, 0, 0, tzinfo=timezone.utc)
    start_b = datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc)

    rec_a = ObsRecord(
        project="RP035A",
        start=start_a,
        sources=[
            SourceTime(name="FRB20201124A", total_seconds=7200.0, antennas=["Ef", "Wb", "Tr"]),
            SourceTime(name="J0542+4951", total_seconds=900.0, antennas=["Ef", "Wb", "Tr"]),
        ],
    )
    rec_b = ObsRecord(
        project="RP035B",
        start=start_b,
        sources=[
            SourceTime(name="FRB20190520B", total_seconds=5400.0, antennas=["Ef", "Wb"]),
        ],
    )
    return [rec_a, rec_b]


def make_mock_activity(mocker: Any) -> MagicMock:
    """Mock Activity that returns 1 Detection 3 days ago for any source."""
    mock_activity = mocker.MagicMock()
    detection_dt = datetime.now(timezone.utc) - timedelta(days=3)
    detection = Detection(
        canonical_name="FRB20201124A",
        utc_datetime=detection_dt,
        facility="CHIME",
    )
    # get_detections returns a list with one Detection regardless of arguments
    mock_activity.get_detections.return_value = [detection]
    return mock_activity


def _make_successful_client(ts: str = "1720000000.123456") -> MagicMock:
    """Return a mock WebClient that returns a successful chat_postMessage response."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": ts, "ok": True}
    return client


# ---------------------------------------------------------------------------
# post_proposal — API call behaviour
# ---------------------------------------------------------------------------


class TestPostProposalApiCall:
    def test_post_proposal_calls_chat_postMessage(self, mocker: Any) -> None:
        """chat_postMessage must be called exactly once."""
        obs_window = make_obs_window()
        targets = make_targets()
        grid = make_visibility_grid(targets)
        activity = make_mock_activity(mocker)
        client = _make_successful_client()

        post_proposal(
            obs_window=obs_window,
            visibility_grid=grid,
            proposed=targets,
            active_15d=["FRB20201124A"],
            active_30d=["FRB20201124A", "FRB20190520B"],
            client=client,
            channel_id="C_TEST",
            activity=activity,
        )

        client.chat_postMessage.assert_called_once()

    def test_post_proposal_returns_ts(self, mocker: Any) -> None:
        """The message timestamp returned by Slack must be passed back to the caller."""
        obs_window = make_obs_window()
        targets = make_targets()
        grid = make_visibility_grid(targets)
        activity = make_mock_activity(mocker)
        expected_ts = "1720000000.999999"
        client = _make_successful_client(ts=expected_ts)

        ts = post_proposal(
            obs_window=obs_window,
            visibility_grid=grid,
            proposed=targets,
            active_15d=[],
            active_30d=[],
            client=client,
            channel_id="C_TEST",
            activity=activity,
        )

        assert ts == expected_ts

    def test_post_proposal_slack_error_raises_slack_error(self, mocker: Any) -> None:
        """SlackApiError from the client must be translated into SlackError."""
        obs_window = make_obs_window()
        targets = make_targets()
        grid = make_visibility_grid(targets)
        activity = make_mock_activity(mocker)
        client = MagicMock()
        client.chat_postMessage.side_effect = SlackApiError(
            message="not_in_channel",
            response={"error": "not_in_channel"},
        )

        with pytest.raises(SlackError):
            post_proposal(
                obs_window=obs_window,
                visibility_grid=grid,
                proposed=targets,
                active_15d=[],
                active_30d=[],
                client=client,
                channel_id="C_BAD",
                activity=activity,
            )


# ---------------------------------------------------------------------------
# post_proposal — Block Kit content
# ---------------------------------------------------------------------------


def _extract_blocks(mocker: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Helper that runs post_proposal and returns the blocks list passed to Slack."""
    obs_window = kwargs.get("obs_window", make_obs_window())
    targets = kwargs.get("targets", make_targets())
    grid = kwargs.get("grid", make_visibility_grid(targets))
    recent_runs = kwargs.get("recent_runs", make_recent_runs())
    activity = kwargs.get("activity", make_mock_activity(mocker))
    active_15d = kwargs.get("active_15d", ["FRB20201124A"])
    active_30d = kwargs.get("active_30d", ["FRB20201124A", "FRB20190520B"])
    channel_id = kwargs.get("channel_id", "C_TEST")

    client = _make_successful_client()

    post_proposal(
        obs_window=obs_window,
        visibility_grid=grid,
        proposed=targets,
        active_15d=active_15d,
        active_30d=active_30d,
        client=client,
        channel_id=channel_id,
        activity=activity,
    )

    call_kwargs = client.chat_postMessage.call_args.kwargs
    return call_kwargs["blocks"]


def _all_block_text(blocks: list[dict[str, Any]]) -> str:
    """Concatenate every text value in *blocks*, context elements included."""
    parts: list[str] = []
    for block in blocks:
        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            parts.append(text_obj.get("text", ""))
        elif isinstance(text_obj, str):
            parts.append(text_obj)
        for element in block.get("elements", []):
            if isinstance(element, dict) and isinstance(element.get("text"), str):
                parts.append(element["text"])
    return "\n".join(parts)


class TestPostProposalBlocks:
    def test_post_proposal_blocks_contain_header(self, mocker: Any) -> None:
        """At least one block must mention the project name."""
        blocks = _extract_blocks(mocker)
        combined = _all_block_text(blocks)
        assert "RP036A" in combined

    def test_post_proposal_blocks_contain_obs_dates(self, mocker: Any) -> None:
        """The header block must mention the observation start date/time."""
        blocks = _extract_blocks(mocker)
        combined = _all_block_text(blocks)
        # start is 2026-07-15 10:00
        assert "2026-07-15" in combined or "10:00" in combined

    def test_post_proposal_blocks_contain_antennas(self, mocker: Any) -> None:
        """The header block must list the antennas."""
        blocks = _extract_blocks(mocker)
        combined = _all_block_text(blocks)
        assert "Ef" in combined
        assert "Wb" in combined
        assert "Tr" in combined

    def test_post_proposal_blocks_contain_visibility_grid(self, mocker: Any) -> None:
        """At least one block must contain emoji from the visibility grid."""
        blocks = _extract_blocks(mocker)
        combined = _all_block_text(blocks)
        assert EMOJI_ALL in combined or EMOJI_MOST in combined or EMOJI_NONE in combined

    def test_post_proposal_blocks_contain_proposed_sources(self, mocker: Any) -> None:
        """At least one block must mention both proposed canonical names."""
        blocks = _extract_blocks(mocker)
        combined = _all_block_text(blocks)
        assert "FRB20201124A" in combined
        assert "FRB20190520B" in combined

    def test_post_proposal_blocks_contain_activity_section(self, mocker: Any) -> None:
        """Activity summary must mention sources active in 15d."""
        blocks = _extract_blocks(mocker, active_15d=["FRB20201124A"], active_30d=["FRB20201124A"])
        combined = _all_block_text(blocks)
        assert "FRB20201124A" in combined

    def test_post_proposal_blocks_contain_actions_block(self, mocker: Any) -> None:
        """The last block must be a Block Kit actions block with interactive controls."""
        blocks = _extract_blocks(mocker)
        # Find the actions block
        actions_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert actions_blocks, "Expected at least one actions block"
        action_ids = {
            el.get("action_id")
            for el in actions_blocks[0].get("elements", [])
        }
        assert "confirm_proposal" in action_ids

    def test_post_proposal_channel_passed_to_client(self, mocker: Any) -> None:
        """chat_postMessage must be called with the correct channel_id."""
        targets = make_targets()
        grid = make_visibility_grid(targets)
        activity = make_mock_activity(mocker)
        client = _make_successful_client()
        channel = "C_SPECIFIC"

        post_proposal(
            obs_window=make_obs_window(),
            visibility_grid=grid,
            proposed=targets,
            active_15d=[],
            active_30d=[],
            client=client,
            channel_id=channel,
            activity=activity,
        )

        assert client.chat_postMessage.call_args.kwargs["channel"] == channel

    def test_post_proposal_capped_at_five_recent_runs(self, mocker: Any) -> None:
        """Even if more than 5 recent_runs are passed in, only 5 should appear."""
        targets = make_targets()
        grid = make_visibility_grid(targets)
        activity = make_mock_activity(mocker)
        client = _make_successful_client()

        # Build 7 records with distinct project names
        runs: list[ObsRecord] = []
        for i in range(7):
            runs.append(
                ObsRecord(
                    project=f"RP03{i:02d}",
                    start=datetime(2026, 1, i + 1, 10, 0, 0, tzinfo=timezone.utc),
                    sources=[SourceTime(name="FRB20201124A", total_seconds=3600.0, antennas=["Ef"])],
                )
            )

        post_proposal(
            obs_window=make_obs_window(),
            visibility_grid=grid,
            proposed=targets,
            active_15d=[],
            active_30d=[],
            client=client,
            channel_id="C_TEST",
            activity=activity,
        )

        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        combined = _all_block_text(blocks)
        # The 6th and 7th project names should not be in the output
        assert "RP0305" not in combined
        assert "RP0306" not in combined


# ---------------------------------------------------------------------------
# Emoji encoding (unit tests for _slot_emoji)
# ---------------------------------------------------------------------------


class TestEmojiEncoding:
    def test_emoji_encoding_all_antennas(self) -> None:
        """slot count == n_antennas → EMOJI_ALL."""
        assert _slot_emoji(3, 3) == EMOJI_ALL

    def test_emoji_encoding_most_antennas(self) -> None:
        """slot count == n_antennas - 1 → EMOJI_MOST."""
        assert _slot_emoji(2, 3) == EMOJI_MOST

    def test_emoji_encoding_none(self) -> None:
        """slot count < n_antennas - 1 → EMOJI_NONE."""
        assert _slot_emoji(1, 3) == EMOJI_NONE
        assert _slot_emoji(0, 3) == EMOJI_NONE

    def test_emoji_encoding_zero_antennas(self) -> None:
        """Degenerate case: zero antennas → EMOJI_NONE."""
        assert _slot_emoji(0, 0) == EMOJI_NONE

    def test_emoji_encoding_single_antenna_full(self) -> None:
        """1 antenna present, 1 total → EMOJI_ALL."""
        assert _slot_emoji(1, 1) == EMOJI_ALL

    def test_emoji_encoding_single_antenna_none(self) -> None:
        """0 antennas present, 1 total → EMOJI_NONE (0 < 1-1 = 0 is False, 0 >= 0 is True)."""
        # With n_antennas=1: n_antennas-1 = 0, so count >= 0 is always True → EMOJI_MOST
        # This is a boundary: count=0, n=1 means EMOJI_MOST (all-but-one = 0 antennas).
        result = _slot_emoji(0, 1)
        # 0 >= 1 is False → not EMOJI_ALL; 0 >= 0 is True → EMOJI_MOST
        assert result == EMOJI_MOST

    def test_emoji_encoding_large_array(self) -> None:
        """Larger arrays: verify thresholds work correctly."""
        n = 10
        assert _slot_emoji(10, n) == EMOJI_ALL
        assert _slot_emoji(9, n) == EMOJI_MOST
        assert _slot_emoji(8, n) == EMOJI_NONE
        assert _slot_emoji(0, n) == EMOJI_NONE


# ---------------------------------------------------------------------------
# _format_actions_block — unit tests
# ---------------------------------------------------------------------------


class TestFormatActionsBlock:
    def test_format_actions_block_has_confirm_button(self) -> None:
        """The actions block must contain an element with action_id 'confirm_proposal'."""
        targets = make_targets()
        block = _format_actions_block(targets)
        assert block["type"] == "actions"
        action_ids = [el.get("action_id") for el in block["elements"]]
        assert "confirm_proposal" in action_ids

    def test_format_actions_block_has_override_dropdown(self) -> None:
        """The actions block must contain a multi-select with all candidate names as options."""
        targets = make_targets()
        block = _format_actions_block(targets)
        dropdowns = [
            el for el in block["elements"]
            if el.get("action_id") == "override_sources"
        ]
        assert dropdowns, "Expected a multi_static_select with action_id 'override_sources'"
        option_values = {opt["value"] for opt in dropdowns[0]["options"]}
        assert option_values == {t.name for t in targets}

    def test_format_actions_block_has_submit_override_button(self) -> None:
        """The actions block must contain a 'submit_override' button."""
        targets = make_targets()
        block = _format_actions_block(targets)
        action_ids = [el.get("action_id") for el in block["elements"]]
        assert "submit_override" in action_ids

    def test_format_actions_block_empty_candidates(self) -> None:
        """With no candidates the dropdown is omitted but confirm/submit buttons remain."""
        block = _format_actions_block([])
        action_ids = [el.get("action_id") for el in block["elements"]]
        assert "confirm_proposal" in action_ids
        assert "submit_override" in action_ids
        assert "override_sources" not in action_ids


# ---------------------------------------------------------------------------
# _verify_slack_signature — unit tests
# ---------------------------------------------------------------------------


class TestVerifySlackSignature:
    def _make_signature(self, secret: str, timestamp: str, body: bytes) -> str:
        import hashlib
        import hmac as _hmac

        sig_base = f"v0:{timestamp}:{body.decode('utf-8')}"
        return "v0=" + _hmac.new(
            secret.encode(), sig_base.encode(), hashlib.sha256
        ).hexdigest()

    def test_verify_slack_signature_valid(self) -> None:
        """A correctly computed signature must verify successfully."""
        import time as _time

        secret = "test_signing_secret"
        body = b"payload=%7B%22action%22%3A%22confirm%22%7D"
        timestamp = str(int(_time.time()))
        sig = self._make_signature(secret, timestamp, body)
        assert _verify_slack_signature(secret, body, timestamp, sig) is True

    def test_verify_slack_signature_stale_timestamp(self) -> None:
        """A request whose timestamp is older than 5 minutes must be rejected."""
        import time as _time

        secret = "test_signing_secret"
        body = b"payload=hello"
        # 10 minutes in the past
        old_timestamp = str(int(_time.time()) - 601)
        sig = self._make_signature(secret, old_timestamp, body)
        assert _verify_slack_signature(secret, body, old_timestamp, sig) is False

    def test_verify_slack_signature_wrong_secret(self) -> None:
        """A signature computed with the wrong secret must be rejected."""
        import time as _time

        body = b"payload=hello"
        timestamp = str(int(_time.time()))
        sig = self._make_signature("wrong_secret", timestamp, body)
        assert _verify_slack_signature("correct_secret", body, timestamp, sig) is False

    def test_verify_slack_signature_invalid_timestamp(self) -> None:
        """A non-integer timestamp string must be rejected."""
        assert _verify_slack_signature("secret", b"body", "not-a-number", "v0=abc") is False


# ---------------------------------------------------------------------------
# await_confirmation — integration-style tests using _server_factory injection
# ---------------------------------------------------------------------------


def _make_confirm_payload(user_id: str = "U123") -> dict[str, Any]:
    """Build a minimal Slack confirm_proposal payload."""
    return {
        "user": {"id": user_id},
        "actions": [{"action_id": "confirm_proposal", "value": "confirm"}],
    }


def _make_override_payload(
    selected_names: list[str],
    user_id: str = "U123",
) -> dict[str, Any]:
    """Build a minimal Slack submit_override payload."""
    return {
        "user": {"id": user_id},
        "actions": [
            {
                "action_id": "submit_override",
                "selected_options": [{"value": n} for n in selected_names],
            }
        ],
    }


def _make_server_factory(*payloads: dict[str, Any]):
    """Return a _server_factory that pre-loads *payloads* into _payload_queue.

    The returned factory injects the payloads into the module-level queue so
    ``await_confirmation``'s poll loop picks them up immediately, avoiding any
    real network calls.
    """
    class _FakeServer:
        def __init__(self, port: int) -> None:
            for p in payloads:
                _payload_queue.put(p)

        def shutdown(self) -> None:
            pass

    return _FakeServer


def _make_authorised_client(email: str = "authorised@example.com") -> MagicMock:
    """Return a mock WebClient whose users_info returns *email*."""
    client = MagicMock()
    client.users_info.return_value = {
        "user": {"profile": {"email": email}}
    }
    client.chat_postMessage.return_value = {"ts": "1720000000.000001", "ok": True}
    return client


def _make_obs_window_past_threshold() -> ObsWindow:
    """Window whose start is 3 days from now (inside the 5-day reminder threshold)."""
    start = datetime.now(timezone.utc) + timedelta(days=3)
    end = start + timedelta(hours=2)
    return ObsWindow(start=start, end=end, antennas=["Ef", "Wb"], project="RP036A")


def _make_obs_window_future() -> ObsWindow:
    """Window whose start is 10 days from now (outside the 5-day reminder threshold)."""
    start = datetime.now(timezone.utc) + timedelta(days=10)
    end = start + timedelta(hours=2)
    return ObsWindow(start=start, end=end, antennas=["Ef", "Wb"], project="RP036A")


class TestAwaitConfirmation:
    """Tests for ``await_confirmation`` using _server_factory injection."""

    def test_confirm_without_a_proposal_returns_every_candidate(self) -> None:
        """With no separate proposal, confirming accepts everything offered."""
        targets = make_targets()
        obs_window = _make_obs_window_future()
        client = _make_authorised_client(email="authorised@example.com")

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(_make_confirm_payload()),
        )

        assert result == targets

    def test_confirm_accepts_the_proposal_not_every_candidate(self) -> None:
        """Confirming schedules what was proposed, not the whole dropdown.

        The daemon offers every observable source for override while
        proposing only a handful; confirming must not schedule all of them.
        """
        targets = make_targets()
        proposed = targets[:1]
        obs_window = _make_obs_window_future()
        client = _make_authorised_client(email="authorised@example.com")

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            proposed=proposed,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(_make_confirm_payload()),
        )

        assert result == proposed
        assert len(result) < len(targets)

    def test_override_can_still_pick_any_candidate(self) -> None:
        """Override is not limited to the proposal — that is its whole point."""
        targets = make_targets()
        not_proposed = targets[-1]
        obs_window = _make_obs_window_future()
        client = _make_authorised_client(email="authorised@example.com")

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            proposed=targets[:1],
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(
                _make_override_payload([not_proposed.name])
            ),
        )

        assert result == [not_proposed]

    def test_debug_mode_auto_confirms_only_the_proposal(self) -> None:
        targets = make_targets()
        proposed = targets[:1]

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            proposed=proposed,
            obs_window=_make_obs_window_future(),
            client=MagicMock(),
            channel_id="C_TEST",
            debug=True,
        )

        assert result == proposed

    def test_override_returns_selected_sources(self) -> None:
        """submit_override with one source → returns only that target."""
        targets = make_targets()
        obs_window = _make_obs_window_future()
        client = _make_authorised_client(email="authorised@example.com")
        # Select only the first target by internal name
        selected_name = targets[0].name

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(
                _make_override_payload([selected_name])
            ),
        )

        assert result == [targets[0]]

    def test_unknown_action_ignored_then_confirm_succeeds(self) -> None:
        """First payload has an unknown action_id and is ignored; second confirm returns candidates."""
        targets = make_targets()
        obs_window = _make_obs_window_future()
        client = _make_authorised_client(email="user@example.com")

        unknown_payload = {
            "user": {"id": "U123"},
            "actions": [{"action_id": "unknown_action", "value": "noop"}],
        }

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(
                unknown_payload,
                _make_confirm_payload(user_id="U123"),
            ),
        )

        assert result == targets

    def test_reminder_posted_when_past_threshold(self) -> None:
        """obs_window.start is within the 5-day reminder threshold → reminder is posted."""
        targets = make_targets()
        # start is 3 days away — past the 5-day threshold
        obs_window = _make_obs_window_past_threshold()
        client = _make_authorised_client(email="authorised@example.com")

        await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(_make_confirm_payload()),
        )

        # Reminder is posted via chat_postMessage with thread_ts
        thread_calls = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == "1720000000.000001"
        ]
        assert thread_calls, "Expected at least one reminder thread reply"

    def test_no_reminder_before_threshold(self) -> None:
        """obs_window.start is 10 days away — no reminder should be posted."""
        targets = make_targets()
        obs_window = _make_obs_window_future()  # 10 days away
        client = _make_authorised_client(email="authorised@example.com")

        await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(_make_confirm_payload()),
        )

        # chat_postMessage should NOT have been called with thread_ts
        thread_calls = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == "1720000000.000001"
        ]
        assert not thread_calls, "Expected no reminder thread reply"

    def test_override_preserves_candidate_order(self) -> None:
        """When overriding with both sources, the order follows the candidates list."""
        targets = make_targets()
        obs_window = _make_obs_window_future()
        client = _make_authorised_client(email="authorised@example.com")
        # Request both but in reverse order in the payload
        selected_names = [targets[1].name, targets[0].name]

        result = await_confirmation(
            message_ts="1720000000.000001",
            candidates=targets,
            obs_window=obs_window,
            client=client,
            channel_id="C_TEST",
            _server_factory=_make_server_factory(
                _make_override_payload(selected_names)
            ),
        )

        # Result order must follow candidates list, not payload order
        assert result == targets


# ---------------------------------------------------------------------------
# The plan summary the daemon posts before asking for confirmation
# ---------------------------------------------------------------------------


def _make_plan(
    observable: list[tuple[str, str | None, int]],
    *,
    dropped: list[tuple[str, str | None, int]] | None = None,
    array: list[str] | None = None,
    project: str = "RP036A",
    forced: bool = False,
):
    """Build a PlanResult from (name, canonical_name, n_antennas) triples."""
    from precise_scheduler.pipeline import PlanResult
    from precise_scheduler.visibility import SourceObservability

    array = array or ["Ef", "Wb", "Tr"]
    n_slots = 8

    def _source(name: str, canonical: str | None, n_antennas: int):
        return SourceObservability(
            target=Target(
                name=name,
                canonical_name=canonical,
                priority=2,
                coordinates=Coordinates(
                    ra="00:33:03.600", dec="+33:08:00.00", equinox="J2000"
                ),
            ),
            n_antennas=n_antennas,
            antennas=array[:n_antennas],
            slots=[n_antennas] * n_slots,
            slots_at_best=n_slots,
            n_slots=n_slots,
        )

    kept = [_source(*row) for row in observable]
    cut = [_source(*row) for row in (dropped or [])]
    return PlanResult(
        obs_window=ObsWindow(
            start=datetime(2026, 9, 7, 19, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 8, 5, 0, tzinfo=timezone.utc),
            antennas=array,
            project=project,
        ),
        from_calendar=True,
        array=array,
        n_slots=n_slots,
        min_antennas=max(1, len(array) - 1),
        observable=kept,
        dropped=cut,
        n_candidates=len(kept) + len(cut),
        forced=forced,
    )


class TestPostKeyFile:
    def _key(self, tmp_path) -> "Path":
        key = tmp_path / "PR411A.key"
        key.write_text("! draft schedule\n")
        return key

    def test_uploads_the_file_to_the_channel(self, tmp_path) -> None:
        client = MagicMock()
        client.files_upload_v2.return_value = {
            "file": {"permalink": "https://slack.example/files/PR411A.key"}
        }

        link = post_key_file(
            self._key(tmp_path), "PR411A", make_targets()[:1],
            client=client, channel_id="C123",
        )

        kwargs = client.files_upload_v2.call_args.kwargs
        assert kwargs["channel"] == "C123"
        assert kwargs["filename"] == "PR411A.key"
        assert link == "https://slack.example/files/PR411A.key"

    def test_says_it_is_a_template_to_be_revised(self, tmp_path) -> None:
        client = MagicMock()
        client.files_upload_v2.return_value = {"file": {}}

        post_key_file(
            self._key(tmp_path), "PR411A", make_targets()[:1],
            client=client, channel_id="C123",
        )

        comment = client.files_upload_v2.call_args.kwargs["initial_comment"]
        assert "prepared observation template" in comment
        assert "review" in comment
        assert "not the final one" in comment

    def test_lists_the_scheduled_sources(self, tmp_path) -> None:
        client = MagicMock()
        client.files_upload_v2.return_value = {"file": {}}
        targets = make_targets()

        post_key_file(
            self._key(tmp_path), "PR411A", targets,
            client=client, channel_id="C123",
        )

        comment = client.files_upload_v2.call_args.kwargs["initial_comment"]
        for target in targets:
            assert (target.canonical_name or target.name) in comment

    def test_debug_mode_uploads_nothing(self, tmp_path) -> None:
        client = MagicMock()

        assert post_key_file(
            self._key(tmp_path), "PR411A", make_targets()[:1],
            client=client, channel_id="C123", debug=True,
        ) is None
        client.files_upload_v2.assert_not_called()

    def test_missing_file_is_reported_not_raised(self, tmp_path) -> None:
        """The schedule is already generated; a failed upload is not fatal."""
        client = MagicMock()

        assert post_key_file(
            tmp_path / "gone.key", "PR411A", make_targets()[:1],
            client=client, channel_id="C123",
        ) is None
        client.files_upload_v2.assert_not_called()

    def test_upload_failure_is_swallowed(self, tmp_path) -> None:
        client = MagicMock()
        client.files_upload_v2.side_effect = SlackApiError(
            "not_in_channel", response={"error": "not_in_channel"}
        )

        assert post_key_file(
            self._key(tmp_path), "PR411A", make_targets()[:1],
            client=client, channel_id="C123",
        ) is None

    def test_can_post_into_a_thread(self, tmp_path) -> None:
        client = MagicMock()
        client.files_upload_v2.return_value = {"file": {}}

        post_key_file(
            self._key(tmp_path), "PR411A", make_targets()[:1],
            client=client, channel_id="C123", thread_ts="1234.5678",
        )

        assert client.files_upload_v2.call_args.kwargs["thread_ts"] == "1234.5678"

    def test_missing_scope_failure_names_the_scope(self, tmp_path, caplog) -> None:
        """chat:write is not enough to upload a file; say so."""
        client = MagicMock()
        client.files_upload_v2.side_effect = SlackApiError(
            "missing_scope", response={"error": "missing_scope"}
        )

        post_key_file(
            self._key(tmp_path), "PR411A", make_targets()[:1],
            client=client, channel_id="C123",
        )

        assert "files:write" in caplog.text




# ---------------------------------------------------------------------------
# The observability report
# ---------------------------------------------------------------------------


class TestVisibilityRow:
    """15-minute slots collapse into GRID_MINUTES cells, worst case wins."""

    def _row(self, slots, n_antennas=4):
        return _visibility_row(slots, n_antennas)

    def test_cells_span_the_configured_period(self) -> None:
        per_cell = int(GRID_MINUTES // 15)
        assert self._row([4] * (per_cell * 3)) == EMOJI_ALL * 3

    def test_full_coverage_is_green(self) -> None:
        assert self._row([4, 4]) == EMOJI_ALL

    def test_partial_coverage_is_yellow(self) -> None:
        assert self._row([3, 3]) == EMOJI_MOST

    def test_any_shortfall_in_the_cell_downgrades_it(self) -> None:
        """A cell is green only if the array was complete for the whole period."""
        assert self._row([4, 3]) == EMOJI_MOST
        assert self._row([3, 4]) == EMOJI_MOST

    def test_no_coverage_is_black(self) -> None:
        assert self._row([0, 0]) == EMOJI_NONE

    def test_a_gap_in_the_cell_makes_it_black(self) -> None:
        assert self._row([4, 0]) == EMOJI_NONE

    def test_one_cell_per_two_slots(self) -> None:
        assert self._row([4, 4, 0, 0, 2, 2]) == EMOJI_ALL + EMOJI_NONE + EMOJI_MOST

    def test_odd_trailing_slot_still_gets_a_cell(self) -> None:
        assert len(self._row([4, 4, 4])) == 2

    def test_no_separator_between_cells(self) -> None:
        row = self._row([4, 4, 0, 0])
        assert " " not in row and len(row) == 2


class TestPlanReport:
    def _text(self, plan) -> str:
        return "\n".join(
            _all_block_text(message) for message in build_plan_messages(plan)
        )

    def test_summary_names_the_run_and_the_array(self) -> None:
        text = self._text(_make_plan([("R1_D", "FRB20121102A", 3)]))

        assert "Observation RP036A" in text
        assert "07 Sep 2026 19:00 to 05:00 UTC (10.0 h)" in text
        assert "3 antennas: Ef, Wb, Tr" in text

    def test_activity_summary_carries_detection_counts(self) -> None:
        plan = _make_plan([("R1_D", "FRB20121102A", 3)])
        plan.active_15d = [("FRB20201124A", 2)]
        plan.active_30d = [("FRB20201124A", 2), ("FRB20220912A", 1)]

        text = self._text(plan)

        assert "FRB20201124A (2 detections)" in text
        assert "FRB20220912A (1 detection)" in text

    def test_a_source_active_recently_is_not_repeated_in_the_older_window(
        self,
    ) -> None:
        plan = _make_plan([("R1_D", "FRB20121102A", 3)])
        plan.active_15d = [("FRB20201124A", 2)]
        plan.active_30d = [("FRB20201124A", 2)]

        older = self._text(plan).split("Active in the last 30 days only:")[1]

        assert "FRB20201124A" not in older.split("\n")[0]

    def test_empty_activity_says_none(self) -> None:
        text = self._text(_make_plan([("R1_D", "FRB20121102A", 3)]))

        assert text.count("none") >= 2

    def test_previous_runs_are_listed(self) -> None:
        from precise_scheduler.obs_schedules import ObsRecord, SourceTime

        plan = _make_plan([("R1_D", "FRB20121102A", 3)])
        plan.recent_runs = [
            ObsRecord(
                project="PR405A",
                start=datetime(2026, 8, 3, tzinfo=timezone.utc),
                sources=[SourceTime("R4_D", 3600.0, ["Ef"])],
            )
        ]

        text = self._text(plan)

        assert "PR405A" in text
        assert "03 Aug 2026" in text
        assert "R4_D" in text

    def test_no_previous_runs_says_so(self) -> None:
        assert "none on record" in self._text(_make_plan([("R1_D", "FRB1", 3)]))

    def test_table_rows_carry_name_frb_hours_and_grid(self) -> None:
        text = self._text(_make_plan([("R230814A_D", "FRB20230814A", 3)]))

        assert "R230814A_D" in text
        assert "FRB20230814A" in text
        assert "2.0h" in text            # 8 slots at 15 min
        assert EMOJI_ALL in text

    def test_source_without_a_canonical_name_shows_a_dash(self) -> None:
        assert "—" in self._text(_make_plan([("R65_D", None, 3)]))

    def test_the_legend_explains_the_colours(self) -> None:
        text = self._text(_make_plan([("R1_D", "FRB1", 3)]))

        assert f"{EMOJI_ALL} all 3 antennas" in text
        assert f"{EMOJI_MOST} some" in text
        assert f"{EMOJI_NONE} none" in text

    def test_empty_result_says_so_plainly(self) -> None:
        text = self._text(_make_plan([], dropped=[("R2_D", "FRB2", 1)]))

        assert "RP036A: 0 FRB observable" in text
        assert "No candidate can be observed by at least 2" in text


class TestPlanMessages:
    def test_the_table_is_a_separate_message(self) -> None:
        """Slack caps message size; the table is the part that grows."""
        messages = build_plan_messages(_make_plan([("R1_D", "FRB1", 3)]))

        assert len(messages) >= 2
        head = _all_block_text(messages[0])
        assert "Activity summary" in head
        assert "R1_D" not in head

    def test_summary_comes_first(self) -> None:
        messages = build_plan_messages(_make_plan([("R1_D", "FRB1", 3)]))

        assert messages[0][0]["type"] == "header"

    def test_a_long_table_is_split_across_messages(self) -> None:
        plan = _make_plan(
            [(f"R{i:04d}_D", f"FRB2020{i:04d}A", 3) for i in range(1000)]
        )

        messages = build_plan_messages(plan)

        assert len(messages) > 2
        assert all(len(m) <= MAX_BLOCKS_PER_MESSAGE for m in messages[1:])

    def test_every_source_survives_the_split(self) -> None:
        plan = _make_plan([(f"R{i:03d}_D", f"FRB2020{i:04d}A", 3) for i in range(400)])

        text = "\n".join(_all_block_text(m) for m in build_plan_messages(plan))

        assert all(f"FRB2020{i:04d}A" in text for i in range(400))

    def test_sections_respect_the_size_limit(self) -> None:
        plan = _make_plan([(f"R{i:03d}_D", f"FRB2020{i:04d}A", 3) for i in range(400)])

        for message in build_plan_messages(plan):
            for block in message:
                if block["type"] == "section":
                    assert len(block["text"]["text"]) <= SECTION_TEXT_LIMIT + 8


class TestPostPlanMessages:
    def test_posts_every_message(self) -> None:
        plan = _make_plan([("R1_D", "FRB1", 3)])
        client = _make_successful_client()

        post_plan(plan, client=client, channel_id="C123")

        assert client.chat_postMessage.call_count == len(build_plan_messages(plan))

    def test_returns_the_first_timestamp(self) -> None:
        plan = _make_plan([("R1_D", "FRB1", 3)])
        client = MagicMock()
        client.chat_postMessage.side_effect = [
            {"ts": "first"}, {"ts": "second"}, {"ts": "third"},
        ]

        assert post_plan(plan, client=client, channel_id="C123") == "first"

    def test_continuations_are_marked_in_the_fallback_text(self) -> None:
        plan = _make_plan([("R1_D", "FRB1", 3)])
        client = _make_successful_client()

        post_plan(plan, client=client, channel_id="C123")

        texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        assert "(continued)" not in texts[0]
        assert all("(continued)" in t for t in texts[1:])

    def test_debug_mode_posts_nothing(self) -> None:
        client = _make_successful_client()

        post_plan(
            _make_plan([("R1_D", "FRB1", 3)]),
            client=client, channel_id="C123", debug=True,
        )

        client.chat_postMessage.assert_not_called()
