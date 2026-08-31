"""Tests for precise_scheduler.daemon (Issue #14)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from precise_scheduler.calendar import ObsWindow
from precise_scheduler.daemon import ScheduleDaemon, STATE_FILE_NAME


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    """Minimal Config-like object for testing."""
    (tmp_path / "targets.toml").write_text("")
    return SimpleNamespace(
        targets_toml=tmp_path / "targets.toml",
        schedules_repo=tmp_path,
        activity_db=tmp_path / "activity.csv",
        google_credentials_file=tmp_path / "creds.json",
        google_spreadsheet_id="abc",
        slack_bot_token="xoxb-test",
        slack_channel_id="C123",
        slack_signing_secret="secret",
        slack_webhook_port=8765,
        slack_authorised_users=["a@b.com"],
        mattermost_webhook_url="https://mm.example.com/hooks/x",
        antenna_host="vlbeer.ira.inaf.it",
        antenna_user="vlbeer",
        antenna_ssh_key=tmp_path / "key",
        antenna_base_path="vlbi_arch",
        poll_interval_hours=6,
        lookahead_days=10,
        debug=False,
    )


def make_obs_window(days_ahead: int = 5) -> ObsWindow:
    """Return an ObsWindow starting days_ahead days from now."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(days=days_ahead)
    end = start + timedelta(hours=8)
    return ObsWindow(start=start, end=end, antennas=["Ef", "Wb"], project="RP036A")


# ---------------------------------------------------------------------------
# Helper: build a ScheduleDaemon with a mocked Slack client
# ---------------------------------------------------------------------------


def _make_daemon(cfg: SimpleNamespace) -> ScheduleDaemon:
    with patch("precise_scheduler.daemon.WebClient"):
        daemon = ScheduleDaemon(cfg)
    daemon._client = MagicMock()
    return daemon


# ---------------------------------------------------------------------------
# run_once — no observation
# ---------------------------------------------------------------------------


def test_run_once_returns_false_when_no_observation(
    cfg: SimpleNamespace, mocker
) -> None:
    """run_once must return False when the calendar finds no upcoming observation."""
    daemon = _make_daemon(cfg)

    mock_calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
    mock_calendar_cls.return_value.get_next_observation.return_value = None

    result = daemon.run_once()

    assert result is False
    mock_calendar_cls.return_value.get_next_observation.assert_called_once_with(
        lookahead_days=cfg.lookahead_days
    )


# ---------------------------------------------------------------------------
# run_once — observation beyond lookahead
# ---------------------------------------------------------------------------


def test_run_once_returns_false_when_beyond_lookahead(
    cfg: SimpleNamespace, mocker
) -> None:
    """run_once must return False when the ObsWindow is beyond lookahead_days."""
    daemon = _make_daemon(cfg)

    # The calendar's own filter already excluded it, but let's return a window
    # that is 20 days away (beyond the 10-day lookahead) to exercise the
    # daemon's own guard.
    far_window = make_obs_window(days_ahead=20)

    mock_calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
    mock_calendar_cls.return_value.get_next_observation.return_value = far_window

    result = daemon.run_once()

    assert result is False


# ---------------------------------------------------------------------------
# run_once — already scheduled
# ---------------------------------------------------------------------------


def test_run_once_returns_false_when_already_in_state(
    cfg: SimpleNamespace, mocker
) -> None:
    """run_once must return False (and not call the pipeline) when the
    observation is already present in state.json."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)

    # Pre-populate state so the obs is already scheduled.
    daemon._mark_scheduled(obs)

    mock_calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
    mock_calendar_cls.return_value.get_next_observation.return_value = obs

    pipeline_mock = mocker.patch.object(daemon, "_run_pipeline")

    result = daemon.run_once()

    assert result is False
    pipeline_mock.assert_not_called()


# ---------------------------------------------------------------------------
# run_once — new observation triggers pipeline
# ---------------------------------------------------------------------------


def test_run_once_triggers_pipeline_for_new_observation(
    cfg: SimpleNamespace, mocker
) -> None:
    """run_once must call _run_pipeline when a new observation is found."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)

    mock_calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
    mock_calendar_cls.return_value.get_next_observation.return_value = obs

    pipeline_mock = mocker.patch.object(daemon, "_run_pipeline")

    result = daemon.run_once()

    assert result is True
    pipeline_mock.assert_called_once_with(obs, debug=False)


# ---------------------------------------------------------------------------
# run_once — state written after pipeline
# ---------------------------------------------------------------------------


def test_run_once_writes_state_after_pipeline(
    cfg: SimpleNamespace, mocker
) -> None:
    """After a successful pipeline run, the observation start must appear in state.json."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)

    mock_calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
    mock_calendar_cls.return_value.get_next_observation.return_value = obs

    mocker.patch.object(daemon, "_run_pipeline")

    daemon.run_once()

    state = daemon._load_state()
    assert obs.start.isoformat() in state.get("scheduled", {})


# ---------------------------------------------------------------------------
# run_once — state persists across instances
# ---------------------------------------------------------------------------


def test_run_once_state_persists_across_instances(
    cfg: SimpleNamespace, mocker
) -> None:
    """A second ScheduleDaemon pointing at the same schedules_repo must
    see the state written by the first instance and not re-trigger the pipeline."""
    obs = make_obs_window(days_ahead=5)

    # First instance runs the pipeline and writes state.
    daemon1 = _make_daemon(cfg)

    mock_calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
    mock_calendar_cls.return_value.get_next_observation.return_value = obs

    mocker.patch.object(daemon1, "_run_pipeline")
    daemon1.run_once()

    # Second instance: pipeline must NOT be called.
    daemon2 = _make_daemon(cfg)
    pipeline_mock = mocker.patch.object(daemon2, "_run_pipeline")

    # Re-patch calendar for second daemon (same mock, still returns obs)
    result = daemon2.run_once()

    assert result is False
    pipeline_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _run_pipeline — steps in order
# ---------------------------------------------------------------------------


def _make_target(name: str, canonical: str, priority: int):
    from precise_scheduler.targets import Coordinates, Target

    return Target(
        name=name,
        canonical_name=canonical,
        priority=priority,
        coordinates=Coordinates(ra="00:33:03.6", dec="+28:49:51.6"),
    )


def _make_plan(sources, array: list[str], observable=None, n_slots: int = 4):
    """A real PlanResult, as ``run_plan`` would return it.

    *sources* maps a Target to how many of *array* can observe it; *observable*
    names the subset that survived the cut (all of them by default).
    """
    from precise_scheduler.pipeline import PlanResult
    from precise_scheduler.visibility import SourceObservability

    def _obs(target, n):
        return SourceObservability(
            target=target,
            n_antennas=n,
            antennas=array[:n],
            slots=[n] * n_slots,
            slots_at_best=n_slots,
            n_slots=n_slots,
        )

    every = [_obs(t, n) for t, n in sources.items()]
    kept_names = {t.name for t in (observable if observable is not None else sources)}
    kept = [s for s in every if s.target.name in kept_names]
    return PlanResult(
        obs_window=make_obs_window(days_ahead=5),
        from_calendar=True,
        array=array,
        n_slots=n_slots,
        min_antennas=max(1, len(array) - 1),
        observable=kept,
        dropped=[s for s in every if s.target.name not in kept_names],
        n_candidates=len(every),
    )


def _stub_pipeline(mocker, plan, *, proposed=None, key_file=None):
    """Mock everything ``_run_pipeline`` reaches out to; return the mocks."""
    confirmed = proposed if proposed is not None else [s.target for s in plan.observable]
    planner = MagicMock()
    planner.generate_schedule.return_value = key_file
    return SimpleNamespace(
        run_plan=mocker.patch(
            "precise_scheduler.pipeline.run_plan", return_value=plan
        ),
        post_plan=mocker.patch(
            "precise_scheduler.slack_scheduler.post_plan", return_value="plan-ts"
        ),
        activity=mocker.patch(
            "precise_scheduler.activity.Activity", return_value=MagicMock()
        ),
        propose=mocker.patch(
            "precise_scheduler.selector.propose", return_value=confirmed
        ),
        post_proposal=mocker.patch(
            "precise_scheduler.slack_scheduler.post_proposal", return_value="ts"
        ),
        await_confirmation=mocker.patch(
            "precise_scheduler.slack_scheduler.await_confirmation",
            return_value=confirmed,
        ),
        post_key_file=mocker.patch(
            "precise_scheduler.slack_scheduler.post_key_file"
        ),
        planner=planner,
        planner_cls=mocker.patch(
            "precise_scheduler.visibility.VisibilityPlanner", return_value=planner
        ),
        publisher=mocker.patch(
            "precise_scheduler.publisher.SchedulePublisher", return_value=MagicMock()
        ),
    )


def test_pipeline_calls_steps_in_order(cfg: SimpleNamespace, mocker) -> None:
    """_run_pipeline must call external modules in the documented order."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    target = _make_target("R201124_A", "FRB20201124A", 2)
    plan = _make_plan({target: 2}, array=["Ef", "Wb"])
    key_file = cfg.schedules_repo / "RP036A.key"
    m = _stub_pipeline(mocker, plan, key_file=key_file)

    daemon._run_pipeline(obs)

    # Step 1 — the report is built once, for the window already in hand.
    m.run_plan.assert_called_once_with(daemon.config, obs_window=obs)

    # Step 2 — it reaches the channel before anything is proposed.
    m.post_plan.assert_called_once()
    assert m.post_plan.call_args.args[0] is plan
    assert m.post_plan.call_args.kwargs["channel_id"] == cfg.slack_channel_id

    # Step 3 — the selector only ever sees the observable sources.
    assert m.propose.call_args.kwargs["targets"] == [target]

    # Step 4-5 — proposal, then confirmation on that message.
    m.post_proposal.assert_called_once()
    assert m.post_proposal.call_args.kwargs["obs_window"] is obs
    m.await_confirmation.assert_called_once()
    assert m.await_confirmation.call_args.kwargs["message_ts"] == "ts"

    # Step 6-7 — schedule, post it for review, commit as a draft.
    m.planner.generate_schedule.assert_called_once_with(
        targets=[target], obs_window=obs, output_path=Path(cfg.schedules_repo)
    )
    m.post_key_file.assert_called_once()
    assert m.post_key_file.call_args.args[0] == key_file
    m.publisher.return_value.commit_schedule.assert_called_once_with(
        obs.project, draft=True, debug=False
    )


def test_pipeline_does_not_reread_the_calendar(
    cfg: SimpleNamespace, mocker
) -> None:
    """The window is already in hand; run_plan must not fetch Sheets again."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    target = _make_target("R201124_A", "FRB20201124A", 2)
    m = _stub_pipeline(mocker, _make_plan({target: 2}, array=["Ef", "Wb"]))
    calendar = mocker.patch("precise_scheduler.calendar.ObservationCalendar")

    daemon._run_pipeline(obs)

    assert m.run_plan.call_args.kwargs["obs_window"] is obs
    calendar.assert_not_called()


def test_pipeline_posts_the_draft_key_file_for_review(
    cfg: SimpleNamespace, mocker
) -> None:
    """The generated .key goes to the channel as a template to be revised."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    target = _make_target("R201124_A", "FRB20201124A", 2)
    key_file = cfg.schedules_repo / "RP036A.key"
    m = _stub_pipeline(
        mocker, _make_plan({target: 2}, array=["Ef", "Wb"]), key_file=key_file
    )

    daemon._run_pipeline(obs)

    m.post_key_file.assert_called_once()
    assert m.post_key_file.call_args.args[0] == key_file
    kwargs = m.post_key_file.call_args.kwargs
    assert kwargs["project"] == obs.project
    assert kwargs["sources"] == [target]
    assert kwargs["channel_id"] == cfg.slack_channel_id


def test_pipeline_posts_plan_before_asking_for_confirmation(
    cfg: SimpleNamespace, mocker
) -> None:
    """The channel learns what is observable before it is asked to confirm."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    target = _make_target("R201124_A", "FRB20201124A", 2)
    m = _stub_pipeline(mocker, _make_plan({target: 2}, array=["Ef", "Wb"]))

    order: list[str] = []
    m.post_plan.side_effect = lambda *a, **k: order.append("plan") or "plan-ts"
    m.post_proposal.side_effect = lambda *a, **k: order.append("proposal") or "ts"
    m.await_confirmation.side_effect = (
        lambda *a, **k: order.append("confirm") or [target]
    )
    m.post_key_file.side_effect = lambda *a, **k: order.append("key") or None

    daemon._run_pipeline(obs)

    assert order == ["plan", "proposal", "confirm", "key"]


def test_pipeline_proposes_only_from_the_observable_sources(
    cfg: SimpleNamespace, mocker
) -> None:
    """Sources the cut dropped never reach the selector or the dropdown."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    all_up = _make_target("R_ALL", "FRB20200101A", 2)
    most_up = _make_target("R_MOST", "FRB20200102A", 2)
    few_up = _make_target("R_FEW", "FRB20200103A", 2)

    plan = _make_plan(
        {all_up: 4, most_up: 3, few_up: 2},
        array=["Ef", "Wb", "Tr", "O8"],
        observable=[all_up, most_up],
    )
    m = _stub_pipeline(mocker, plan, proposed=[all_up])

    daemon._run_pipeline(obs)

    assert m.propose.call_args.kwargs["targets"] == [all_up, most_up]
    assert m.post_proposal.call_args.kwargs["candidates"] == [all_up, most_up]


def test_pipeline_offers_every_observable_source_but_proposes_a_few(
    cfg: SimpleNamespace, mocker
) -> None:
    """Confirm accepts the proposal; the override dropdown offers more.

    Passing the observable list as the proposal too would schedule every
    observable source the moment anyone clicked Confirm.
    """
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    chosen = _make_target("R_CHOSEN", "FRB20200101A", 2)
    other = _make_target("R_OTHER", "FRB20200102A", 2)

    plan = _make_plan({chosen: 2, other: 2}, array=["Ef", "Wb"])
    m = _stub_pipeline(mocker, plan, proposed=[chosen])

    daemon._run_pipeline(obs)

    kwargs = m.await_confirmation.call_args.kwargs
    assert kwargs["proposed"] == [chosen]
    assert kwargs["candidates"] == [chosen, other]


def test_pipeline_calendar_error_posts_alert(
    cfg: SimpleNamespace, mocker
) -> None:
    """CalendarError raised inside _run_pipeline must post a calendar_read_failure alert."""
    from precise_scheduler.calendar import CalendarError

    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)

    mocker.patch(
        "precise_scheduler.targets.load_targets",
        side_effect=CalendarError("sheets down"),
    )
    mock_post_alert = mocker.patch("precise_scheduler.alerts.post_alert")
    mocker.patch(
        "precise_scheduler.alerts.calendar_read_failure", return_value="cal fail msg"
    )

    with pytest.raises(CalendarError):
        daemon._run_pipeline(obs)

    mock_post_alert.assert_called_once_with(
        "cal fail msg", daemon._client, cfg.slack_channel_id
    )


# ---------------------------------------------------------------------------
# _run_pipeline — VisibilityError posts alert
# ---------------------------------------------------------------------------


def test_pipeline_visibility_error_posts_alert(
    cfg: SimpleNamespace, mocker
) -> None:
    """VisibilityError raised inside _run_pipeline must post a planobs_failure alert."""
    from precise_scheduler.visibility import VisibilityError

    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    target = _make_target("R201124_A", "FRB20201124A", 2)

    mocker.patch("precise_scheduler.targets.load_targets", return_value=[target])
    mocker.patch("precise_scheduler.activity.Activity", return_value=MagicMock())
    mocker.patch("precise_scheduler.selector.eligible", return_value=[target])

    mock_planner = MagicMock()
    mock_planner.observability.side_effect = VisibilityError("planobs failed")
    mocker.patch(
        "precise_scheduler.visibility.VisibilityPlanner", return_value=mock_planner
    )

    mock_post_alert = mocker.patch("precise_scheduler.alerts.post_alert")
    mocker.patch(
        "precise_scheduler.alerts.planobs_failure", return_value="planobs fail msg"
    )

    with pytest.raises(VisibilityError):
        daemon._run_pipeline(obs)

    mock_post_alert.assert_called_once_with(
        "planobs fail msg", daemon._client, cfg.slack_channel_id
    )


# ---------------------------------------------------------------------------
# _load_state — missing file
# ---------------------------------------------------------------------------


def test_load_state_returns_empty_dict_when_missing(cfg: SimpleNamespace) -> None:
    """_load_state must return {} when state.json does not exist."""
    daemon = _make_daemon(cfg)
    # Ensure no state file
    state_path = Path(cfg.schedules_repo) / STATE_FILE_NAME
    assert not state_path.exists()

    state = daemon._load_state()
    assert state == {}


# ---------------------------------------------------------------------------
# _save_state — atomic write / round-trip
# ---------------------------------------------------------------------------


def test_save_state_atomic(cfg: SimpleNamespace) -> None:
    """_save_state must write state.json; _load_state must read it back correctly."""
    daemon = _make_daemon(cfg)
    sample_state: dict = {"scheduled": {"2026-07-10T18:00:00+00:00": {"project": "RP036A"}}}

    daemon._save_state(sample_state)

    state_path = Path(cfg.schedules_repo) / STATE_FILE_NAME
    assert state_path.exists()

    loaded = daemon._load_state()
    assert loaded == sample_state


# ---------------------------------------------------------------------------
# _load_state — corrupt file
# ---------------------------------------------------------------------------


def test_load_state_returns_empty_dict_when_corrupt(cfg: SimpleNamespace) -> None:
    """_load_state must return {} on a corrupt (non-JSON) state file."""
    daemon = _make_daemon(cfg)
    state_path = Path(cfg.schedules_repo) / STATE_FILE_NAME
    state_path.write_text("not-valid-json!!!", encoding="utf-8")

    state = daemon._load_state()
    assert state == {}


# ---------------------------------------------------------------------------
# _is_already_scheduled / _mark_scheduled
# ---------------------------------------------------------------------------


def test_is_already_scheduled_false_initially(cfg: SimpleNamespace) -> None:
    """_is_already_scheduled must return False before anything is scheduled."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    assert daemon._is_already_scheduled(obs) is False


def test_mark_scheduled_then_is_already_scheduled(cfg: SimpleNamespace) -> None:
    """After _mark_scheduled, _is_already_scheduled must return True."""
    daemon = _make_daemon(cfg)
    obs = make_obs_window(days_ahead=5)
    daemon._mark_scheduled(obs)
    assert daemon._is_already_scheduled(obs) is True


def test_mark_scheduled_different_obs_is_false(cfg: SimpleNamespace) -> None:
    """A different observation start time must not be marked as scheduled."""
    daemon = _make_daemon(cfg)
    obs_a = make_obs_window(days_ahead=5)
    obs_b = make_obs_window(days_ahead=7)

    daemon._mark_scheduled(obs_a)
    assert daemon._is_already_scheduled(obs_b) is False
