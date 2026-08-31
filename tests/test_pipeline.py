"""Tests for precise_scheduler.pipeline (CLI `schedule` stages)."""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from precise_scheduler.pipeline import (
    TENTATIVE_PROJECT,
    ScheduleInputError,
    parse_epoch_spec,
    resolve_obs_window,
    resolve_targets,
    run_plan,
    run_tentative_schedule,
)
from precise_scheduler.calendar import ObsWindow as _ObsWindow
from precise_scheduler.targets import Calibrator, Coordinates, Target

from .fake_planobs import install as install_fake_planobs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _coords() -> Coordinates:
    return Coordinates(ra="00:33:03.600", dec="+28:49:51.600", equinox="J2000")


def _make_target(name: str, canonical: str, priority: int) -> Target:
    cal = Calibrator(name="J0034+2754", coordinates=_coords(), flux="0.200")
    check = Calibrator(name="J0019+2956", coordinates=_coords(), flux=None)
    return Target(
        name=name,
        canonical_name=canonical,
        priority=priority,
        coordinates=_coords(),
        phasecal=cal,
        checksource=check,
    )


TARGET_P2 = _make_target("R200223_D", "FRB20200223D", 2)
TARGET_P1 = _make_target("R201124_A", "FRB20201124A", 1)
TARGET_P0 = _make_target("R121102_A", "FRB20121102A", 0)
ALL_TARGETS = [TARGET_P2, TARGET_P1, TARGET_P0]


def _names(targets: list[Target]) -> list[str]:
    """Compare targets by internal name — Target equality also covers the
    calibrator coordinates, which these tests do not care about."""
    return [t.name for t in targets]


_TARGET_ENTRY = """\
[[target]]
name = "{name}"
canonical_name = "{canonical}"
priority = {priority}
coordinates.RA = "00:33:03.600"
coordinates.Dec = "+28:49:51.600"
coordinates.equinox = "J2000"
phasecal.name = "J0034+2754"
phasecal.coordinates.RA = "00:34:43.486105"
phasecal.coordinates.Dec = "+27:54:25.72030"
phasecal.coordinates.equinox = "J2000"
phasecal.flux = "0.200"
checksource.name = "J0019+2956"
checksource.coordinates.RA = "00:19:37.790320"
checksource.coordinates.Dec = "+29:56:01.92209"
checksource.coordinates.equinox = "J2000"
"""


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    """Minimal Config-like object backed by a real targets.toml/activity.csv."""
    toml = "".join(
        _TARGET_ENTRY.format(
            name=t.name, canonical=t.canonical_name, priority=t.priority
        )
        for t in ALL_TARGETS
    )
    (tmp_path / "targets.toml").write_text(toml)

    now = datetime.now(timezone.utc)
    (tmp_path / "activity.csv").write_text(
        "canonical_name,utc_datetime,facility\n"
        f"FRB20201124A,{(now - timedelta(days=3)).isoformat()},CHIME\n"
    )

    return SimpleNamespace(
        targets_toml=tmp_path / "targets.toml",
        schedules_repo=tmp_path,
        activity_db=tmp_path / "activity.csv",
        google_spreadsheet_id="sheet-id",
        lookahead_days=10,
        debug=False,
    )


@pytest.fixture
def stub_planobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Replace planobs: every scheduled source is up for the whole window."""
    return install_fake_planobs(
        monkeypatch, all_visible=True, key_dir=tmp_path
    )


# ---------------------------------------------------------------------------
# parse_epoch_spec
# ---------------------------------------------------------------------------


class TestParseEpochSpec:
    def test_parses_integer_duration(self) -> None:
        start, duration = parse_epoch_spec("2026-08-01 18:00 8")
        assert start == datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
        assert duration == timedelta(hours=8)

    def test_parses_fractional_duration(self) -> None:
        _, duration = parse_epoch_spec("2026-08-01 18:00 7.5")
        assert duration == timedelta(hours=7, minutes=30)

    def test_start_is_utc_aware(self) -> None:
        start, _ = parse_epoch_spec("2026-08-01 18:00 8")
        assert start.tzinfo is timezone.utc

    @pytest.mark.parametrize(
        "spec",
        [
            "2026-08-01 18:00",          # missing duration
            "2026-08-01",                 # missing time and duration
            "2026-08-01 18:00 8 extra",   # too many fields
            "",
        ],
    )
    def test_wrong_field_count_raises(self, spec: str) -> None:
        with pytest.raises(ScheduleInputError, match="Expected"):
            parse_epoch_spec(spec)

    def test_invalid_date_raises(self) -> None:
        with pytest.raises(ScheduleInputError, match="epoch start"):
            parse_epoch_spec("2026-13-45 18:00 8")

    def test_non_numeric_duration_raises(self) -> None:
        with pytest.raises(ScheduleInputError, match="duration"):
            parse_epoch_spec("2026-08-01 18:00 eight")

    @pytest.mark.parametrize("duration", ["0", "-3"])
    def test_non_positive_duration_raises(self, duration: str) -> None:
        with pytest.raises(ScheduleInputError, match="positive"):
            parse_epoch_spec(f"2026-08-01 18:00 {duration}")


# ---------------------------------------------------------------------------
# resolve_obs_window
# ---------------------------------------------------------------------------


class TestResolveObsWindow:
    def test_epoch_builds_window_without_calendar(
        self, cfg: SimpleNamespace, mocker
    ) -> None:
        calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")

        window, from_calendar = resolve_obs_window(
            cfg,
            epoch="2026-08-01 18:00 8",
            antennas=["Ef", "Wb"],
            project="RP036A",
        )

        assert from_calendar is False
        assert window.start == datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
        assert window.end == datetime(2026, 8, 2, 2, 0, tzinfo=timezone.utc)
        assert window.antennas == ["Ef", "Wb"]
        assert window.project == "RP036A"
        calendar_cls.assert_not_called()

    def test_epoch_without_project_uses_tentative(self, cfg: SimpleNamespace) -> None:
        window, _ = resolve_obs_window(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"]
        )
        assert window.project == TENTATIVE_PROJECT

    def test_epoch_without_antennas_raises(self, cfg: SimpleNamespace) -> None:
        with pytest.raises(ScheduleInputError, match="--epoch requires --antennas"):
            resolve_obs_window(cfg, epoch="2026-08-01 18:00 8")

    def test_antennas_without_epoch_raises(self, cfg: SimpleNamespace) -> None:
        with pytest.raises(ScheduleInputError, match="--antennas only applies"):
            resolve_obs_window(cfg, antennas=["Ef"])

    def test_project_without_epoch_raises(self, cfg: SimpleNamespace) -> None:
        with pytest.raises(ScheduleInputError, match="--project only applies"):
            resolve_obs_window(cfg, project="RP036A")

    def test_reads_calendar_when_no_epoch(self, cfg: SimpleNamespace, mocker) -> None:
        from precise_scheduler.calendar import ObsWindow

        expected = ObsWindow(
            start=datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc),
            end=datetime(2026, 8, 2, 2, 0, tzinfo=timezone.utc),
            antennas=["Ef", "Wb"],
            project="RP036A",
        )
        calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
        calendar_cls.return_value.get_next_observation.return_value = expected

        window, from_calendar = resolve_obs_window(cfg)

        assert window is expected
        assert from_calendar is True
        calendar_cls.return_value.get_next_observation.assert_called_once_with(
            lookahead_days=cfg.lookahead_days
        )

    def test_empty_calendar_raises_with_epoch_hint(
        self, cfg: SimpleNamespace, mocker
    ) -> None:
        calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
        calendar_cls.return_value.get_next_observation.return_value = None

        with pytest.raises(ScheduleInputError, match="--epoch"):
            resolve_obs_window(cfg)


# ---------------------------------------------------------------------------
# resolve_targets
# ---------------------------------------------------------------------------


class TestResolveTargets:
    def test_resolves_by_internal_name(self) -> None:
        assert resolve_targets(ALL_TARGETS, ["R200223_D"]) == [TARGET_P2]

    def test_resolves_by_canonical_name(self) -> None:
        assert resolve_targets(ALL_TARGETS, ["FRB20200223D"]) == [TARGET_P2]

    def test_resolution_is_case_insensitive(self) -> None:
        assert resolve_targets(ALL_TARGETS, ["frb20200223d"]) == [TARGET_P2]

    def test_preserves_caller_order(self) -> None:
        result = resolve_targets(ALL_TARGETS, ["FRB20201124A", "FRB20200223D"])
        assert result == [TARGET_P1, TARGET_P2]

    def test_deduplicates_mixed_aliases(self) -> None:
        """The same source named twice (once per alias) is scheduled once."""
        result = resolve_targets(ALL_TARGETS, ["R200223_D", "FRB20200223D"])
        assert result == [TARGET_P2]

    def test_priority_zero_can_be_forced(self) -> None:
        assert resolve_targets(ALL_TARGETS, ["FRB20121102A"]) == [TARGET_P0]

    def test_unknown_name_raises_listing_all_unknown(self) -> None:
        with pytest.raises(ScheduleInputError) as exc:
            resolve_targets(ALL_TARGETS, ["NOPE_A", "R200223_D", "NOPE_B"])
        assert "NOPE_A" in str(exc.value)
        assert "NOPE_B" in str(exc.value)

    def test_unknown_name_error_lists_known_targets(self) -> None:
        with pytest.raises(ScheduleInputError, match="FRB20200223D"):
            resolve_targets(ALL_TARGETS, ["NOPE"])


# ---------------------------------------------------------------------------
# run_tentative_schedule
# ---------------------------------------------------------------------------


class TestRunTentativeSchedule:
    def test_uses_selector_when_no_targets_forced(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        result = run_tentative_schedule(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef", "Wb"], project="RP036A"
        )

        assert result.forced is False
        assert result.selected == result.proposed
        # Priority 0 is never selected on its own.
        assert TARGET_P0.name not in _names(result.selected)

    def test_forced_targets_replace_selection(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        result = run_tentative_schedule(
            cfg,
            epoch="2026-08-01 18:00 8",
            antennas=["Ef", "Wb"],
            project="RP036A",
            forced_target_names=["FRB20200223D"],
        )

        assert result.forced is True
        assert _names(result.selected) == ["R200223_D"]
        # The selector's own choice is still reported for comparison.
        assert _names(result.proposed) != _names(result.selected)

    def test_forced_priority_zero_target_is_in_visibility_grid(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        """A forced priority-0 source must still get a visibility row, or the
        proposal would show it as never observable."""
        result = run_tentative_schedule(
            cfg,
            epoch="2026-08-01 18:00 8",
            antennas=["Ef", "Wb"],
            project="RP036A",
            forced_target_names=["FRB20121102A"],
        )

        assert _names(result.selected) == ["R121102_A"]
        assert "R121102_A" in result.visibility_grid
        assert sum(result.visibility_grid["R121102_A"]) > 0

    def test_writes_key_file(self, cfg: SimpleNamespace, stub_planobs) -> None:
        result = run_tentative_schedule(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
        )
        assert result.key_file is not None
        assert result.key_file.exists()

    def test_write_key_false_skips_generation(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        result = run_tentative_schedule(
            cfg,
            epoch="2026-08-01 18:00 8",
            antennas=["Ef"],
            project="RP036A",
            write_key=False,
        )
        assert result.key_file is None
        assert not (Path(cfg.schedules_repo) / "RP036A.key").exists()

    def test_does_not_commit_or_push(
        self, cfg: SimpleNamespace, stub_planobs, mocker
    ) -> None:
        """A tentative run must never touch git."""
        run_mock = mocker.patch("subprocess.run", side_effect=AssertionError("git ran"))

        run_tentative_schedule(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
        )

        run_mock.assert_not_called()

    def test_returns_proposal_blocks(self, cfg: SimpleNamespace, stub_planobs) -> None:
        result = run_tentative_schedule(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
        )
        types = [b["type"] for b in result.proposal_blocks]
        assert "section" in types
        assert "actions" in types

    def test_no_schedulable_sources_raises(
        self, cfg: SimpleNamespace, stub_planobs, mocker
    ) -> None:
        mocker.patch("precise_scheduler.selector.propose", return_value=[])

        with pytest.raises(ScheduleInputError, match="No sources are schedulable"):
            run_tentative_schedule(
                cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
            )


# ---------------------------------------------------------------------------
# run_plan — which pre-approved FRBs the array can actually observe
# ---------------------------------------------------------------------------


def _plan_visibility(planobs, per_source: dict[str, int], array: list[str]) -> None:
    """Make *per_source* map each source to how many of *array* can see it."""
    planobs.observation.all_visible = False
    planobs.observation.response = {
        name: {
            station: [station_index < n_antennas] * 8
            for station_index, station in enumerate(array)
        }
        for name, n_antennas in per_source.items()
    }


class TestRunPlan:
    def test_checks_only_pre_approved_sources(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        """priority 2 always; priority 1 only while recently detected; never 0.

        The fixture's activity.csv has a 3-day-old detection of FRB20201124A,
        so the priority-1 source is active and the priority-0 one is not
        considered at all.
        """
        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef", "Wb"], project="RP036A"
        )

        scheduled = set(stub_planobs.observation.calls[0]["scans"])
        assert scheduled == {TARGET_P2.name, TARGET_P1.name}
        assert TARGET_P0.name not in scheduled
        assert plan.n_candidates == 2

    def test_quiet_priority_one_source_is_not_checked(
        self, cfg: SimpleNamespace, stub_planobs, tmp_path: Path
    ) -> None:
        """With no recent detection, only the priority-2 source is pre-approved."""
        (tmp_path / "activity.csv").write_text(
            "canonical_name,utc_datetime,facility\n"
        )

        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef", "Wb"], project="RP036A"
        )

        assert set(stub_planobs.observation.calls[0]["scans"]) == {TARGET_P2.name}
        assert plan.n_candidates == 1

    def test_keeps_sources_the_whole_array_can_observe(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        array = ["Ef", "Wb", "Tr", "O8"]
        _plan_visibility(
            stub_planobs, {TARGET_P2.name: 4, TARGET_P1.name: 4}, array
        )

        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=array, project="RP036A"
        )

        assert plan.min_antennas == 3        # 4 antennas, all but one allowed
        assert {s.target.name for s in plan.observable} == {
            TARGET_P2.name, TARGET_P1.name
        }
        assert plan.dropped == []

    def test_keeps_sources_all_but_one_antenna_can_observe(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        array = ["Ef", "Wb", "Tr", "O8"]
        _plan_visibility(
            stub_planobs, {TARGET_P2.name: 3, TARGET_P1.name: 4}, array
        )

        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=array, project="RP036A"
        )

        assert {s.target.name for s in plan.observable} == {
            TARGET_P2.name, TARGET_P1.name
        }

    def test_drops_sources_two_antennas_would_miss(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        array = ["Ef", "Wb", "Tr", "O8"]
        _plan_visibility(
            stub_planobs, {TARGET_P2.name: 4, TARGET_P1.name: 2}, array
        )

        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=array, project="RP036A"
        )

        assert [s.target.name for s in plan.observable] == [TARGET_P2.name]
        assert [s.target.name for s in plan.dropped] == [TARGET_P1.name]

    def test_observable_sorted_by_coverage_then_dwell(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        """Best-covered first; among equals, the one up longest comes first."""
        array = ["Ef", "Wb", "Tr"]
        stub_planobs.observation.all_visible = False
        stub_planobs.observation.response = {
            TARGET_P2.name: {s: [True] * 8 for s in array},          # 3/3, 8 slots
            TARGET_P1.name: {                                        # 2/3
                "Ef": [True] * 8, "Wb": [True] * 8, "Tr": [False] * 8,
            },
        }

        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=array, project="RP036A"
        )

        assert [s.target.name for s in plan.observable] == [
            TARGET_P2.name, TARGET_P1.name
        ]
        assert plan.observable[0].n_antennas == 3
        assert plan.observable[0].hours_at_best == pytest.approx(2.0)

    def test_reports_the_array_planobs_resolved(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        plan = run_plan(
            cfg,
            epoch="2026-08-01 18:00 8",
            antennas=["Ef", "Wb", "Tr"],
            project="RP036A",
        )

        assert plan.array == ["Ef", "Wb", "Tr"]
        assert plan.n_array == 3
        assert plan.n_slots == 32          # 8 h at 15-min sampling

    def test_nothing_observable_is_not_an_error(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        """An empty answer is a real result to report, not a failure."""
        array = ["Ef", "Wb", "Tr"]
        _plan_visibility(
            stub_planobs, {TARGET_P2.name: 0, TARGET_P1.name: 0}, array
        )

        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=array, project="RP036A"
        )

        assert plan.observable == []
        assert len(plan.dropped) == 2

    def test_named_targets_bypass_pre_approval(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        """--targets checks exactly what was asked for, priority 0 included."""
        plan = run_plan(
            cfg,
            epoch="2026-08-01 18:00 8",
            antennas=["Ef", "Wb"],
            project="RP036A",
            candidate_names=[TARGET_P0.canonical_name],
        )

        assert set(stub_planobs.observation.calls[0]["scans"]) == {TARGET_P0.name}
        assert plan.forced is True

    def test_pre_approved_run_is_not_marked_forced(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
        )
        assert plan.forced is False

    def test_unknown_named_target_raises(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        with pytest.raises(ScheduleInputError, match="Unknown target"):
            run_plan(
                cfg,
                epoch="2026-08-01 18:00 8",
                antennas=["Ef"],
                project="RP036A",
                candidate_names=["FRB99999999Z"],
            )

    def test_no_pre_approved_source_raises(
        self, cfg: SimpleNamespace, stub_planobs, tmp_path: Path
    ) -> None:
        """Nothing to plan is a configuration problem worth explaining."""
        (tmp_path / "activity.csv").write_text(
            "canonical_name,utc_datetime,facility\n"
        )
        (tmp_path / "targets.toml").write_text(
            _TARGET_ENTRY.format(
                name=TARGET_P0.name, canonical=TARGET_P0.canonical_name, priority=0
            )
        )

        with pytest.raises(ScheduleInputError, match="No FRB is pre-approved"):
            run_plan(
                cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
            )

    def test_window_comes_from_the_calendar_by_default(
        self, cfg: SimpleNamespace, stub_planobs, mocker
    ) -> None:
        window = _ObsWindow(
            start=datetime(2026, 9, 7, 19, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 8, 5, 0, tzinfo=timezone.utc),
            antennas=["Ef", "Wb"],
            project="PR411A",
        )
        calendar_cls = mocker.patch("precise_scheduler.calendar.ObservationCalendar")
        calendar_cls.return_value.get_next_observation.return_value = window

        plan = run_plan(cfg)

        assert plan.from_calendar is True
        assert plan.obs_window.project == "PR411A"
        calendar_cls.return_value.get_next_observation.assert_called_once_with(
            lookahead_days=cfg.lookahead_days
        )

    def test_epoch_window_is_not_from_the_calendar(
        self, cfg: SimpleNamespace, stub_planobs
    ) -> None:
        plan = run_plan(
            cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A"
        )

        assert plan.from_calendar is False
        assert plan.obs_window.project == "RP036A"

    def test_epoch_without_antennas_raises(self, cfg: SimpleNamespace) -> None:
        with pytest.raises(ScheduleInputError, match="requires --antennas"):
            run_plan(cfg, epoch="2026-08-01 18:00 8")

    def test_writes_nothing(
        self, cfg: SimpleNamespace, stub_planobs, tmp_path: Path
    ) -> None:
        """plan is read-only: no .key file, and planobs is never asked for one."""
        before = {p.name for p in tmp_path.iterdir()}

        run_plan(cfg, epoch="2026-08-01 18:00 8", antennas=["Ef"], project="RP036A")

        assert {p.name for p in tmp_path.iterdir()} == before
        assert not list(tmp_path.glob("*.key"))
        assert all(
            "duration" not in call for call in stub_planobs.observation.calls
        )
