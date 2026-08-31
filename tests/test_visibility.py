"""Tests for precise_scheduler.visibility.

``visibility`` is a thin adapter over planobs, so these tests replace planobs
itself: :func:`tests.fake_planobs.install` patches the single import seam,
:meth:`VisibilityPlanner._import_planobs`, so nothing here needs planobs
installed and no observation is ever really computed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from precise_scheduler.visibility import (
    DEFAULT_PULSARS,
    DEFAULT_SCAN_MINUTES,
    DEFAULT_SETUP,
    SCAN_MINUTES,
    SLOT_MINUTES,
    SRT_ONLY_SETUP,
    VisibilityError,
    VisibilityPlanner,
)

from .fake_planobs import FakeSourceType, scan_block, target_block
from .fake_planobs import install as install_fake_planobs

_TARGETS_TOML = Path("/tmp/fake_targets.toml")


def _coord_source(name: str):
    """A source carrying real sky coordinates, as planobs' picks do."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    from .fake_planobs import FakeSource

    source = FakeSource(name=name, source_type=FakeSourceType.FRINGEFINDER)
    source.coord = SkyCoord(ra=5.0 * u.deg, dec=45.0 * u.deg)
    return source


@pytest.fixture
def planobs(monkeypatch: pytest.MonkeyPatch):
    """Patch the planobs import seam; return a handle to drive the fakes.

    ``blocks=[]`` starts with an empty scan-block catalogue so each test can
    declare exactly which sources planobs knows about.
    """
    return install_fake_planobs(
        monkeypatch, blocks=[], pulsars=["PSR_GOOD", "PSR_POOR"]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_planner() -> VisibilityPlanner:
    return VisibilityPlanner(targets_toml_path=_TARGETS_TOML)


def _make_obs_window(*, duration_minutes: int = 120, antennas=None):
    from precise_scheduler.calendar import ObsWindow

    start = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    return ObsWindow(
        start=start,
        end=start + timedelta(minutes=duration_minutes),
        antennas=["Ef", "Wb", "Jb"] if antennas is None else antennas,
        project="RP036A",
    )


def _make_targets(names: list[str]):
    from precise_scheduler.targets import Calibrator, Coordinates, Target

    coords = Coordinates(ra="00:00:00.000", dec="+00:00:00.000", equinox="J2000")
    return [
        Target(
            name=n,
            canonical_name=f"FRB_{n}",
            priority=1,
            coordinates=coords,
            phasecal=Calibrator(name="J0000+0000", coordinates=coords, flux=1.0),
            checksource=Calibrator(name="J0001+0001", coordinates=coords),
        )
        for n in names
    ]


def _visible(stations: list[str], flags: list[bool]) -> dict[str, list[bool]]:
    """Same visibility pattern for every station."""
    return {s: list(flags) for s in stations}


# ---------------------------------------------------------------------------
# observability / get_visibility_grid
# ---------------------------------------------------------------------------


class TestVisibilityGridShape:
    def test_returns_a_row_per_target(self, planobs) -> None:
        obs = _make_obs_window(duration_minutes=60)  # 60 / 15 = 4 slots
        targets = _make_targets(["R200223_D", "R201124_A"])
        planobs.blocks.update({t.name: object() for t in targets})
        planobs.observation.response = {
            t.name: _visible(["Ef", "Wb", "Jb"], [True] * 4) for t in targets
        }

        grid = _make_planner().get_visibility_grid(targets, obs)

        assert set(grid) == {"R200223_D", "R201124_A"}
        assert all(len(slots) == 4 for slots in grid.values())

    def test_slot_counts_are_antennas_on_source(self, planobs) -> None:
        """Each slot holds how many antennas see the source at that instant."""
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R200223_D"])
        planobs.blocks["R200223_D"] = object()
        planobs.observation.response = {
            "R200223_D": {
                "Ef": [True, True, True, False],
                "Wb": [True, True, False, False],
                "Jb": [True, False, False, False],
            }
        }

        grid = _make_planner().get_visibility_grid(targets, obs)

        assert grid["R200223_D"] == [3, 2, 1, 0]

    def test_calibrators_in_the_response_are_ignored(self, planobs) -> None:
        """planobs reports the phase cal too; only the target is counted."""
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R200223_D"])
        planobs.blocks["R200223_D"] = object()
        planobs.observation.response = {
            "R200223_D": _visible(["Ef", "Wb"], [True] * 4),
            "J0000+0000": _visible(["Ef", "Wb", "Jb"], [True] * 4),
        }

        report = _make_planner().observability(targets, obs)

        assert set(report.grid) == {"R200223_D"}
        assert report.sources[0].n_antennas == 2


class TestObservabilityReport:
    def test_reports_best_simultaneous_count_and_dwell(self, planobs) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {
            "R1_D": {
                "Ef": [True, True, True, True],
                "Wb": [True, True, False, False],
                "Jb": [False, True, False, False],
            }
        }

        source = _make_planner().observability(targets, obs).sources[0]

        assert source.n_antennas == 3          # best simultaneous count
        assert source.slots == [2, 3, 1, 1]
        assert source.slots_at_best == 1       # only one slot reaches 3
        assert source.hours_at_best == pytest.approx(SLOT_MINUTES / 60.0)
        assert sorted(source.antennas) == ["Ef", "Jb", "Wb"]

    def test_never_visible_source_reports_zero(self, planobs) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {
            "R1_D": _visible(["Ef", "Wb", "Jb"], [False] * 4)
        }

        source = _make_planner().observability(targets, obs).sources[0]

        assert source.n_antennas == 0
        assert source.slots_at_best == 0
        assert source.hours_at_best == 0.0
        assert source.antennas == []

    def test_source_absent_from_response_fills_zeros(self, planobs) -> None:
        """A block exists but planobs said nothing about it: not a crash."""
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {}

        source = _make_planner().observability(targets, obs).sources[0]

        assert source.n_antennas == 0
        assert source.slots == [0, 0, 0, 0]

    def test_observable_filters_on_min_antennas(self, planobs) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R_ALL", "R_MOST", "R_FEW"])
        planobs.blocks.update({t.name: object() for t in targets})
        planobs.observation.response = {
            "R_ALL": _visible(["Ef", "Wb", "Jb"], [True] * 4),
            "R_MOST": {
                "Ef": [True] * 4, "Wb": [True] * 4, "Jb": [False] * 4,
            },
            "R_FEW": {
                "Ef": [True] * 4, "Wb": [False] * 4, "Jb": [False] * 4,
            },
        }

        report = _make_planner().observability(targets, obs)

        assert report.n_array == 3
        assert [s.target.name for s in report.observable(3)] == ["R_ALL"]
        assert [s.target.name for s in report.observable(2)] == ["R_ALL", "R_MOST"]

    def test_targets_without_a_scan_block_are_skipped(self, planobs) -> None:
        """planobs cannot schedule what it has no block for; the rest still runs."""
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R_KNOWN", "R_UNKNOWN"])
        planobs.blocks["R_KNOWN"] = object()
        planobs.observation.response = {
            "R_KNOWN": _visible(["Ef", "Wb", "Jb"], [True] * 4)
        }

        report = _make_planner().observability(targets, obs)

        assert [s.target.name for s in report.sources] == ["R_KNOWN"]
        assert set(planobs.observation.calls[0]["scans"]) == {"R_KNOWN"}

    def test_no_known_target_raises(self, planobs) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R_UNKNOWN"])

        with pytest.raises(VisibilityError, match="none of the requested sources"):
            _make_planner().observability(targets, obs)


class TestVisibilityErrors:
    def test_planobs_failure_becomes_visibility_error(self, planobs) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.raises = RuntimeError("planobs exploded")

        with pytest.raises(VisibilityError, match="visibility query failed"):
            _make_planner().observability(targets, obs)

    def test_unreadable_catalogue_becomes_visibility_error(self, planobs) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.catalog_error.append(OSError("no such file"))

        with pytest.raises(VisibilityError, match="could not read"):
            _make_planner().observability(targets, obs)

    def test_missing_planobs_reports_how_to_install_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real adapter turns a missing vlbiplanobs into install advice."""
        # Block the import at the finder level so the already-imported module
        # in sys.modules cannot satisfy it.
        monkeypatch.delitem(sys.modules, "vlbiplanobs", raising=False)
        monkeypatch.delitem(sys.modules, "vlbiplanobs.observation", raising=False)
        monkeypatch.setitem(sys.modules, "vlbiplanobs", None)

        with pytest.raises(VisibilityError, match="pip install vlbiplanobs"):
            VisibilityPlanner._import_planobs()

    def test_unknown_antennas_are_dropped_with_a_warning(
        self, planobs, caplog
    ) -> None:
        obs = _make_obs_window(antennas=["Ef", "Nope", "Wb"])
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {"R1_D": _visible(["Ef", "Wb"], [True] * 8)}

        report = _make_planner().observability(targets, obs)

        assert report.array == ["Ef", "Wb"]
        assert "Nope" in caplog.text

    def test_no_recognised_antenna_raises(self, planobs) -> None:
        obs = _make_obs_window(antennas=["Nope", "Nada"])
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()

        with pytest.raises(VisibilityError, match="recognised none of the antennas"):
            _make_planner().observability(targets, obs)


class TestAntennaResolution:
    @pytest.mark.parametrize("label", ["Ef", "EF", "ef", " Ef ", "Effelsberg"])
    def test_antenna_labels_resolve_case_insensitively(
        self, planobs, label: str
    ) -> None:
        obs = _make_obs_window(antennas=[label])
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {"R1_D": _visible(["Ef"], [True] * 8)}

        report = _make_planner().observability(targets, obs)

        assert report.array == ["Ef"]


class TestSlotCount:
    @pytest.mark.parametrize(
        "minutes, expected_slots",
        [
            (15, 1),
            (16, 1),     # slots are sample instants: the next one is past the end
            (30, 2),
            (60, 4),
            (105, 7),
            (120, 8),
            (600, 40),   # a full 10-hour PRECISE run
        ],
    )
    def test_slot_count_follows_window_length(
        self, planobs, minutes: int, expected_slots: int
    ) -> None:
        obs = _make_obs_window(duration_minutes=minutes)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {
            "R1_D": _visible(["Ef", "Wb", "Jb"], [True] * expected_slots)
        }

        report = _make_planner().observability(targets, obs)

        assert report.n_slots == expected_slots
        assert len(report.sources[0].slots) == expected_slots
        assert len(planobs.observation.calls[0]["times"]) == expected_slots

    def test_window_shorter_than_a_slot_still_gets_one(self, planobs) -> None:
        obs = _make_obs_window(duration_minutes=5)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {"R1_D": _visible(["Ef"], [True])}

        assert _make_planner().observability(targets, obs).n_slots == 1


class TestPlanobsArguments:
    def test_observation_is_built_for_the_requested_window(self, planobs) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = object()
        planobs.observation.response = {"R1_D": _visible(["Ef"], [True] * 4)}

        _make_planner().observability(targets, obs)

        call = planobs.observation.calls[0]
        assert call["band"] == "21cm"
        assert call["stations"].station_codenames == ["Ef", "Wb", "Jb"]
        assert call["datarate"] == 2048
        times = call["times"]
        assert times[0].strftime("%Y-%m-%d %H:%M") == "2026-07-14 18:00"
        # Consecutive samples are one slot apart.
        assert (times[1] - times[0]).to("min").value == pytest.approx(SLOT_MINUTES)

    def test_only_the_requested_sources_are_scheduled(self, planobs) -> None:
        """The rest of targets.toml is parsed but must not be observed."""
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks.update({"R1_D": object(), "R_OTHER": object()})
        planobs.observation.response = {"R1_D": _visible(["Ef"], [True] * 4)}

        _make_planner().observability(targets, obs)

        assert set(planobs.observation.calls[0]["scans"]) == {"R1_D"}


class TestSetupSelection:
    def test_default_setup_for_a_mixed_array(self) -> None:
        assert _make_planner().setup_for(["Ef", "Wb", "Sr"]) == DEFAULT_SETUP

    def test_srt_alone_gets_its_own_setup(self) -> None:
        assert _make_planner().setup_for(["Sr"]) == SRT_ONLY_SETUP
        assert _make_planner().setup_for(["srt", "SR"]) == SRT_ONLY_SETUP

    def test_explicit_setup_wins(self) -> None:
        planner = VisibilityPlanner(_TARGETS_TOML, setup="MY_SETUP")
        assert planner.setup_for(["Sr"]) == "MY_SETUP"

    def test_empty_array_falls_back_to_default(self) -> None:
        assert _make_planner().setup_for([]) == DEFAULT_SETUP


class TestGenerateSchedule:
    def test_writes_the_key_file_planobs_returned(
        self, planobs, tmp_path: Path
    ) -> None:
        """planobs returns the key text and writes nothing; the adapter writes."""
        obs = _make_obs_window(duration_minutes=120)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.scheduler.key_content = "! PR411A schedule\n"

        key_file = _make_planner().generate_schedule(targets, obs, tmp_path)

        assert key_file == tmp_path / "RP036A.key"
        assert key_file.read_text() == "! PR411A schedule\n"

    def test_names_the_file_after_the_project(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")

        key_file = _make_planner().generate_schedule(
            targets, obs, tmp_path, experiment_code="PR411A"
        )

        assert key_file.name == "PR411A.key"
        assert planobs.scheduler.key_calls[0]["experiment_code"] == "PR411A"

    def test_creates_the_output_directory(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        nested = tmp_path / "does" / "not" / "exist"

        assert _make_planner().generate_schedule(targets, obs, nested).exists()

    def test_passes_the_window_duration_and_setup(
        self, planobs, tmp_path: Path
    ) -> None:
        obs = _make_obs_window(duration_minutes=120)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")

        _make_planner().generate_schedule(targets, obs, tmp_path)

        call = planobs.observation.calls[-1]
        assert call["duration"].to("hour").value == pytest.approx(2.0)
        assert planobs.scheduler.key_calls[0]["setup_file"] == DEFAULT_SETUP

    def test_empty_schedule_raises(self, planobs, tmp_path: Path) -> None:
        """A blank key file is a failure, not a schedule."""
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.scheduler.key_content = "   \n"

        with pytest.raises(VisibilityError, match="empty schedule"):
            _make_planner().generate_schedule(targets, obs, tmp_path)

    def test_scheduler_failure_becomes_visibility_error(
        self, planobs, tmp_path: Path
    ) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.scheduler.raises = RuntimeError("sched blew up")

        with pytest.raises(VisibilityError, match="schedule generation failed"):
            _make_planner().generate_schedule(targets, obs, tmp_path)

    def test_no_scan_block_for_any_target_raises(
        self, planobs, tmp_path: Path
    ) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R_UNKNOWN"])

        with pytest.raises(VisibilityError, match="no scan block"):
            _make_planner().generate_schedule(targets, obs, tmp_path)


class TestScanDurations:
    """targets.toml carries no durations; planobs cannot schedule without them."""

    def test_every_scan_gets_a_duration(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scans = planobs.observation.calls[-1]["scans"]["R1_D"].scans
        assert all(s.duration is not None for s in scans)

    def test_durations_follow_the_source_type(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")

        _make_planner().generate_schedule(targets, obs, tmp_path)

        by_type = {
            s.source.type.name: s.duration.to("min").value
            for s in planobs.observation.calls[-1]["scans"]["R1_D"].scans
        }
        assert by_type["TARGET"] == pytest.approx(SCAN_MINUTES["TARGET"])
        assert by_type["PHASECAL"] == pytest.approx(SCAN_MINUTES["PHASECAL"])
        assert by_type["CHECKSOURCE"] == pytest.approx(SCAN_MINUTES["CHECKSOURCE"])

    def test_a_duration_from_the_catalogue_is_kept(
        self, planobs, tmp_path: Path
    ) -> None:
        """Only missing durations are filled in."""
        import astropy.units as u

        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = scan_block(
            ("R1_D", FakeSourceType.TARGET), duration=9.0 * u.min
        )

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scans = planobs.observation.calls[-1]["scans"]["R1_D"].scans
        assert scans[0].duration.to("min").value == pytest.approx(9.0)

    def test_unknown_source_type_gets_the_default(
        self, planobs, tmp_path: Path
    ) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = scan_block(("R1_D", FakeSourceType.UNKNOWN))

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scans = planobs.observation.calls[-1]["scans"]["R1_D"].scans
        assert scans[0].duration.to("min").value == pytest.approx(
            DEFAULT_SCAN_MINUTES
        )


class TestFringeFinders:
    """planobs picks and places these; nothing here selects them.

    ``ObservationScheduler._select_fringefinders`` returns early when the
    observation already carries fringe-finder blocks, so injecting our own
    would silently override planobs' choice.
    """

    def test_no_fringe_finder_blocks_are_injected(
        self, planobs, tmp_path: Path
    ) -> None:
        obs = _make_obs_window(duration_minutes=600)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scheduled = planobs.observation.calls[-1]["scans"]
        assert not [n for n in scheduled if n.startswith("FF_")]
        assert all(
            scan.source.type is not FakeSourceType.FRINGEFINDER
            for block in scheduled.values()
            for scan in block.scans
        )


class TestPulsarCheck:
    def test_the_best_covered_pulsar_is_added(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window(duration_minutes=60)      # 4 slots
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.observation.response = {
            "PSR_GOOD": {"Ef": [True] * 4, "Wb": [True] * 4, "Jb": [True] * 4},
            "PSR_POOR": {"Ef": [True] * 4, "Wb": [True] * 4, "Jb": [False] * 4},
        }

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scheduled = planobs.observation.calls[-1]["scans"]
        assert "PSR_GOOD" in scheduled
        assert "PSR_POOR" not in scheduled

    def test_pulsar_scan_gets_its_duration(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.observation.response = {
            "PSR_GOOD": {"Ef": [True] * 4, "Wb": [True] * 4, "Jb": [True] * 4},
        }

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scan = planobs.observation.calls[-1]["scans"]["PSR_GOOD"].scans[0]
        assert scan.duration.to("min").value == pytest.approx(SCAN_MINUTES["PULSAR"])

    def test_only_one_pulsar_is_added(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.observation.response = {
            n: {"Ef": [True] * 4, "Wb": [True] * 4, "Jb": [True] * 4}
            for n in ("PSR_GOOD", "PSR_POOR")
        }

        _make_planner().generate_schedule(targets, obs, tmp_path)

        scheduled = planobs.observation.calls[-1]["scans"]
        assert sum(n.startswith("PSR_") for n in scheduled) == DEFAULT_PULSARS

    def test_no_pulsar_up_still_writes_a_schedule(
        self, planobs, tmp_path: Path, caplog
    ) -> None:
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.observation.response = {
            n: {"Ef": [False] * 4, "Wb": [False] * 4, "Jb": [False] * 4}
            for n in ("PSR_GOOD", "PSR_POOR")
        }

        assert _make_planner().generate_schedule(targets, obs, tmp_path).exists()
        assert "No pulsar is up" in caplog.text

    def test_catalogue_without_pulsars_still_writes_a_schedule(
        self, monkeypatch, tmp_path: Path, caplog
    ) -> None:
        """targets.toml need not declare any [[pulsar]] at all."""
        install_fake_planobs(monkeypatch, blocks=["R1_D"], pulsars=None)
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])

        assert _make_planner().generate_schedule(targets, obs, tmp_path).exists()
        assert "No pulsars in" in caplog.text


class TestFringeFinderSpec:
    """The spec is planobs' own; we only hand it over untouched."""

    def _generate(self, planobs, tmp_path: Path, spec):
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        VisibilityPlanner(_TARGETS_TOML, fringe_finders=spec).generate_schedule(
            targets, obs, tmp_path
        )
        return planobs.scheduler.calls[-1]["fringefinder_spec"]

    def test_none_leaves_planobs_on_its_default(self, planobs, tmp_path: Path) -> None:
        assert self._generate(planobs, tmp_path, None) is None

    def test_a_count_is_passed_through(self, planobs, tmp_path: Path) -> None:
        assert self._generate(planobs, tmp_path, ["3"]) == ["3"]

    def test_explicit_names_are_passed_through(
        self, planobs, tmp_path: Path
    ) -> None:
        names = ["J0927+3902", "J2253+1608"]
        assert self._generate(planobs, tmp_path, names) == names

    def test_a_named_fringe_finder_planobs_drops_is_reported(
        self, planobs, tmp_path: Path, caplog
    ) -> None:
        """planobs lays out what fits and drops the rest, silently."""
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.scheduler.key_content = "source='R1_D' gap=0:00 dur=4:00 /\n"

        VisibilityPlanner(
            _TARGETS_TOML, fringe_finders=["J0927+3902"]
        ).generate_schedule(targets, obs, tmp_path)

        assert "did not schedule the fringe finder J0927+3902" in caplog.text


class TestPulsarSpec:
    def _pulsars_in(self, planobs) -> list[str]:
        scheduled = planobs.observation.calls[-1]["scans"]
        return [n for n in scheduled if n.startswith("PSR_")]

    def _generate(self, planobs, tmp_path: Path, spec):
        obs = _make_obs_window(duration_minutes=60)
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        # Both are up for the whole array at some point; GOOD for longer.
        planobs.observation.response = {
            "PSR_GOOD": {"Ef": [True] * 4, "Wb": [True] * 4, "Jb": [True] * 4},
            "PSR_POOR": {
                "Ef": [True] * 4,
                "Wb": [True] * 4,
                "Jb": [True, True, False, False],
            },
        }
        VisibilityPlanner(_TARGETS_TOML, pulsars=spec).generate_schedule(
            targets, obs, tmp_path
        )
        return self._pulsars_in(planobs)

    def test_none_picks_the_default_count(self, planobs, tmp_path: Path) -> None:
        assert self._generate(planobs, tmp_path, None) == ["PSR_GOOD"]

    def test_a_count_picks_that_many_best_covered(
        self, planobs, tmp_path: Path
    ) -> None:
        assert sorted(self._generate(planobs, tmp_path, ["2"])) == [
            "PSR_GOOD", "PSR_POOR"
        ]

    def test_zero_means_no_pulsar_check(self, planobs, tmp_path: Path) -> None:
        assert self._generate(planobs, tmp_path, ["0"]) == []

    def test_named_pulsars_are_used_as_given(self, planobs, tmp_path: Path) -> None:
        """Naming one overrides the visibility ranking, even a poorer one."""
        assert self._generate(planobs, tmp_path, ["PSR_POOR"]) == ["PSR_POOR"]

    def test_unknown_pulsar_name_raises(self, planobs, tmp_path: Path) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")

        with pytest.raises(VisibilityError, match="No .*pulsar.* entry"):
            VisibilityPlanner(
                _TARGETS_TOML, pulsars=["PSR_NOPE"]
            ).generate_schedule(targets, obs, tmp_path)

    def test_a_pulsar_planobs_drops_is_reported(
        self, planobs, tmp_path: Path, caplog
    ) -> None:
        obs = _make_obs_window()
        targets = _make_targets(["R1_D"])
        planobs.blocks["R1_D"] = target_block("R1_D")
        planobs.scheduler.key_content = "source='R1_D' gap=0:00 dur=4:00 /\n"

        VisibilityPlanner(_TARGETS_TOML, pulsars=["PSR_GOOD"]).generate_schedule(
            targets, obs, tmp_path
        )

        assert "did not schedule the pulsar PSR_GOOD" in caplog.text


class TestParseSpec:
    @pytest.mark.parametrize(
        "spec, expected",
        [
            (None, (1, [])),
            ([], (1, [])),
            (["  "], (1, [])),
            (["0"], (0, [])),
            (["3"], (3, [])),
            (["B0329+54_D"], (1, ["B0329+54_D"])),
            (["A", "B"], (2, ["A", "B"])),
            (["2", "B"], (2, ["2", "B"])),   # a digit among names is a name
        ],
    )
    def test_spec_forms(self, spec, expected) -> None:
        assert VisibilityPlanner._parse_spec(spec, 1) == expected
