"""
Slack scheduling interface for PRECISE Scheduler.

This module manages the Slack proposal message lifecycle.
Issue #8: static post_proposal (this file).
Issue #9: interactive await_confirmation (added here).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
import math
import queue
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

if TYPE_CHECKING:
    from .pipeline import PlanResult
    from .activity import Activity
    from .calendar import ObsWindow
    from .targets import Target
    from .visibility import VisibilityGrid

log = logging.getLogger(__name__)


class SlackError(Exception):
    """Raised when a Slack API call fails."""


SLOT_MINUTES = 15

#: Look-back windows named in the activity summary.  These mirror
#: ``pipeline.ACTIVE_WINDOW_SHORT_DAYS`` / ``ACTIVE_WINDOW_LONG_DAYS``; they
#: are labels only, so the report module needs no import from the pipeline.
ACTIVE_15D_LABEL = 15
ACTIVE_30D_LABEL = 30

#: One visibility cell per this many minutes.  Coarser than the 15-minute
#: sampling so a ten-hour run fits on one line: 40 slots become 20 cells.
GRID_MINUTES = 30

#: How many table sections go in one message.  Slack's hard cap is 50 blocks,
#: but a message of 50 full-size sections is ~145 kB of JSON — so the table is
#: split well before that, into messages that stay a sensible size to read.
MAX_BLOCKS_PER_MESSAGE = 10

#: Slack rejects the whole message if any single section's text exceeds 3000
#: characters, so long source listings are split across several sections.
#: Left a little under the limit to leave room for the mrkdwn markup.
SECTION_TEXT_LIMIT = 2900

# Emoji encoding for the visibility grid
EMOJI_ALL = "🟩"   # all N antennas can observe
EMOJI_MOST = "🟨"  # all-but-one can observe
EMOJI_NONE = "⬛"  # no antenna can observe


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _slot_emoji(count: int, n_antennas: int) -> str:
    """Return the visibility emoji for a single 15-min slot.

    Parameters
    ----------
    count:
        Number of antennas that can observe in this slot.
    n_antennas:
        Total number of participating antennas.
    """
    if n_antennas <= 0:
        return EMOJI_NONE
    if count >= n_antennas:
        return EMOJI_ALL
    if count >= n_antennas - 1:
        return EMOJI_MOST
    return EMOJI_NONE


def _format_header_block(obs_window: ObsWindow) -> dict[str, Any]:
    """Build the header section block."""
    start_str = obs_window.start.strftime("%Y-%m-%d %H:%M")
    end_str = obs_window.end.strftime("%Y-%m-%d %H:%M")
    antennas_str = ", ".join(obs_window.antennas)
    text = (
        f"*PRECISE Schedule Proposal — {obs_window.project}*\n"
        f"Observation: {start_str} → {end_str} (UTC)\n"
        f"Antennas: {antennas_str}"
    )
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _format_activity_block(active_15d: list[str], active_30d: list[str], activity: Activity) -> dict[str, Any]:
    """Build the activity summary section block.

    Shows sources active in the last 15 days and those active in 30 days but
    not 15 days, each with detection counts.
    """
    active_15d_set = set(active_15d)

    def _det_count(canonical_name: str, since_days: int) -> int:
        return len(activity.get_detections(canonical_name, since_days=since_days))

    lines: list[str] = ["*Activity summary*"]

    if active_15d:
        parts = [
            f"{name} ({_det_count(name, 15)} detection{'s' if _det_count(name, 15) != 1 else ''})"
            for name in active_15d
        ]
        lines.append(f"Active in last 15 days: {', '.join(parts)}")
    else:
        lines.append("Active in last 15 days: _none_")

    beyond_15d = [name for name in active_30d if name not in active_15d_set]
    if beyond_15d:
        parts = [
            f"{name} ({_det_count(name, 30)} detection{'s' if _det_count(name, 30) != 1 else ''})"
            for name in beyond_15d
        ]
        lines.append(f"Active in last 30 days (not in 15d): {', '.join(parts)}")
    else:
        lines.append("Active in last 30 days (not in 15d): _none_")

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


def _format_visibility_block(obs_window: ObsWindow, visibility_grid: VisibilityGrid) -> dict[str, Any]:
    """Build the visibility grid block.

    One row per source, one column per 15-min slot. Header shows HH:MM times.
    Emoji encoding: EMOJI_ALL / EMOJI_MOST / EMOJI_NONE.
    """
    n_antennas = len(obs_window.antennas)
    n_slots = math.ceil(
        (obs_window.end - obs_window.start).total_seconds() / (SLOT_MINUTES * 60)
    )

    # Build slot start times for the header
    slot_times: list[str] = []
    for i in range(n_slots):
        slot_dt = obs_window.start + timedelta(minutes=SLOT_MINUTES * i)
        slot_times.append(slot_dt.strftime("%H:%M"))

    # Column width: each emoji cell is 1 character wide but we pad for alignment
    slot_width = 5  # "HH:MM"
    name_col_width = 16

    header_row = f"{'Source':{name_col_width}}| " + "  ".join(
        t.rjust(slot_width) for t in slot_times
    )
    divider = "-" * len(header_row)

    lines = [header_row, divider]

    for source_name, slots in visibility_grid.items():
        # Pad or truncate slot list to match n_slots
        padded = (list(slots) + [0] * n_slots)[:n_slots]
        emoji_cells = [_slot_emoji(count, n_antennas) for count in padded]
        # Each emoji is 2 chars wide in monospace; use spacing that aligns
        row = f"{source_name:{name_col_width}}| " + "   ".join(emoji_cells)
        lines.append(row)

    grid_text = "\n".join(lines)
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Visibility grid (15-min slots):*\n```\n{grid_text}\n```",
        },
    }


def _days_since_last_detection(canonical_name: str, activity: Activity) -> int | None:
    """Return days since the most recent detection, or None if no records exist."""
    records = activity.get_detections(canonical_name, since_days=36500)
    if not records:
        return None
    most_recent = records[0].utc_datetime
    delta = datetime.now(timezone.utc) - most_recent
    return max(0, delta.days)


def _visible_percent(slots: list[int], n_antennas: int) -> float:
    """Return the percentage of the window where the source is fully visible."""
    if not slots or n_antennas <= 0:
        return 0.0
    total_capacity = len(slots) * n_antennas
    return 100.0 * sum(slots) / total_capacity


def _format_proposed_sources_block(proposed: list[Target], obs_window: ObsWindow,
                                   visibility_grid: VisibilityGrid, activity: Activity) -> dict[str, Any]:
    """Build the proposed sources section block."""
    n_antennas = len(obs_window.antennas)
    lines = [f"*Proposed sources ({len(proposed)}):*"]

    for idx, target in enumerate(proposed, start=1):
        days_ago = _days_since_last_detection(target.canonical_name, activity)
        if days_ago is None:
            detection_str = "never detected"
        elif days_ago == 0:
            detection_str = "last detected today"
        else:
            detection_str = f"last detected {days_ago} day{'s' if days_ago != 1 else ''} ago"

        slots = visibility_grid.get(target.name, [])
        vis_pct = _visible_percent(slots, n_antennas)

        lines.append(
            f"{idx}. {target.canonical_name} — priority {target.priority}, "
            f"{detection_str}, visible {vis_pct:.0f}% of window"
        )

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


def _format_actions_block(candidates: list[Target]) -> dict[str, Any]:
    """Build the Block Kit actions block with interactive controls.

    Contains:
    - A "Confirm" button (``action_id="confirm_proposal"``).
    - A multi-select static dropdown of all candidates
      (``action_id="override_sources"``).
    - A "Submit override" button (``action_id="submit_override"``).

    Parameters
    ----------
    candidates:
        All candidate targets offered for selection in the dropdown.
    """
    options = [
        {
            "text": {"type": "plain_text", "text": t.canonical_name, "emoji": False},
            "value": t.name,
        }
        for t in candidates
    ]

    elements: list[dict[str, Any]] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Confirm proposal", "emoji": True},
            "style": "primary",
            "action_id": "confirm_proposal",
            "value": "confirm",
        },
    ]

    if options:
        elements.append(
            {
                "type": "multi_static_select",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Override sources…",
                    "emoji": False,
                },
                "options": options,
                "action_id": "override_sources",
            }
        )

    elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Submit override", "emoji": False},
            "action_id": "submit_override",
            "value": "submit_override",
        }
    )

    return {"type": "actions", "elements": elements}


def _divider_block() -> dict[str, Any]:
    """Return a Slack divider block."""
    return {"type": "divider"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_proposal_blocks(obs_window: ObsWindow, visibility_grid: VisibilityGrid, proposed: list[Target],
                          active_15d: list[str], active_30d: list[str],
                          activity: Activity, candidates: list[Target] | None = None) -> list[dict[str, Any]]:
    """Build the Block Kit blocks for the scheduling proposal.

    Split out of :func:`post_proposal` so the same message can be rendered to
    the terminal by ``precise schedule`` without a Slack round-trip.  See
    :func:`post_proposal` for the parameter meanings.
    """
    effective_candidates: list[Target] = candidates if candidates is not None else proposed

    return [
        _format_header_block(obs_window), _divider_block(),
        _format_activity_block(active_15d, active_30d, activity), _divider_block(),
        _format_visibility_block(obs_window, visibility_grid), _divider_block(),
        _format_proposed_sources_block(proposed, obs_window, visibility_grid, activity),
        _divider_block(), _format_actions_block(effective_candidates)]


def proposal_fallback_text(obs_window: ObsWindow) -> str:
    """Return the plain-text fallback/notification line for a proposal."""
    return (
        f"PRECISE Schedule Proposal — {obs_window.project} "
        f"({obs_window.start.strftime('%Y-%m-%d %H:%M')} UTC)"
    )


def post_proposal(obs_window: ObsWindow, visibility_grid: VisibilityGrid, proposed: list[Target],
                  active_15d: list[str], active_30d: list[str],
                  client: WebClient, channel_id: str, activity: Activity,
                  candidates: list[Target] | None = None, *, debug: bool = False) -> str:
    """Post the full Block Kit scheduling proposal to Slack.

    Parameters
    ----------
    obs_window:
        The upcoming observation window (project, start, end, antennas).
    visibility_grid:
        Per-source, per-15-min-slot antenna counts for all candidate targets.
    proposed:
        Ordered list of targets selected for the observation.
    active_15d:
        Canonical names of sources with at least one detection in the last 15 days.
    active_30d:
        Canonical names of sources with at least one detection in the last 30 days.
    client:
        Authenticated Slack :class:`~slack_sdk.WebClient`.
    channel_id:
        Slack channel to post into.
    activity:
        :class:`~precise_scheduler.activity.Activity` instance used to
        resolve per-source detection counts and last-detection dates.
    candidates:
        All candidate targets offered for interactive selection in the actions
        block.  Defaults to *proposed* when not supplied.

    Returns
    -------
    str
        The Slack message timestamp (``ts``) for use by ``await_confirmation``.

    Raises
    ------
    SlackError
        If the Slack API call fails.
    """
    blocks = build_proposal_blocks(
        obs_window=obs_window,
        visibility_grid=visibility_grid,
        proposed=proposed,
        active_15d=active_15d,
        active_30d=active_30d,
        activity=activity,
        candidates=candidates,
    )

    fallback_text = proposal_fallback_text(obs_window)

    if debug:
        print("\n" + "=" * 70)
        print(f"[DEBUG] Slack proposal (would post to channel {channel_id}):")
        print(f"  {fallback_text}")
        print(f"  Proposed sources: {[t.canonical_name for t in proposed]}")
        print("=" * 70 + "\n")
        log.info("[DEBUG] Skipping Slack post (debug mode). Returning dummy ts.")
        return "debug-ts-0000000000.000000"

    try:
        response = client.chat_postMessage(
            channel=channel_id,
            blocks=blocks,
            text=fallback_text,
        )
    except SlackApiError as exc:
        raise SlackError(
            f"Failed to post scheduling proposal: {exc.response['error']}"
        ) from exc

    ts: str = response["ts"]
    log.info(
        "Posted scheduling proposal for %s to channel %s (ts=%s)",
        obs_window.project,
        channel_id,
        ts,
    )
    return ts


# ---------------------------------------------------------------------------
# Issue #9 — interactive confirmation machinery
# ---------------------------------------------------------------------------

# Module-level queue used to pass payloads from the HTTP handler to the main
# loop.  Using a module-level queue means the handler class can access it
# without needing a closure (BaseHTTPRequestHandler does not support easy
# dependency injection).
_payload_queue: queue.Queue[dict[str, Any]] = queue.Queue()


def _verify_slack_signature(signing_secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    """Return ``True`` if the Slack request signature is valid.

    Parameters
    ----------
    signing_secret:
        The Slack signing secret from the app configuration.
    body:
        Raw POST body bytes.
    timestamp:
        Value of the ``X-Slack-Request-Timestamp`` header.
    signature:
        Value of the ``X-Slack-Signature`` header (``v0=<hex>``).
    """
    try:
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        return False

    # Reject requests older than 5 minutes to prevent replay attacks.
    if abs(time.time() - ts_int) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


class _SlackInteractiveHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that accepts Slack interactive payloads."""

    # Set on the class before the server is started.
    signing_secret: str = ""

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")

        # Always respond 200 immediately — Slack requires a fast response.
        self.send_response(200)
        self.end_headers()

        if self.signing_secret and not _verify_slack_signature(
            self.signing_secret, body, timestamp, signature
        ):
            log.warning("Rejected Slack request with invalid signature.")
            return

        try:
            parsed = urllib.parse.parse_qs(body.decode("utf-8"))
            payload_str = parsed.get("payload", [""])[0]
            payload = json.loads(payload_str)
        except Exception:
            log.exception("Failed to parse Slack interactive payload.")
            return

        _payload_queue.put(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress default Apache-style HTTP server log output."""


def _post_reminder(
    client: WebClient,
    channel_id: str,
    message_ts: str,
    obs_window: ObsWindow,
) -> None:
    """Post a thread reminder that confirmation is still outstanding.

    Logs and swallows any :class:`SlackApiError` so the caller can continue.
    """
    start_str = obs_window.start.strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"⏰ Reminder: the scheduling proposal for *{obs_window.project}* "
        f"(starting {start_str}) has not yet been confirmed. "
        "Please use the buttons above to confirm or override the source list."
    )
    try:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=text,
        )
        log.info(
            "Posted reminder for %s in channel %s (thread_ts=%s)",
            obs_window.project,
            channel_id,
            message_ts,
        )
    except SlackApiError:
        log.exception(
            "Failed to post reminder for %s — will continue waiting.",
            obs_window.project,
        )


def _process_payload(payload: dict[str, Any], candidates: list[Target], client: WebClient, channel_id: str,
                     proposed: list[Target] | None = None) -> list[Target] | None:
    """Process one Slack interactive payload.

    Returns the confirmed target list when a valid action is received, or
    ``None`` if the payload cannot be processed (logged at DEBUG/WARNING).
    Any user who can see the channel may confirm or override.

    Parameters
    ----------
    payload:
        Parsed Slack interactive payload dict.
    candidates:
        Every target offered in the override dropdown.
    client:
        Slack Web API client.
    channel_id:
        Channel to post ephemeral replies into.
    proposed:
        The sources the message actually proposes — what "Confirm" accepts.
        Defaults to *candidates* for callers that offer no wider choice.
    """
    accepted = candidates if proposed is None else proposed
    user_id: str = payload.get("user", {}).get("id", "")
    if not user_id:
        log.warning("Received payload with no user id; ignoring.")
        return None

    actions: list[dict[str, Any]] = payload.get("actions", [])
    if not actions:
        log.warning("Received payload with no actions; ignoring.")
        return None

    action = actions[0]
    action_id: str = action.get("action_id", "")

    if action_id == "confirm_proposal":
        # Confirming accepts the proposal as posted — not every source that
        # was offered in the override dropdown.
        log.info(
            "User %s confirmed the proposal: %s",
            user_id, [t.name for t in accepted],
        )
        return list(accepted)

    if action_id == "submit_override":
        selected_options: list[dict[str, Any]] = action.get("selected_options", [])
        selected_names: set[str] = {opt["value"] for opt in selected_options}
        # Return matching targets in the same order as candidates.
        result = [t for t in candidates if t.name in selected_names]
        log.info(
            "User %s submitted override with sources: %s",
            user_id,
            [t.name for t in result],
        )
        return result

    log.warning("Unknown action_id %r; ignoring.", action_id)
    return None


def await_confirmation(message_ts: str, candidates: list[Target], obs_window: ObsWindow,
                       client: WebClient, channel_id: str, port: int = 8765, *,
                       proposed: list[Target] | None = None, debug: bool = False,
                       _server_factory: Any = None) -> list[Target]:
    """Start an HTTP server, wait for a Slack interactive payload, and return
    the confirmed source list.

    Behaviour
    ---------
    - Starts a lightweight HTTP server on *port* (or uses *_server_factory*
      for testing).
    - Slack sends a POST to ``http://<host>:<port>/slack/interactive`` when a
      user clicks **Confirm** or **Submit override**.
    - Validates the Slack request signature (``X-Slack-Signature`` header).
    - Any user who can see the channel is allowed to confirm or override.
    - On ``"confirm_proposal"`` action: returns *proposed* — the sources the
      message actually proposes, which is normally a small subset of the
      *candidates* offered in the dropdown.
    - On ``"submit_override"`` action: parses the selected values from the
      multi-select dropdown and returns the matching :class:`Target` objects.
    - Reminder: if ``datetime.now(UTC) >= obs_window.start - timedelta(days=5)``
      and no confirmation has arrived yet, posts a reminder thread reply once
      and keeps waiting.
    - Waits indefinitely; caller is responsible for any outer timeout.
    - Shuts the HTTP server down cleanly after confirmation is received.

    Parameters
    ----------
    message_ts:
        Timestamp of the proposal message (for thread replies).
    candidates:
        All candidate sources offered for selection (used for override mapping).
    proposed:
        The sources the proposal actually asks for — what "Confirm" accepts.
        Defaults to *candidates*, i.e. every offered source, which is only
        right when the proposal offers no wider choice.
    obs_window:
        Used to compute the reminder threshold (``obs_window.start - 5 days``).
    client:
        Slack Web API client.
    channel_id:
        Channel where the proposal was posted.
    port:
        Port the HTTP server listens on.
    _server_factory:
        If provided, called instead of starting a real HTTP server.  Must
        return an object with a ``shutdown()`` method.  Used in tests to
        inject a fake server that pre-loads payloads into ``_payload_queue``.

    Returns
    -------
    list[Target]
        The confirmed (or overridden) list of targets.

    Raises
    ------
    SlackError
        If an unrecoverable error occurs while posting a reminder or ephemeral
        reply.  Single API failures are logged and swallowed — only
        unrecoverable conditions propagate.
    """
    accepted = candidates if proposed is None else proposed

    if debug:
        print("\n" + "=" * 70)
        print(f"[DEBUG] Awaiting confirmation for proposal ts={message_ts}")
        print(f"[DEBUG] Auto-confirming the {len(accepted)} proposed source(s) "
              f"(of {len(candidates)} offered).")
        for t in accepted:
            print(f"[DEBUG]   {t.canonical_name or t.name}")
        print("=" * 70 + "\n")
        log.info("[DEBUG] Auto-confirmed in debug mode.")
        return list(accepted)

    # Clear any stale payloads left from a previous call (e.g. in tests).
    while not _payload_queue.empty():
        try:
            _payload_queue.get_nowait()
        except queue.Empty:
            break

    if _server_factory is not None:
        server = _server_factory(port)
    else:
        handler_class = type(
            "_BoundSlackHandler",
            (_SlackInteractiveHandler,),
            {"signing_secret": ""},  # signing_secret injected by caller if needed
        )
        server = HTTPServer(("", port), handler_class)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        log.info("Slack interactive webhook listening on port %d.", port)

    reminder_sent = False
    reminder_threshold = obs_window.start - timedelta(days=5)

    try:
        while True:
            # ---- reminder check ------------------------------------------------
            now = datetime.now(timezone.utc)
            if not reminder_sent and now >= reminder_threshold:
                _post_reminder(client, channel_id, message_ts, obs_window)
                reminder_sent = True

            # ---- payload check (non-blocking, 60-second poll) ------------------
            try:
                payload = _payload_queue.get(timeout=60)
            except queue.Empty:
                continue

            result = _process_payload(
                payload, candidates, client, channel_id, proposed=accepted
            )
            if result is not None:
                return result
            # Unauthorised user — loop and keep waiting.
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Plan summary — posted by the daemon when a run is coming up                  #
# --------------------------------------------------------------------------- #

def _section_blocks(rows: list[str]) -> list[dict[str, Any]]:
    """Pack *rows* into as few section blocks as Slack's size limit allows.

    One long section is nicer to read than many short ones, but a section over
    :data:`SECTION_TEXT_LIMIT` characters makes Slack reject the entire
    message — so a listing that outgrows one section is continued in the next.
    """
    blocks: list[dict[str, Any]] = []
    chunk: list[str] = []
    length = 0
    for row in rows:
        # +1 for the newline that will join this row to the previous one.
        if chunk and length + len(row) + 1 > SECTION_TEXT_LIMIT:
            blocks.append(
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": "\n".join(chunk)}}
            )
            chunk, length = [], 0
        chunk.append(row)
        length += len(row) + 1
    if chunk:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}}
        )
    return blocks


# --------------------------------------------------------------------------- #
# The draft schedule file, posted for review                                   #
# --------------------------------------------------------------------------- #

def key_file_message(project: str, sources: list[Target]) -> str:
    """Wording for the draft .key upload: a template, not a final schedule."""
    named = ", ".join(t.canonical_name or t.name for t in sources)
    return (
        f"*{project} — prepared observation template.*\n"
        f"This is the draft schedule for review, not the final one: check the "
        f"sources, the scan pattern and the frequency setup before it is "
        f"distributed.\n"
        f"Scheduled sources: {named}."
    )


def post_key_file(
    key_file: Path,
    project: str,
    sources: list[Target],
    client: WebClient,
    channel_id: str,
    thread_ts: str | None = None,
    debug: bool = False,
) -> str | None:
    """Upload the draft ``.key`` file to the channel for revision.

    Needs the ``files:write`` bot scope — ``chat:write`` alone is not enough,
    and Slack answers ``missing_scope`` without it.

    Returns the uploaded file's permalink, or ``None`` when nothing was sent.
    A failure here is logged and swallowed: the schedule has already been
    generated and committed, and losing the upload is not worth aborting over.
    """
    key_file = Path(key_file)
    comment = key_file_message(project, sources)

    if debug:
        log.info(
            "[DEBUG] Would upload %s to %s:\n%s", key_file, channel_id, comment
        )
        return None

    if not key_file.is_file():
        log.warning("No .key file at %s to post to Slack.", key_file)
        return None

    try:
        response = client.files_upload_v2(
            channel=channel_id,
            file=str(key_file),
            filename=key_file.name,
            title=f"{project} — draft schedule",
            initial_comment=comment,
            thread_ts=thread_ts,
        )
    except Exception as exc:
        hint = (
            "  The bot needs the 'files:write' scope to upload files."
            if "missing_scope" in str(exc)
            else ""
        )
        log.warning(
            "Could not upload %s to Slack: %s%s", key_file.name, exc, hint
        )
        return None

    permalink = (response.get("file") or {}).get("permalink")
    log.info("Posted %s to %s for review.", key_file.name, channel_id)
    return permalink


# --------------------------------------------------------------------------- #
# The observability report — the whole of `precise plan`                       #
# --------------------------------------------------------------------------- #

def _grid_emoji(count: int, n_antennas: int) -> str:
    """Colour for one cell: all antennas, some of them, or none."""
    if count <= 0 or n_antennas <= 0:
        return EMOJI_NONE
    return EMOJI_ALL if count >= n_antennas else EMOJI_MOST


def _visibility_row(slots: list[int], n_antennas: int) -> str:
    """Collapse 15-minute slots into :data:`GRID_MINUTES` cells.

    A cell takes the *worst* coverage over the period it spans, so a green
    cell means the whole array was on source for the entire half hour rather
    than merely part of it.  Cells are written without separators: at one per
    half hour a ten-hour run is 20 characters wide.
    """
    per_cell = max(1, int(GRID_MINUTES // SLOT_MINUTES))
    return "".join(
        _grid_emoji(min(slots[i:i + per_cell]), n_antennas)
        for i in range(0, len(slots), per_cell)
    )


def _plan_summary_block(plan: "PlanResult") -> dict[str, Any]:
    """What run this is: project, window, array, and what was considered."""
    w = plan.obs_window
    hours = (w.end - w.start).total_seconds() / 3600.0
    origin = "from the calendar" if plan.from_calendar else "from --epoch"
    checked = (
        f"the {plan.n_candidates} candidate(s) named with --targets"
        if plan.forced
        else f"{plan.n_candidates} pre-approved candidate(s)"
    )
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*Observation {w.project}*  _{origin}_\n"
                f"{w.start:%d %b %Y %H:%M} to {w.end:%H:%M} UTC ({hours:.1f} h)\n"
                f"{plan.n_array} antennas: {', '.join(plan.array)}\n"
                f"planobs checked {checked}; kept those observable by "
                f"{plan.min_antennas} antennas or more."
            ),
        },
    }


def _plan_activity_block(plan: "PlanResult") -> dict[str, Any]:
    """Which sources have been detected recently, and how often."""
    lines = ["*Activity summary*"]

    def _render(counted: list[tuple[str, int]]) -> str:
        return ", ".join(
            f"{name} ({n} detection{'s' if n != 1 else ''})" for name, n in counted
        )

    recent = plan.active_15d
    lines.append(
        f"Active in the last {ACTIVE_15D_LABEL} days: "
        + (_render(recent) if recent else "_none_")
    )

    seen = {name for name, _ in recent}
    older = [(name, n) for name, n in plan.active_30d if name not in seen]
    lines.append(
        f"Active in the last {ACTIVE_30D_LABEL} days only: "
        + (_render(older) if older else "_none_")
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _previous_runs_block(recent_runs: list[Any]) -> dict[str, Any]:
    """PRECISE's own recent runs, listing only the FRBs each one observed."""
    if not recent_runs:
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Previous runs:* _none on record_"},
        }

    lines = []
    for record in recent_runs:
        names = ", ".join(st.name for st in record.sources) or "—"
        lines.append(f"{record.project:<8} {record.start:%d %b %Y}  {names}")
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Previous runs*\n```\n" + "\n".join(lines) + "\n```",
        },
    }


def _fenced_sections(rows: list[str]) -> list[dict[str, Any]]:
    """Pack *rows* into monospace sections, respecting Slack's size limit.

    The table only lines up in a fixed-width font, and a fence is the only way
    to get one in Slack.  The terminal renderer emits fenced text verbatim for
    the same reason.
    """
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "```\n" + block["text"]["text"] + "\n```",
            },
        }
        for block in _section_blocks(rows)
    ]


def _plan_table_sections(plan: "PlanResult") -> list[dict[str, Any]]:
    """The observable FRBs, one row each, with their visibility grid."""
    if not plan.observable:
        return [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"_No candidate can be observed by at least "
                    f"{plan.min_antennas} of the {plan.n_array} antennas in "
                    f"this window._"
                ),
            },
        }]

    w = plan.obs_window
    header = (
        f"*Observable FRBs*  ({w.start:%H:%M} to {w.end:%H:%M} UTC, "
        f"{int(GRID_MINUTES)} min per cell)\n"
        f"{EMOJI_ALL} all {plan.n_array} antennas · {EMOJI_MOST} some · "
        f"{EMOJI_NONE} none"
    )
    rows = [
        f"{s.target.name:<12} {s.target.canonical_name or '—':<13} "
        f"{s.hours_at_best:>5.1f}h  {_visibility_row(s.slots, plan.n_array)}"
        for s in plan.observable
    ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        *_fenced_sections(rows),
    ]


def build_plan_messages(plan: "PlanResult") -> list[list[dict[str, Any]]]:
    """The report as a list of messages, in order.

    Slack caps both the size of a section and the number of blocks in one
    message, and the FRB table is the part that grows without bound — so it
    goes in its own message, or several.  The terminal renders the same
    messages one after another, which is what keeps the two outputs identical.
    """
    w = plan.obs_window
    head: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{w.project}: {len(plan.observable)} FRB observable",
            },
        },
        _plan_summary_block(plan),
        _divider_block(),
        _plan_activity_block(plan),
        _divider_block(),
        _previous_runs_block(plan.recent_runs),
    ]

    table = _plan_table_sections(plan)
    messages = [head]
    for start in range(0, len(table), MAX_BLOCKS_PER_MESSAGE):
        messages.append(table[start:start + MAX_BLOCKS_PER_MESSAGE])
    return messages


def plan_fallback_text(plan: "PlanResult") -> str:
    """Notification text for the plan summary."""
    return (
        f"{plan.obs_window.project}: {len(plan.observable)} FRB(s) observable "
        f"by {plan.min_antennas}+ of {plan.n_array} antennas"
    )


def post_plan(
    plan: "PlanResult",
    client: WebClient,
    channel_id: str,
    thread_ts: str | None = None,
    debug: bool = False,
) -> str | None:
    """Post the report to Slack, one message per part.

    Returns the timestamp of the first message, which the rest are *not*
    threaded under — they read as a continuation of the same report.
    """
    messages = build_plan_messages(plan)
    text = plan_fallback_text(plan)

    if debug:
        log.info(
            "[DEBUG] Would post %d plan message(s) to %s:\n%s",
            len(messages), channel_id, text,
        )
        for source in plan.observable:
            log.info(
                "[DEBUG]   %s (%s): %d antennas",
                source.target.name,
                source.target.canonical_name or "-",
                source.n_antennas,
            )
        return "debug-ts-0000000000.000000"

    first_ts: str | None = None
    for index, blocks in enumerate(messages):
        try:
            response = client.chat_postMessage(
                channel=channel_id,
                blocks=blocks,
                text=text if index == 0 else f"{text} (continued)",
                thread_ts=thread_ts,
            )
        except Exception as exc:
            raise SlackError(f"Could not post the plan summary: {exc}") from exc
        if first_ts is None:
            first_ts = response["ts"]
    return first_ts
