"""Tests for the Activity module (precise_scheduler/activity.py)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from precise_scheduler.activity import Activity, Detection, _parse_since


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(days_ago: float = 0, **kwargs) -> datetime:
    """Return a UTC-aware datetime offset from *now* by *days_ago* days.

    Extra keyword arguments are forwarded to :func:`datetime` so callers can
    pin a specific time (e.g. ``hour=12``) if they need stable ISO strings.
    """
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    if kwargs:
        base = base.replace(**kwargs)
    return base


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read the activity CSV and return a list of row dicts."""
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# add_detection
# ---------------------------------------------------------------------------


class TestAddDetection:
    def test_add_creates_file_with_header(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20201124A", _utc(), "CHIME")

        assert db.exists()
        rows = _read_csv(db)
        assert rows[0]["canonical_name"] == "FRB20201124A"
        assert rows[0]["facility"] == "CHIME"
        # Verify the header is present by checking DictReader picked it up.
        assert "utc_datetime" in rows[0]

    def test_add_appends_to_existing(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t1 = _utc(days_ago=2)
        t2 = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t1, "CHIME")
        act.add_detection("FRB20201124A", t2, "CHIME")

        rows = _read_csv(db)
        assert len(rows) == 2
        # First row is the first insertion.
        assert rows[0]["utc_datetime"] == t1.isoformat()
        assert rows[1]["utc_datetime"] == t2.isoformat()

    def test_non_chime_facility_stored(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20220912A", _utc(), "FAST")

        rows = _read_csv(db)
        assert rows[0]["facility"] == "FAST"


# ---------------------------------------------------------------------------
# get_active_sources
# ---------------------------------------------------------------------------


class TestGetActiveSources:
    def test_get_active_sources_within_window(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20201124A", _utc(days_ago=14), "CHIME")

        sources = act.get_active_sources(since_days=15)

        assert sources == ["FRB20201124A"]

    def test_get_active_sources_outside_window(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20201124A", _utc(days_ago=16), "CHIME")

        sources = act.get_active_sources(since_days=15)

        assert sources == []

    def test_get_active_sources_sorted(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20220912A", _utc(days_ago=1), "CHIME")
        act.add_detection("FRB20201124A", _utc(days_ago=1), "CHIME")

        sources = act.get_active_sources(since_days=15)

        assert sources == sorted(sources)

    def test_get_active_sources_deduplicates(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20201124A", _utc(days_ago=1), "CHIME")
        act.add_detection("FRB20201124A", _utc(days_ago=2), "CHIME")

        sources = act.get_active_sources(since_days=15)

        assert sources == ["FRB20201124A"]

    def test_get_active_sources_nonexistent_db(self, tmp_path: Path) -> None:
        db = tmp_path / "nonexistent.csv"
        act = Activity(db)

        assert act.get_active_sources(since_days=15) == []


# ---------------------------------------------------------------------------
# get_detections
# ---------------------------------------------------------------------------


class TestGetDetections:
    def test_get_detections_single_source(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t, "CHIME")
        act.add_detection("FRB20220912A", _utc(days_ago=1), "CHIME")

        results = act.get_detections("FRB20201124A", since_days=15)

        assert len(results) == 1
        assert results[0].canonical_name == "FRB20201124A"

    def test_get_detections_newest_first(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t_old = _utc(days_ago=5)
        t_new = _utc(days_ago=2)
        act.add_detection("FRB20201124A", t_old, "CHIME")
        act.add_detection("FRB20201124A", t_new, "CHIME")

        results = act.get_detections("FRB20201124A", since_days=15)

        assert len(results) == 2
        assert results[0].utc_datetime > results[1].utc_datetime

    def test_get_detections_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)

        results = act.get_detections("FRB20201124A", since_days=15)

        assert results == []

    def test_get_detections_unknown_source(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        act.add_detection("FRB20201124A", _utc(days_ago=1), "CHIME")

        results = act.get_detections("FRB_NONEXISTENT", since_days=15)

        assert results == []

    def test_get_detections_returns_detection_objects(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t, "FAST")

        results = act.get_detections("FRB20201124A", since_days=15)

        assert len(results) == 1
        d = results[0]
        assert isinstance(d, Detection)
        assert d.canonical_name == "FRB20201124A"
        assert d.facility == "FAST"
        assert d.utc_datetime.tzinfo is not None  # UTC-aware


# ---------------------------------------------------------------------------
# delete_detection
# ---------------------------------------------------------------------------


class TestDeleteDetection:
    def test_delete_detection_found(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t, "CHIME")

        result = act.delete_detection("FRB20201124A", t)

        assert result is True
        rows = _read_csv(db)
        assert len(rows) == 0

    def test_delete_detection_not_found(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t, "CHIME")

        wrong_time = _utc(days_ago=5)
        result = act.delete_detection("FRB20201124A", wrong_time)

        assert result is False
        rows = _read_csv(db)
        assert len(rows) == 1  # original row untouched

    def test_delete_detection_removes_only_matched_row(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t1 = _utc(days_ago=2)
        t2 = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t1, "CHIME")
        act.add_detection("FRB20201124A", t2, "CHIME")

        act.delete_detection("FRB20201124A", t1)

        rows = _read_csv(db)
        assert len(rows) == 1
        assert rows[0]["utc_datetime"] == t2.isoformat()

    def test_delete_detection_wrong_source_name(self, tmp_path: Path) -> None:
        db = tmp_path / "activity.csv"
        act = Activity(db)
        t = _utc(days_ago=1)
        act.add_detection("FRB20201124A", t, "CHIME")

        result = act.delete_detection("FRB_OTHER", t)

        assert result is False
        rows = _read_csv(db)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_parse_since_days(self) -> None:
        assert _parse_since("15d") == 15

    def test_parse_since_day_singular(self) -> None:
        assert _parse_since("1day") == 1

    def test_parse_since_days_plural(self) -> None:
        assert _parse_since("7days") == 7

    def test_parse_since_months(self) -> None:
        assert _parse_since("2month") == 60

    def test_parse_since_months_plural(self) -> None:
        assert _parse_since("3months") == 90

    def test_parse_since_years(self) -> None:
        assert _parse_since("1yr") == 365

    def test_parse_since_year_singular(self) -> None:
        assert _parse_since("2year") == 730

    def test_parse_since_years_plural(self) -> None:
        assert _parse_since("1years") == 365

    def test_parse_since_case_insensitive(self) -> None:
        assert _parse_since("10D") == 10
        assert _parse_since("2MONTH") == 60
        assert _parse_since("1YR") == 365

    def test_parse_since_invalid(self) -> None:
        with pytest.raises(ValueError):
            _parse_since("2weeks")

    def test_parse_since_invalid_no_unit(self) -> None:
        with pytest.raises(ValueError):
            _parse_since("15")

    def test_parse_since_invalid_empty(self) -> None:
        with pytest.raises(ValueError):
            _parse_since("")

    def test_parse_since_invalid_text_only(self) -> None:
        with pytest.raises(ValueError):
            _parse_since("days")
