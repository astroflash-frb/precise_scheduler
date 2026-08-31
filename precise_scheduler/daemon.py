"""
PRECISE Scheduler daemon — main orchestration loop.

Polls the Google Sheets calendar every POLL_INTERVAL_HOURS hours.
When an observation window is found within LOOKAHEAD_DAYS, runs the
full scheduling pipeline once. Uses state.json to avoid re-triggering.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from slack_sdk import WebClient

from .config import Config

log = logging.getLogger(__name__)

STATE_FILE_NAME = "state.json"   # relative to schedules_repo


class ScheduleDaemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._state_path = Path(config.schedules_repo) / STATE_FILE_NAME
        self._client = WebClient(token=config.slack_bot_token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> bool:
        """
        Single poll iteration. Returns True if a scheduling pipeline was
        triggered, False if nothing was done.

        Steps:
        1. Read the Google Sheets calendar → ObsWindow or None
        2. If None or > lookahead_days away: return False
        3. Check state.json: if obs_window.start.isoformat() already in state: return False
        4. Run the full pipeline (see _run_pipeline)
        5. Write obs_window.start.isoformat() to state.json atomically
        6. Return True
        """
        from .calendar import CalendarError, ObservationCalendar
        from .alerts import post_alert, calendar_read_failure

        # Step 1: read calendar
        try:
            calendar = ObservationCalendar(
                spreadsheet_id=self.config.google_spreadsheet_id,
            )
            obs_window = calendar.get_next_observation(
                lookahead_days=self.config.lookahead_days
            )
        except CalendarError as exc:
            msg = calendar_read_failure(details=str(exc))
            post_alert(msg, self._client, self.config.slack_channel_id)
            raise

        # Step 2: nothing to do if no window found
        if obs_window is None:
            log.debug("No upcoming observation found; skipping pipeline.")
            return False

        # Step 2b: double-check lookahead (calendar already filters, but be explicit)
        now = datetime.now(timezone.utc)
        if obs_window.start > now + timedelta(days=self.config.lookahead_days):
            log.debug(
                "Observation %s at %s is beyond lookahead (%d days); skipping.",
                obs_window.project,
                obs_window.start.isoformat(),
                self.config.lookahead_days,
            )
            return False

        # Step 3: check state to avoid duplicate runs
        if self._is_already_scheduled(obs_window):
            log.info(
                "Observation %s (%s) already scheduled; skipping pipeline.",
                obs_window.project,
                obs_window.start.isoformat(),
            )
            return False

        # Step 4: run pipeline
        self._run_pipeline(obs_window, debug=self.config.debug)

        # Step 5: persist state
        self._mark_scheduled(obs_window)

        return True

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(self, obs_window, *, debug: bool = False) -> None:
        """
        Full scheduling pipeline for one observation window:
        1. pipeline.run_plan — the observability report: pre-approved sources,
           planobs visibility, activity and previous-run summaries
        2. post_plan — put that report in the channel
        3. selector.propose — narrow the observable list to a proposal
        4. post_proposal → message_ts
        5. await_confirmation → confirmed_targets
        6. VisibilityPlanner.generate_schedule(confirmed_targets, obs_window, repo)
        6b. post_key_file — put the draft .key in the channel for review
        7. SchedulePublisher.commit_schedule(project, draft=True)

        Error handling:
        - CalendarError: post calendar_read_failure alert, re-raise
        - VisibilityError: post planobs_failure alert, re-raise
        - Any other exception: log at ERROR with traceback, post generic alert, re-raise
        """
        from .activity import Activity
        from .alerts import (
            calendar_read_failure,
            planobs_failure,
            post_alert,
        )
        from .calendar import CalendarError
        from .publisher import SchedulePublisher
        from .selector import propose
        from .pipeline import run_plan
        from .slack_scheduler import (
            await_confirmation,
            post_key_file,
            post_plan,
            post_proposal,
        )
        from .visibility import VisibilityError, VisibilityPlanner

        try:
            # Step 1: work out what is observable at all.  run_plan is the
            # same stage `precise plan` runs, so the daemon and the command
            # cannot drift apart -- it loads the targets, pre-approves the
            # candidates, asks planobs, and gathers the activity and
            # previous-run summaries.
            log.info("Step 1: building the observability report")
            plan = run_plan(self.config, obs_window=obs_window)
            observable = plan.observable
            visibility_grid = {s.target.name: s.slots for s in observable}
            log.info(
                "%d of %d candidates observable by >= %d of %d antennas",
                len(observable), plan.n_candidates, plan.min_antennas,
                plan.n_array,
            )

            # Step 2: tell the channel what can be observed at all.
            log.info("Step 2: posting the observability report to Slack")
            post_plan(
                plan,
                client=self._client,
                channel_id=self.config.slack_channel_id,
                debug=debug,
            )

            # Step 3: propose candidates, from the observable ones only
            log.info("Step 3: selecting sources")
            activity = Activity(self.config.activity_db)
            proposed = propose(
                targets=[s.target for s in observable],
                activity=activity,
                visibility_grid=visibility_grid,
            )
            log.info("Proposed sources: %s", [t.canonical_name for t in proposed])

            # Step 4: post Slack proposal.  The activity names come from the
            # report, so both messages agree on who is active.
            active_15d = [name for name, _ in plan.active_15d]
            active_30d = [name for name, _ in plan.active_30d]
            log.info("Step 4: posting Slack proposal")
            message_ts = post_proposal(
                obs_window=obs_window,
                visibility_grid=visibility_grid,
                proposed=proposed,
                active_15d=active_15d,
                active_30d=active_30d,
                client=self._client,
                channel_id=self.config.slack_channel_id,
                activity=activity,
                candidates=[s.target for s in observable],
                debug=debug,
            )

            # Step 5: wait for human confirmation
            log.info("Step 5: awaiting confirmation (ts=%s)", message_ts)
            confirmed_targets = await_confirmation(
                message_ts=message_ts,
                # Confirm accepts the proposal; the override dropdown may pick
                # any observable source instead.
                proposed=proposed,
                candidates=[s.target for s in observable],
                obs_window=obs_window,
                client=self._client,
                channel_id=self.config.slack_channel_id,
                port=self.config.slack_webhook_port,
                debug=debug,
            )
            log.info(
                "Confirmed targets: %s", [t.canonical_name for t in confirmed_targets]
            )

            # Step 6: generate schedule file
            log.info("Step 6: generating schedule")
            planner = VisibilityPlanner(self.config.targets_toml)
            key_file = planner.generate_schedule(
                targets=confirmed_targets,
                obs_window=obs_window,
                output_path=Path(self.config.schedules_repo),
            )

            # Step 6b: put the draft in the channel for someone to review.
            log.info("Step 6b: posting the draft schedule to Slack")
            post_key_file(
                key_file,
                project=obs_window.project,
                sources=confirmed_targets,
                client=self._client,
                channel_id=self.config.slack_channel_id,
                debug=debug,
            )

            # Step 7: commit draft schedule
            log.info("Step 7: committing draft schedule")
            publisher = SchedulePublisher(self.config)
            publisher.commit_schedule(obs_window.project, draft=True, debug=debug)

            log.info(
                "Pipeline complete for project %s (%s)",
                obs_window.project,
                obs_window.start.isoformat(),
            )

        except CalendarError as exc:
            msg = calendar_read_failure(details=str(exc))
            post_alert(msg, self._client, self.config.slack_channel_id)
            raise

        except VisibilityError as exc:
            source_names = [t.canonical_name for t in proposed] if "proposed" in dir() else []
            msg = planobs_failure(source_list=source_names, details=str(exc))
            post_alert(msg, self._client, self.config.slack_channel_id)
            raise

        except Exception as exc:
            log.error(
                "Unexpected error in scheduling pipeline for project %s: %s",
                obs_window.project,
                exc,
                exc_info=True,
            )
            post_alert(
                f"Unexpected error in PRECISE scheduling pipeline: {exc}",
                self._client,
                self.config.slack_channel_id,
            )
            raise

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        """Load state.json; return {} if missing or corrupt."""
        if not self._state_path.exists():
            return {}
        try:
            with self._state_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file %s: %s — starting with empty state.", self._state_path, exc)
            return {}

    def _save_state(self, state: dict) -> None:
        """Atomically write state.json."""
        parent = self._state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=parent, prefix=".state_tmp_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._state_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _is_already_scheduled(self, obs_window) -> bool:
        """Check if this observation's start time is already in state."""
        state = self._load_state()
        key = obs_window.start.isoformat()
        return key in state.get("scheduled", {})

    def _mark_scheduled(self, obs_window) -> None:
        """Add this observation's start time to state.json atomically."""
        state = self._load_state()
        scheduled = state.setdefault("scheduled", {})
        key = obs_window.start.isoformat()
        scheduled[key] = {
            "project": obs_window.project,
            "marked_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state(state)
        log.debug("Marked %s (%s) as scheduled in state.", obs_window.project, key)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(config: Config) -> None:
    """Entry point for the daemon. Runs indefinitely."""
    log.info(
        "PRECISE Scheduler daemon started — polling every %d hours",
        config.poll_interval_hours,
    )
    daemon = ScheduleDaemon(config)
    while True:
        try:
            daemon.run_once()
        except Exception:
            log.exception("Unhandled exception in daemon poll loop; will retry")
        sleep_seconds = config.poll_interval_hours * 3600
        log.debug("Sleeping %d s until next poll", sleep_seconds)
        time.sleep(sleep_seconds)
