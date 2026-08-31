"""
Terminal rendering of Slack Block Kit messages.

``precise schedule`` builds the very same Block Kit payload the daemon would
post to Slack, but prints it instead of sending it.  This module is the only
place that translates blocks into console output, so the Slack modules stay
free of presentation concerns.

Only the block/element types actually produced by
:func:`precise_scheduler.slack_scheduler.build_proposal_blocks` are handled:
``section``, ``divider`` and ``actions``.
"""

from __future__ import annotations

import re
from typing import Any

from rich.markup import escape as rich_escape

RULE_WIDTH = 70

# Slack mrkdwn *bold*. Deliberately refuses to span newlines.
_MRKDWN_BOLD_RE = re.compile(r"\*([^*\n]+)\*")

# Slack mrkdwn _italic_. The lookarounds keep underscores that sit inside a
# word intact, so PRECISE target names like R200223_D survive unstyled.
_MRKDWN_ITALIC_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")


def _mrkdwn_to_rich(text: str) -> str:
    """Convert Slack mrkdwn to rich console markup.

    Text inside triple-backtick fences is emitted verbatim: the visibility
    grid and the previous-runs table are column-aligned there, and styling
    them would corrupt the alignment.
    """
    rendered: list[str] = []
    for index, part in enumerate(text.split("```")):
        escaped = rich_escape(part)
        if index % 2 == 1:  # inside a fence
            rendered.append(escaped.strip("\n"))
        else:
            styled = _MRKDWN_BOLD_RE.sub(r"[bold]\1[/bold]", escaped)
            styled = _MRKDWN_ITALIC_RE.sub(r"[italic dim]\1[/italic dim]", styled)
            rendered.append(styled)
    return "".join(rendered)


def _element_label(element: dict[str, Any]) -> str | None:
    """Return the human-readable label of an actions-block element."""
    for key in ("text", "placeholder"):
        holder = element.get(key)
        if isinstance(holder, dict) and "text" in holder:
            return str(holder["text"])
    return None


def render_blocks(blocks: list[dict[str, Any]]) -> str:
    """Render Block Kit *blocks* as a rich-markup string.

    Interactive ``actions`` blocks have no terminal equivalent, so their
    controls are listed as a dimmed note rather than silently dropped.
    """
    lines: list[str] = []

    for block in blocks:
        block_type = block.get("type")

        if block_type == "divider":
            lines.append(f"[dim]{'─' * RULE_WIDTH}[/dim]")

        elif block_type == "section":
            text = block.get("text", {}).get("text", "")
            lines.append(_mrkdwn_to_rich(text))

        elif block_type == "actions":
            labels = [
                label
                for label in (
                    _element_label(el) for el in block.get("elements", [])
                )
                if label
            ]
            if labels:
                joined = " · ".join(rich_escape(label) for label in labels)
                lines.append(f"[dim]Slack-only controls: {joined}[/dim]")

    return "\n".join(lines)
