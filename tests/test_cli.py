"""Tests for precise_scheduler.cli helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from precise_scheduler.cli import _format_obs_dates


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestFormatObsDates:
    def test_two_day_range(self) -> None:
        assert _format_obs_dates(_dt(2026, 8, 3, 18, 0), _dt(2026, 8, 4, 4, 0)) == "3-4 Aug 2026"

    def test_single_day(self) -> None:
        assert _format_obs_dates(_dt(2026, 8, 11, 18, 0), _dt(2026, 8, 11, 23, 0)) == "11 Aug 2026"

    def test_month_boundary(self) -> None:
        assert _format_obs_dates(_dt(2026, 8, 31, 18, 0), _dt(2026, 9, 1, 4, 0)) == "31 Aug - 1 Sep 2026"

    def test_year_boundary(self) -> None:
        assert _format_obs_dates(_dt(2026, 12, 31, 18, 0), _dt(2027, 1, 1, 4, 0)) == "31 Dec 2026 - 1 Jan 2027"
