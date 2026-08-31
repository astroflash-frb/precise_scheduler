"""Tests for precise_scheduler.calendar.

All Google Sheets / gspread calls are mocked via pytest-mock.  No network
access or real credentials are required.
"""

from __future__ import annotations

import calendar as _calendar
from datetime import datetime, timedelta, timezone


import pytest

from precise_scheduler.calendar import (
    CalendarError,
    ObservationCalendar,
    _make_tab_name,
    _parse_date_range,
    _parse_row,
    _parse_time_window,
    _sheet_name_matches,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SPREADSHEET_ID = "spreadsheet-id-xyz"

# A minimal header row used in most tests:
#   col 0 = project, col 1 = date, col 2 = Ef, col 3 = Wb, col 4 = Jb,
#   col 5 = sched/comments
_HEADER = ["", "date", "Ef", "Wb", "Jb", "sched/comments"]

# The real header layout used in the PRECISE spreadsheet (e.g. tab July26):
# an extra "UT-range to observe" column before "Date", telescope columns,
# then Comments / data status / sched-comments columns.
_REAL_HEADER = [
    "", "UT-range \nto observe\n", "Date",
    "Ef", "SRT", "O8", "Tr", "Ir", "Wb", "Ur", "T6", "Sh", "Mc", "Nt",
    "JBO", "Sv", "Bd", "Zc", "Ro",
    "Comments", "data status", "", "sched/comments",
]


def _make_fetch_sheet_side_effect(
    sheet_data: dict[str, list[list[str]]]
):
    """Return a side_effect function for patching ``_fetch_sheet``.

    *sheet_data* maps tab-name → list-of-rows (including the header row).
    Returns ``None`` for any tab not present in *sheet_data*.
    """
    def _side_effect(sheet_name: str) -> list[list[str]] | None:
        return sheet_data.get(sheet_name, None)
    return _side_effect



def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _tab_for_offset(month_offset: int = 0) -> str:
    """Return the tab name for a month offset from today."""
    now = _now_utc()
    month = now.month + month_offset
    year = now.year
    if month > 12:
        month -= 12
        year += 1
    return _make_tab_name(year, month)


def _future_date_str(days_ahead: int = 5) -> tuple[str, str, int, int]:
    """Return (date_range_str, tab_name, year, month) for a date *days_ahead* in the future.

    If the target date is the last day of a month, shift one day earlier to
    avoid month-boundary edge cases in the end-day computation.
    """
    target = _now_utc() + timedelta(days=days_ahead)
    end = target + timedelta(days=1)
    # Avoid month boundary: if end crosses into next month, shift target back
    if end.month != target.month:
        target = target - timedelta(days=1)
        end = end - timedelta(days=1)
    day1 = target.day
    day2 = end.day
    month_abbr = _calendar.month_abbr[target.month]
    date_str = f"{day1}-{day2} {month_abbr}"
    tab_name = _make_tab_name(target.year, target.month)
    return date_str, tab_name, target.year, target.month


# ---------------------------------------------------------------------------
# Unit tests for pure parsing helpers
# ---------------------------------------------------------------------------


class TestMakeTabName:
    def test_july_2026(self) -> None:
        assert _make_tab_name(2026, 7) == "Jul26"

    def test_december_2025(self) -> None:
        assert _make_tab_name(2025, 12) == "Dec25"

    def test_january_2027(self) -> None:
        assert _make_tab_name(2027, 1) == "Jan27"


class TestSheetNameMatches:
    def test_exact(self) -> None:
        assert _sheet_name_matches("Aug26", "Aug26")

    def test_full_month_name(self) -> None:
        assert _sheet_name_matches("Jul26", "July26")
        assert _sheet_name_matches("Jun26", "June26")

    def test_full_month_and_year(self) -> None:
        assert _sheet_name_matches("Aug19", "August2019")

    def test_stray_whitespace(self) -> None:
        assert _sheet_name_matches("Feb24", " Feb24")

    def test_case_insensitive(self) -> None:
        assert _sheet_name_matches("Jul26", "JULY26")

    def test_wrong_year_rejected(self) -> None:
        assert not _sheet_name_matches("Jul26", "July25")
        assert not _sheet_name_matches("Aug26", "August2019")

    def test_wrong_month_rejected(self) -> None:
        assert not _sheet_name_matches("Jul26", "June26")
        assert not _sheet_name_matches("Sep26", "Aug26")


class TestParseTimeWindow:
    def test_simple(self) -> None:
        assert _parse_time_window("1800-0600") == (18, 0, 6, 0)

    def test_same_hour(self) -> None:
        assert _parse_time_window("0900-1200") == (9, 0, 12, 0)

    def test_with_minutes(self) -> None:
        assert _parse_time_window("1830-0030") == (18, 30, 0, 30)

    def test_invalid_returns_none(self) -> None:
        assert _parse_time_window("") is None
        assert _parse_time_window("-") is None
        assert _parse_time_window("n/a") is None
        assert _parse_time_window("18:00-06:00") is None


class TestParseDateRange:
    def test_normal(self) -> None:
        assert _parse_date_range("14-15 Jul", 2026, 7) == (14, 15)

    def test_single_digit_days(self) -> None:
        assert _parse_date_range("3-4 Aug", 2026, 8) == (3, 4)

    def test_single_day(self) -> None:
        assert _parse_date_range("11 Aug", 2026, 8) == (11, 11)

    def test_month_boundary(self) -> None:
        assert _parse_date_range("31-1 Jul", 2026, 7) == (31, 1)

    def test_invalid_returns_none(self) -> None:
        assert _parse_date_range("", 2026, 7) is None
        assert _parse_date_range("Jul", 2026, 7) is None
        assert _parse_date_range("Jul 14", 2026, 7) is None


class TestParseRow:
    def _header(self) -> list[str]:
        return list(_HEADER)

    def test_valid_row(self) -> None:
        row = ["RP036A", "14-15 Jul", "1800-0600", "1800-0600", "-", "Alice"]
        obs = _parse_row(row, self._header(), 2026, 7)
        assert obs is not None
        assert obs.project == "RP036A"
        assert obs.antennas == ["Ef", "Wb"]
        assert obs.start == datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
        assert obs.end == datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)

    def test_midnight_crossing(self) -> None:
        # Start 22:00 day 14, end 04:00 day 15 — end < start in time-of-day
        row = ["RP036A", "14-15 Jul", "2200-0400", "2200-0400", "-", ""]
        obs = _parse_row(row, self._header(), 2026, 7)
        assert obs is not None
        assert obs.start == datetime(2026, 7, 14, 22, 0, tzinfo=timezone.utc)
        assert obs.end == datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)

    def test_no_project_returns_none(self) -> None:
        row = ["", "14-15 Jul", "1800-0600", "1800-0600", "-", ""]
        assert _parse_row(row, self._header(), 2026, 7) is None

    def test_no_date_returns_none(self) -> None:
        row = ["RP036A", "", "1800-0600", "1800-0600", "-", ""]
        assert _parse_row(row, self._header(), 2026, 7) is None

    def test_all_dashes_returns_none(self) -> None:
        row = ["RP036A", "14-15 Jul", "-", "-", "-", "Alice"]
        assert _parse_row(row, self._header(), 2026, 7) is None

    def test_sched_comments_column_excluded(self) -> None:
        # Column 5 is "sched/comments" — the value there must not be counted
        row = ["RP036A", "14-15 Jul", "1800-0600", "-", "-", "some comment"]
        obs = _parse_row(row, self._header(), 2026, 7)
        assert obs is not None
        assert "sched/comments" not in obs.antennas
        assert obs.antennas == ["Ef"]


class TestParseRowRealLayout:
    """Rows in the layout actually used by the PRECISE spreadsheet
    (see _REAL_HEADER): date in the 'Date' column (index 2), telescope
    windows from column 3, trailing Comments / data status columns."""

    def _real_row(self, project: str, date: str, comment: str = "") -> list[str]:
        # Mirrors e.g. PR405A in tab Aug26: Ef Tr Ir Wb Ur Mc JBO observe,
        # O8 and T6 marked "--", SRT/Sh/Nt/Sv/Bd/Zc/Ro blank.
        return [
            project, "", date,
            "1800-0400", "", "--", "1800-0400", "1800-0400", "1800-0400",
            "1800-0400", "--", "", "1800-0400", "",
            "1800-0400", "", "", "", "",
            comment, "", "", "Dante",
        ]

    def test_real_row(self) -> None:
        obs = _parse_row(self._real_row("PR405A", "3-4 Aug"), list(_REAL_HEADER), 2026, 8)
        assert obs is not None
        assert obs.project == "PR405A"
        assert obs.antennas == ["Ef", "Tr", "Ir", "Wb", "Ur", "Mc", "JBO"]
        assert obs.start == datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
        assert obs.end == datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)

    def test_single_day_observation(self) -> None:
        # PR407A in Aug26 runs 11 Aug 1800-2300 (no second day).
        row = self._real_row("PR407A", "11 Aug")
        row = [c.replace("1800-0400", "1800-2300") for c in row]
        obs = _parse_row(row, list(_REAL_HEADER), 2026, 8)
        assert obs is not None
        assert obs.start == datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)
        assert obs.end == datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc)

    def test_single_day_crossing_midnight(self) -> None:
        obs = _parse_row(self._real_row("PR407A", "11 Aug"), list(_REAL_HEADER), 2026, 8)
        assert obs is not None
        assert obs.start == datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)
        assert obs.end == datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)

    def test_month_boundary_range(self) -> None:
        obs = _parse_row(self._real_row("PR410A", "31-1 Aug"), list(_REAL_HEADER), 2026, 8)
        assert obs is not None
        assert obs.start == datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)
        assert obs.end == datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)

    def test_comment_with_time_window_not_an_antenna(self) -> None:
        # A "Comments" cell that happens to contain a HHMM-HHMM string must
        # not be picked up as a telescope.
        obs = _parse_row(
            self._real_row("PR405A", "3-4 Aug", comment="1900-0300"),
            list(_REAL_HEADER), 2026, 8,
        )
        assert obs is not None
        assert "Comments" not in obs.antennas
        assert obs.antennas == ["Ef", "Tr", "Ir", "Wb", "Ur", "Mc", "JBO"]


# ---------------------------------------------------------------------------
# Integration-style tests with mocked gspread
# ---------------------------------------------------------------------------


def _make_calendar() -> ObservationCalendar:
    return ObservationCalendar(_FAKE_SPREADSHEET_ID)


class TestGetNextObservation:
    def test_next_observation_found(self, mocker: "pytest_mock.MockerFixture") -> None:
        """A future observation in the current-month tab is returned correctly."""
        date_str, tab_name, year, month = _future_date_str(days_ahead=5)
        # Build day1 from what _future_date_str produced
        target = _now_utc() + timedelta(days=5)
        day1 = target.day
        day2 = (target + timedelta(days=1)).day

        rows = [
            _HEADER,
            ["RP036A", date_str, "1800-0600", "1800-0600", "-", "Alice"],
        ]
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({tab_name: rows}))

        obs = cal.get_next_observation(lookahead_days=10)

        assert obs is not None
        assert obs.project == "RP036A"
        assert "Ef" in obs.antennas
        assert "Wb" in obs.antennas
        assert "Jb" not in obs.antennas
        assert obs.start.tzinfo == timezone.utc
        assert obs.end.tzinfo == timezone.utc
        assert obs.start.year == year
        assert obs.start.month == month
        assert obs.start.day == day1
        assert obs.end.day == day2
        assert obs.start < obs.end

    def test_no_observation_within_lookahead(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """All rows are in the past → returns None.

        We place the observation 5 days ago but use day 2 of the month as
        the anchor so we stay safely within the current month and avoid
        month-boundary edge cases.
        """
        now = _now_utc()
        # Pick a date that is definitely in the past and in the current month.
        # Use day 2 if we're past day 7, otherwise fall back to day 1.
        if now.day > 7:
            past_day = now.day - 5
        else:
            past_day = 1
        past_day2 = past_day + 1
        month_abbr = _calendar.month_abbr[now.month]
        date_str = f"{past_day}-{past_day2} {month_abbr}"
        tab_name = _make_tab_name(now.year, now.month)

        # Use a start hour that is clearly in the past for today
        rows = [
            _HEADER,
            ["RP036A", date_str, "0000-0600", "0000-0600", "-", "Alice"],
        ]
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({tab_name: rows}))

        assert cal.get_next_observation(lookahead_days=10) is None

    def test_tab_missing_for_current_month(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """No tab for the current month, but a future observation in next month is found."""
        # Build an observation 35 days from now so it falls in the next month tab
        future = _now_utc() + timedelta(days=35)
        future_end = future + timedelta(days=1)
        month_abbr = _calendar.month_abbr[future.month]
        date_str = f"{future.day}-{future_end.day} {month_abbr}"
        next_tab = _make_tab_name(future.year, future.month)

        rows = [
            _HEADER,
            ["RP037A", date_str, "1200-2000", "-", "1200-2000", "Bob"],
        ]
        # Only provide the next-month tab (current month is absent)
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({next_tab: rows}))

        obs = cal.get_next_observation(lookahead_days=40)

        assert obs is not None
        assert obs.project == "RP037A"
        assert "Ef" in obs.antennas
        assert "Jb" in obs.antennas
        assert "Wb" not in obs.antennas

    def test_blank_row_skipped(self, mocker: "pytest_mock.MockerFixture") -> None:
        """Blank rows between observations do not cause errors."""
        date_str, tab_name, _year, _month = _future_date_str(days_ahead=5)
        rows = [
            _HEADER,
            ["", "", "", "", "", ""],          # blank separator
            ["", "", "", "", "", ""],          # another blank separator
            ["RP036A", date_str, "1800-0600", "-", "-", "Alice"],
        ]
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({tab_name: rows}))

        obs = cal.get_next_observation(lookahead_days=10)

        assert obs is not None
        assert obs.project == "RP036A"

    def test_telescope_with_dash_excluded(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """A ``-`` cell means that telescope is not participating; exclude it."""
        date_str, tab_name, _year, _month = _future_date_str(days_ahead=5)
        # Ef = participating, Wb = dash (excluded), Jb = participating
        rows = [
            _HEADER,
            ["RP036A", date_str, "1800-0600", "-", "1800-0600", "Alice"],
        ]
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({tab_name: rows}))

        obs = cal.get_next_observation(lookahead_days=10)

        assert obs is not None
        assert "Ef" in obs.antennas
        assert "Wb" not in obs.antennas
        assert "Jb" in obs.antennas

    def test_calendar_error_on_network_failure(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """A network OSError during sheet fetch is wrapped in CalendarError."""
        cal = _make_calendar()
        mocker.patch.object(
            cal,
            "_fetch_sheet",
            side_effect=CalendarError("Network error fetching sheet: connection refused"),
        )

        with pytest.raises(CalendarError, match="Network error"):
            cal.get_next_observation()

    def test_all_tabs_absent(self, mocker: "pytest_mock.MockerFixture") -> None:
        """When the spreadsheet has no tabs at all, returns None."""
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({}))

        assert cal.get_next_observation(lookahead_days=10) is None

    def test_observation_beyond_lookahead_ignored(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """An observation that starts after the lookahead window is not returned."""
        date_str, tab_name, _year, _month = _future_date_str(days_ahead=15)
        rows = [
            _HEADER,
            ["RP036A", date_str, "1800-0600", "1800-0600", "-", "Alice"],
        ]
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({tab_name: rows}))

        # Only look 10 days ahead, but observation is 15 days away
        assert cal.get_next_observation(lookahead_days=10) is None

    def test_multiple_observations_returns_first(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """When multiple future observations exist, the earliest one is returned."""
        date_str_near, tab_name, year, month = _future_date_str(days_ahead=3)
        date_str_far, _tab2, _y2, _m2 = _future_date_str(days_ahead=7)

        rows = [
            _HEADER,
            ["RP036A", date_str_near, "1800-0600", "-", "-", "Alice"],
            ["RP037A", date_str_far, "0900-1800", "-", "-", "Bob"],
        ]
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({tab_name: rows}))

        obs = cal.get_next_observation(lookahead_days=10)

        assert obs is not None
        assert obs.project == "RP036A"


class TestGetUpcomingObservations:
    def test_returns_all_within_lookahead_sorted(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """All future observations within the window are returned in start order,
        even when the sheet rows are not chronological."""
        date_str_near, tab_near, _year, _month = _future_date_str(days_ahead=3)
        date_str_far, tab_far, _y2, _m2 = _future_date_str(days_ahead=7)

        near_row = ["RP036A", date_str_near, "1800-0600", "-", "-", "Alice"]
        far_row = ["RP037A", date_str_far, "0900-1800", "-", "-", "Bob"]
        blank = ["", "", "", "", "", ""]

        # The two dates straddle a month boundary for part of every month, and
        # each row only ever appears in its own month's tab.  Build the sheets
        # accordingly, with the later row first so the sort is really exercised.
        if tab_near == tab_far:
            sheets = {tab_near: [_HEADER, far_row, blank, near_row]}
        else:
            sheets = {
                tab_near: [_HEADER, blank, near_row],
                tab_far: [_HEADER, far_row],
            }

        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect(sheets))

        windows = cal.get_upcoming_observations(lookahead_days=10)

        assert [w.project for w in windows] == ["RP036A", "RP037A"]
        assert windows[0].start < windows[1].start

    def test_excludes_past_and_beyond_lookahead(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """Only observations inside (now, now + lookahead] are returned."""
        now = _now_utc()
        past_day = now.day - 5 if now.day > 7 else 1
        month_abbr = _calendar.month_abbr[now.month]
        past_str = f"{past_day}-{past_day + 1} {month_abbr}"
        date_str_in, tab_name, _year, _month = _future_date_str(days_ahead=5)
        date_str_out, tab_out, _y2, _m2 = _future_date_str(days_ahead=15)

        sheet_data = {
            tab_name: [
                _HEADER,
                ["RP035A", past_str, "0000-0600", "-", "-", ""],
                ["RP036A", date_str_in, "1800-0600", "-", "-", ""],
            ],
        }
        sheet_data.setdefault(tab_out, [_HEADER]).append(
            ["RP037A", date_str_out, "0900-1800", "-", "-", ""]
        )
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect(sheet_data))

        windows = cal.get_upcoming_observations(lookahead_days=10)

        assert [w.project for w in windows] == ["RP036A"]

    def test_spans_multiple_month_tabs(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """Observations from the current and next month tabs are combined."""
        date_str_near, tab_near, _year, _month = _future_date_str(days_ahead=5)
        future = _now_utc() + timedelta(days=35)
        future_end = future + timedelta(days=1)
        month_abbr = _calendar.month_abbr[future.month]
        date_str_next = f"{future.day}-{future_end.day} {month_abbr}"
        tab_next = _make_tab_name(future.year, future.month)

        sheet_data = {
            tab_near: [
                _HEADER,
                ["RP036A", date_str_near, "1800-0600", "-", "-", ""],
            ],
        }
        sheet_data.setdefault(tab_next, [_HEADER]).append(
            ["RP037A", date_str_next, "1200-2000", "-", "1200-2000", ""]
        )
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect(sheet_data))

        windows = cal.get_upcoming_observations(lookahead_days=40)

        assert [w.project for w in windows] == ["RP036A", "RP037A"]

    def test_empty_calendar_returns_empty_list(
        self, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        cal = _make_calendar()
        mocker.patch.object(cal, "_fetch_sheet",
                            side_effect=_make_fetch_sheet_side_effect({}))

        assert cal.get_upcoming_observations(lookahead_days=10) == []
