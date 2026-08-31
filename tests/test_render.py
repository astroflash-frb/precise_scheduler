"""Tests for precise_scheduler.render (Block Kit → terminal)."""

from __future__ import annotations

import pytest

from precise_scheduler.render import _mrkdwn_to_rich, render_blocks


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


# ---------------------------------------------------------------------------
# mrkdwn conversion
# ---------------------------------------------------------------------------


class TestMrkdwnToRich:
    def test_bold_becomes_rich_bold(self) -> None:
        assert _mrkdwn_to_rich("*Activity summary*") == "[bold]Activity summary[/bold]"

    def test_standalone_italic_is_converted(self) -> None:
        assert "[italic dim]none[/italic dim]" in _mrkdwn_to_rich("Active: _none_")

    def test_underscore_inside_target_name_is_untouched(self) -> None:
        """PRECISE internal names contain underscores; a naive _italic_ rule
        would eat the text between two source names."""
        text = "1. R200223_D and 2. R201124_A"
        assert _mrkdwn_to_rich(text) == text

    def test_two_underscored_names_are_not_italicised(self) -> None:
        text = "R190611_A, R200223_D"
        assert "italic" not in _mrkdwn_to_rich(text)

    def test_square_brackets_are_escaped(self) -> None:
        """Literal brackets must not be read as rich markup."""
        assert _mrkdwn_to_rich("[bold]not markup") == r"\[bold]not markup"

    def test_fenced_text_is_not_styled(self) -> None:
        """Code fences hold column-aligned tables; styling breaks alignment."""
        result = _mrkdwn_to_rich("before\n```\nSource | *x* | _y_\n```\nafter")
        assert "*x*" in result
        assert "_y_" in result
        assert "[bold]" not in result

    def test_bold_outside_fence_still_converted(self) -> None:
        result = _mrkdwn_to_rich("*Grid:*\n```\nraw *text*\n```")
        assert result.startswith("[bold]Grid:[/bold]")
        assert "raw *text*" in result

    def test_bold_does_not_span_newlines(self) -> None:
        text = "*not\nbold*"
        assert "[bold]" not in _mrkdwn_to_rich(text)


# ---------------------------------------------------------------------------
# render_blocks
# ---------------------------------------------------------------------------


class TestRenderBlocks:
    def test_section_text_is_rendered(self) -> None:
        assert "hello" in render_blocks([_section("hello")])

    def test_divider_becomes_rule(self) -> None:
        result = render_blocks([{"type": "divider"}])
        assert "─" in result

    def test_actions_block_lists_control_labels(self) -> None:
        blocks = [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Confirm proposal"},
                    },
                    {
                        "type": "multi_static_select",
                        "placeholder": {"type": "plain_text", "text": "Override sources"},
                    },
                ],
            }
        ]
        result = render_blocks(blocks)
        assert "Confirm proposal" in result
        assert "Override sources" in result
        assert "Slack-only" in result

    def test_actions_block_without_elements_is_skipped(self) -> None:
        assert render_blocks([{"type": "actions", "elements": []}]) == ""

    def test_unknown_block_type_is_skipped(self) -> None:
        assert render_blocks([{"type": "image", "image_url": "x"}]) == ""

    def test_section_without_text_does_not_raise(self) -> None:
        assert render_blocks([{"type": "section"}]) == ""

    def test_blocks_render_in_order(self) -> None:
        result = render_blocks([_section("first"), _section("second")])
        assert result.index("first") < result.index("second")

    def test_empty_blocks_render_empty(self) -> None:
        assert render_blocks([]) == ""


# ---------------------------------------------------------------------------
# Integration with the real proposal blocks
# ---------------------------------------------------------------------------


def test_renders_real_proposal_blocks_without_raising(mocker) -> None:
    """render_blocks must handle every block type build_proposal_blocks emits."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    from precise_scheduler.calendar import ObsWindow
    from precise_scheduler.slack_scheduler import build_proposal_blocks

    start = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
    window = ObsWindow(
        start=start,
        end=start + timedelta(hours=2),
        antennas=["Ef", "Wb"],
        project="RP036A",
    )

    target = MagicMock()
    target.name = "R200223_D"
    target.canonical_name = "FRB20200223D"
    target.priority = 2

    activity = MagicMock()
    activity.get_detections.return_value = []

    blocks = build_proposal_blocks(
        obs_window=window,
        visibility_grid={"R200223_D": [2] * 8},
        proposed=[target],
        active_15d=[],
        active_30d=[],
        activity=activity,
        candidates=[target],
    )

    result = render_blocks(blocks)

    assert "RP036A" in result
    assert "FRB20200223D" in result
    # The internal name carries an underscore and must survive verbatim.
    assert "R200223_D" in result
