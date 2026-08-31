"""Tests for precise_scheduler.obs_schedules."""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from precise_scheduler.obs_schedules import (
    PARALLEL_THRESHOLD,
    ObsRecord,
    SourceTime,
    get_records,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_VEX = Path(__file__).parent / "fixtures" / "rp036a.vex"


@pytest.fixture()
def vex_repo(tmp_path: Path) -> Path:
    """Copy the sample VEX file into a temporary directory and return it."""
    dest = tmp_path / "rp036a.vex"
    shutil.copy(FIXTURE_VEX, dest)
    return tmp_path


@pytest.fixture()
def records(vex_repo: Path) -> list[ObsRecord]:
    """Parsed records from the single-file repo."""
    return get_records(vex_repo)


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------


class TestGetRecordsBasic:
    def test_get_records_finds_vex_file(self, records: list[ObsRecord]) -> None:
        assert len(records) == 1

    def test_project_name_uppercased(self, records: list[ObsRecord]) -> None:
        assert records[0].project == "RP036A"

    def test_start_is_utc_aware(self, records: list[ObsRecord]) -> None:
        import datetime as _dt
        assert records[0].start.tzinfo is not None

    def test_start_epoch_correct(self, records: list[ObsRecord]) -> None:
        # First scan: 2024y200d10h00m00s  → 2024-07-18 10:00:00 UTC
        # (2024 is a leap year; day 200 = 18 July)
        start = records[0].start
        assert start.year == 2024
        assert start.hour == 10
        assert start.minute == 0
        assert start.second == 0


# ---------------------------------------------------------------------------
# Source time accumulation
# ---------------------------------------------------------------------------


class TestSourceTimes:
    def _get_source(self, records: list[ObsRecord], name: str) -> SourceTime:
        for st in records[0].sources:
            if st.name == name:
                return st
        raise KeyError(f"Source {name!r} not found in record")

    def test_source_time_summed(self, records: list[ObsRecord]) -> None:
        """FRB20201124A: scans of 300 s + 600 s = 900 s total."""
        frb = self._get_source(records, "FRB20201124A")
        assert frb.total_seconds == 900.0

    def test_calibrator_present(self, records: list[ObsRecord]) -> None:
        """J1234+5678 calibrator: single 120 s scan."""
        cal = self._get_source(records, "J1234+5678")
        assert cal.total_seconds == 120.0

    def test_antennas_correct(self, records: list[ObsRecord]) -> None:
        """Both Ef and Wb observed FRB20201124A; list must be sorted."""
        frb = self._get_source(records, "FRB20201124A")
        assert frb.antennas == ["Ef", "Wb"]

    def test_calibrator_antennas_correct(self, records: list[ObsRecord]) -> None:
        cal = self._get_source(records, "J1234+5678")
        assert cal.antennas == ["Ef", "Wb"]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_filter_by_source_found(self, vex_repo: Path) -> None:
        result = get_records(vex_repo, source="FRB20201124A")
        assert len(result) == 1
        assert result[0].project == "RP036A"

    def test_filter_by_source_not_found(self, vex_repo: Path) -> None:
        result = get_records(vex_repo, source="UNKNOWNSRC")
        assert result == []

    def test_filter_by_source_case_insensitive(self, vex_repo: Path) -> None:
        result = get_records(vex_repo, source="frb20201124a")
        assert len(result) == 1

    def test_filter_by_project_found(self, vex_repo: Path) -> None:
        result = get_records(vex_repo, project="RP036A")
        assert len(result) == 1

    def test_filter_by_project_case_insensitive(self, vex_repo: Path) -> None:
        result = get_records(vex_repo, project="rp036a")
        assert len(result) == 1

    def test_filter_by_project_not_found(self, vex_repo: Path) -> None:
        result = get_records(vex_repo, project="WRONG")
        assert result == []

    def test_max_projects_cap(self, tmp_path: Path) -> None:
        """Two VEX files present; max_projects=1 returns only one."""
        shutil.copy(FIXTURE_VEX, tmp_path / "rp036a.vex")
        shutil.copy(FIXTURE_VEX, tmp_path / "rp036b.vex")
        result = get_records(tmp_path, max_projects=1)
        assert len(result) == 1

    def test_max_age_filter_excludes_old(self, vex_repo: Path) -> None:
        """Fixture starts in 2024; a 1-day max_age window should exclude it."""
        result = get_records(vex_repo, max_age=timedelta(days=1))
        assert result == []

    def test_max_age_filter_large_window_includes(self, vex_repo: Path) -> None:
        """A large enough window (several years) should include the 2024 fixture."""
        result = get_records(vex_repo, max_age=timedelta(days=5 * 365))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        result = get_records(tmp_path)
        assert result == []

    def test_malformed_vex_skipped_no_raise(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A corrupt .vex file must not raise; a warning must be logged."""
        corrupt = tmp_path / "corrupt.vex"
        corrupt.write_text("VEX_rev = 1.5;\n$SCHED;\nscan No0001;\n*** GARBAGE ***\n")

        with caplog.at_level(logging.WARNING, logger="precise_scheduler.obs_schedules"):
            result = get_records(tmp_path)

        assert result == []
        assert any(
            "corrupt.vex" in record.message or "corrupt" in record.message.lower()
            for record in caplog.records
        )

    def test_malformed_alongside_valid(self, tmp_path: Path) -> None:
        """A corrupt file next to a valid one: only the valid one is returned."""
        shutil.copy(FIXTURE_VEX, tmp_path / "rp036a.vex")
        corrupt = tmp_path / "bad.vex"
        corrupt.write_text("VEX_rev = 1.5;\n$SCHED;\n*** junk ***\n")

        result = get_records(tmp_path)
        assert len(result) == 1
        assert result[0].project == "RP036A"

    def test_returns_list_type(self, vex_repo: Path) -> None:
        result = get_records(vex_repo)
        assert isinstance(result, list)

    def test_record_is_obs_record(self, records: list[ObsRecord]) -> None:
        assert isinstance(records[0], ObsRecord)

    def test_sources_are_source_time(self, records: list[ObsRecord]) -> None:
        for st in records[0].sources:
            assert isinstance(st, SourceTime)


# ---------------------------------------------------------------------------
# Which files are opened at all
# ---------------------------------------------------------------------------


_INLINE_SCAN_VEX = """\
$SCHED;
scan No0001;
     start=2026y240d18h00m00s; mode=R2.L1024; source=R1_D;
     station=Ef:    0 sec:  300 sec:    0.000 GB:   : &ccw  : 1;
     station=Wb:    0 sec:  300 sec:    0.000 GB:   : &ccw  : 1;
endscan;
$SOURCE;
def R1_D;
     source_name = R1_D;
     ra = 05h31m58.7s; dec =  33d08'52.57"; ref_coord_frame = J2000;
enddef;
"""


class TestFileSelection:
    """Name filters are answered before a file is opened.

    The schedules repository holds thousands of unrelated experiments, most in
    formats this parser was never meant to read.  Filtering after parsing meant
    opening all of them and warning about each one.
    """

    def _repo(self, tmp_path: Path, names: list[str]) -> Path:
        for name in names:
            (tmp_path / f"{name}.vex").write_text("this is not valid VEX at all\n")
        return tmp_path

    def test_unrelated_projects_are_never_parsed(
        self, tmp_path: Path, caplog
    ) -> None:
        repo = self._repo(tmp_path, ["cmag41", "b19l09", "ev022"])

        with caplog.at_level(logging.WARNING):
            assert get_records(repo, project_prefix="PR") == []

        assert "malformed" not in caplog.text

    def test_matching_projects_are_still_reported_when_malformed(
        self, tmp_path: Path, caplog
    ) -> None:
        """Silence is for files we never wanted; a broken PR file still matters."""
        repo = self._repo(tmp_path, ["PR411A"])

        with caplog.at_level(logging.WARNING):
            get_records(repo, project_prefix="PR")

        assert "malformed" in caplog.text

    def test_prefix_is_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "pr411a.vex").write_text(_INLINE_SCAN_VEX)

        assert [r.project for r in get_records(tmp_path, project_prefix="PR")] == [
            "PR411A"
        ]

    def test_exact_project_filter_skips_the_rest(
        self, tmp_path: Path, caplog
    ) -> None:
        (tmp_path / "PR411A.vex").write_text(_INLINE_SCAN_VEX)
        (tmp_path / "PR412A.vex").write_text("not valid VEX\n")

        with caplog.at_level(logging.WARNING):
            records = get_records(tmp_path, project="PR411A")

        assert [r.project for r in records] == ["PR411A"]
        assert "malformed" not in caplog.text

    def test_empty_files_are_skipped_quietly(self, tmp_path: Path, caplog) -> None:
        """A zero-byte placeholder is not a malformed schedule."""
        (tmp_path / "PR998I.vex").write_text("")

        with caplog.at_level(logging.WARNING):
            assert get_records(tmp_path, project_prefix="PR") == []

        assert caplog.text == ""


class TestInlineScanLayout:
    """Older PRECISE schedules put start, mode and source on one line."""

    def _records(self, tmp_path: Path) -> list[ObsRecord]:
        (tmp_path / "PR410A.vex").write_text(_INLINE_SCAN_VEX)
        return get_records(tmp_path, max_age=None)

    def test_inline_scans_are_parsed(self, tmp_path: Path) -> None:
        records = self._records(tmp_path)

        assert len(records) == 1
        assert records[0].project == "PR410A"

    def test_source_is_read_from_a_shared_line(self, tmp_path: Path) -> None:
        assert [st.name for st in self._records(tmp_path)[0].sources] == ["R1_D"]

    def test_start_is_read_from_a_shared_line(self, tmp_path: Path) -> None:
        assert self._records(tmp_path)[0].start.year == 2026

    def test_scan_durations_are_summed(self, tmp_path: Path) -> None:
        assert self._records(tmp_path)[0].sources[0].total_seconds == 300.0

    def test_source_name_is_not_confused_with_source(self, tmp_path: Path) -> None:
        """`source_name =` in a $SOURCE def must not be read as a scan source."""
        assert [st.name for st in self._records(tmp_path)[0].sources] == ["R1_D"]


# ---------------------------------------------------------------------------
# Reading many files
# ---------------------------------------------------------------------------


class TestParallelReading:
    """Parsing is CPU-bound, so many files are read in worker processes."""

    def _repo(self, tmp_path: Path, count: int) -> Path:
        for i in range(count):
            (tmp_path / f"PR{i:03d}A.vex").write_text(
                _INLINE_SCAN_VEX.replace("R1_D", f"R{i:03d}_D")
            )
        return tmp_path

    def test_parallel_and_serial_agree(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, PARALLEL_THRESHOLD + 8)

        parallel = get_records(repo, max_age=None)
        serial = get_records(repo, max_age=None, max_workers=1)

        assert [r.project for r in parallel] == [r.project for r in serial]
        assert [
            [st.name for st in r.sources] for r in parallel
        ] == [[st.name for st in r.sources] for r in serial]

    def test_every_file_is_read(self, tmp_path: Path) -> None:
        count = PARALLEL_THRESHOLD + 8
        repo = self._repo(tmp_path, count)

        assert len(get_records(repo, max_age=None)) == count

    def test_small_sets_are_read_serially(self, tmp_path: Path, mocker) -> None:
        """Starting a pool for a handful of files costs more than it saves."""
        pool = mocker.patch("precise_scheduler.obs_schedules.ProcessPoolExecutor")
        self._repo(tmp_path, 2)

        get_records(tmp_path, max_age=None)

        pool.assert_not_called()

    def test_max_workers_one_forces_serial(self, tmp_path: Path, mocker) -> None:
        pool = mocker.patch("precise_scheduler.obs_schedules.ProcessPoolExecutor")
        self._repo(tmp_path, PARALLEL_THRESHOLD + 8)

        get_records(tmp_path, max_age=None, max_workers=1)

        pool.assert_not_called()

    def test_a_pool_that_cannot_start_falls_back_to_serial(
        self, tmp_path: Path, mocker
    ) -> None:
        """A sandbox without process spawning must still get its report."""
        mocker.patch(
            "precise_scheduler.obs_schedules.ProcessPoolExecutor",
            side_effect=OSError("cannot fork"),
        )
        count = PARALLEL_THRESHOLD + 8
        self._repo(tmp_path, count)

        assert len(get_records(tmp_path, max_age=None)) == count

    def test_a_malformed_file_is_named_in_the_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Failures come back from the workers as data, not opaque pool errors."""
        self._repo(tmp_path, PARALLEL_THRESHOLD + 8)
        (tmp_path / "PR999Z.vex").write_text("this is not VEX\n")

        with caplog.at_level(logging.WARNING):
            get_records(tmp_path, max_age=None)

        assert "PR999Z.vex" in caplog.text

    def test_one_bad_file_does_not_lose_the_others(
        self, tmp_path: Path, caplog
    ) -> None:
        count = PARALLEL_THRESHOLD + 8
        self._repo(tmp_path, count)
        (tmp_path / "PR999Z.vex").write_text("this is not VEX\n")

        with caplog.at_level(logging.WARNING):
            assert len(get_records(tmp_path, max_age=None)) == count
