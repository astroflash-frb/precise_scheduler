"""
ObservationCalendar — reads the PRECISE scheduling Google Spreadsheet and
returns the next upcoming observation window.

Spreadsheet layout
------------------
- One tab per month.  The canonical name is ``mmmYY`` (e.g. ``Jul26``), but
  the sheet also contains variants such as ``July26``, ``June26`` or
  ``August2019``; all are matched case-insensitively (see
  :func:`_sheet_name_matches`).  Missing tab = no observations that month.
- Row 0 is the header row: column 0 = project label, a ``Date`` column
  holds the date label, and the telescope codename columns (``Ef``, ``Tr``,
  ``Wb``, …) hold the time windows.  Non-telescope columns such as
  ``UT-range to observe``, ``Comments``, ``data status`` and
  ``sched/comments`` are ignored.
- Each observation occupies one data row:
    - column 0: project code (e.g. ``PR405A``)
    - ``Date`` column: date range string like ``3-4 Aug`` (or a single day,
      e.g. ``11 Aug``)
    - telescope columns: time window ``HHMM-HHMM`` (UTC), or blank / ``-`` /
      ``--`` when that telescope is not participating
- Blank rows (all cells empty or whitespace) separate observations; skip them.

Fetching strategy
-----------------
The sheet list (tab name → gid) is scraped from the public ``htmlview``
page, then each tab is downloaded via the ``export?format=csv&gid=…``
endpoint.  The ``gviz`` CSV endpoint is deliberately avoided: it silently
returns the *first* tab when the requested sheet name does not exist, and
its header auto-detection can merge the first data rows into the header.
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Public htmlview page: lists all tabs with their gids (works for
# "anyone with the link can view" spreadsheets, no credentials needed).
_HTMLVIEW_URL = "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/htmlview"

# Raw CSV export of a single tab by gid.
_CSV_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    "?format=csv&gid={gid}"
)

# Matches the per-tab entries in the htmlview page JavaScript:
#   items.push({name: "Aug26", pageUrl: "...gid=456380787"});
_SHEET_ENTRY_RE = re.compile(r'\{name:\s*"([^"]*)",\s*pageUrl:\s*"[^"]*?gid=(\d+)')

# Matches "HHMM-HHMM", e.g. "1800-0600"
_TIME_WINDOW_RE = re.compile(r"^\d{4}-\d{4}$")

# Header cells (whitespace-normalised, lowercase) that are not telescopes.
_SKIP_HEADERS = {
    "",
    "date",
    "ut-range to observe",
    "comments",
    "data status",
    "sched/comments",
}


class CalendarError(Exception):
    """Raised when the calendar cannot be read or parsed."""


@dataclass
class ObsWindow:
    start: datetime    # UTC-aware
    end: datetime      # UTC-aware
    antennas: list[str]
    project: str


class ObservationCalendar:
    def __init__(self, spreadsheet_id: str) -> None:
        self._spreadsheet_id = spreadsheet_id
        # Cached tab-name → gid mapping; refreshed on every
        # get_next_observation() call.
        self._sheet_index: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_next_observation(self, lookahead_days: int = 10) -> ObsWindow | None:
        """Return the next scheduled :class:`ObsWindow` whose start is in the
        future and within *lookahead_days*.

        Returns ``None`` if no such observation is found.

        Raises
        ------
        CalendarError
            On authentication / network failure or unparseable spreadsheet data.
        """
        upcoming = self.get_upcoming_observations(lookahead_days=lookahead_days)
        if not upcoming:
            return None
        obs = upcoming[0]
        log.info(
            "Next observation: project=%s start=%s antennas=%s",
            obs.project,
            obs.start.isoformat(),
            obs.antennas,
        )
        return obs

    def get_upcoming_observations(self, lookahead_days: int = 10) -> list[ObsWindow]:
        """Return every scheduled :class:`ObsWindow` whose start is in the
        future and within *lookahead_days*, ordered by start time.

        Returns an empty list if no such observation is found.

        Raises
        ------
        CalendarError
            On authentication / network failure or unparseable spreadsheet data.
        """
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(days=lookahead_days)

        # Force a fresh sheet listing for every poll so newly added tabs
        # are picked up by long-running daemons.
        self._sheet_index = None

        windows: list[ObsWindow] = []

        # Iterate every month tab between now and the deadline (inclusive).
        for year, month in _months_between(now, deadline):
            tab_name = _make_tab_name(year, month)
            rows = self._fetch_sheet(tab_name)
            if rows is None:
                log.debug("No tab found for %s, skipping.", tab_name)
                continue

            if not rows:
                log.debug("Worksheet %s is empty.", tab_name)
                continue

            header = rows[0]
            log.debug("Processing tab %s with %d data rows.", tab_name, len(rows) - 1)

            for row in rows[1:]:
                # Skip blank rows
                if not any(cell.strip() for cell in row):
                    continue

                obs = _parse_row(row, header, year, month)
                if obs is None:
                    continue

                if obs.start <= now:
                    log.debug(
                        "Skipping past observation %s starting %s.",
                        obs.project,
                        obs.start.isoformat(),
                    )
                    continue

                if obs.start > deadline:
                    log.debug(
                        "Observation %s at %s is beyond lookahead window.",
                        obs.project,
                        obs.start.isoformat(),
                    )
                    continue

                windows.append(obs)

        if not windows:
            log.info("No upcoming observation found within %d days.", lookahead_days)
        windows.sort(key=lambda w: w.start)
        return windows

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_sheet(self, sheet_name: str) -> list[list[str]] | None:
        """Fetch the tab matching *sheet_name* as a list of rows.

        *sheet_name* is the canonical ``mmmYY`` name; the actual tab is
        located with :func:`_sheet_name_matches` (so ``Jul26`` also finds a
        tab named ``July26``).  Returns ``None`` if no tab matches, or an
        empty list if the tab is blank.  Raises :class:`CalendarError` on
        network or parse failures.
        """
        if self._sheet_index is None:
            self._sheet_index = self._list_sheets()

        gid = next(
            (
                g
                for name, g in self._sheet_index.items()
                if _sheet_name_matches(sheet_name, name)
            ),
            None,
        )
        if gid is None:
            log.debug("No tab matching %r in spreadsheet.", sheet_name)
            return None

        url = _CSV_EXPORT_URL.format(
            spreadsheet_id=self._spreadsheet_id, gid=gid
        )
        log.debug("Fetching sheet %r (gid=%s) from %s", sheet_name, gid, url)
        text = self._http_get(url, f"sheet {sheet_name!r}")
        reader = csv.reader(io.StringIO(text))
        return [row for row in reader]

    def _list_sheets(self) -> dict[str, str]:
        """Return a mapping of tab name → gid scraped from the public
        ``htmlview`` page.

        Raises
        ------
        CalendarError
            On network failure, or when no tabs can be found (spreadsheet
            not public / unexpected page format).
        """
        url = _HTMLVIEW_URL.format(spreadsheet_id=self._spreadsheet_id)
        html = self._http_get(url, "sheet list")
        sheets = {name: gid for name, gid in _SHEET_ENTRY_RE.findall(html)}
        if not sheets:
            raise CalendarError(
                "Could not find any tabs in the spreadsheet htmlview page. "
                "Is the spreadsheet shared as 'Anyone with the link can view'?"
            )
        log.debug("Spreadsheet has %d tabs: %s", len(sheets), sorted(sheets))
        return sheets

    def _http_get(self, url: str, what: str) -> str:
        """GET *url* and return the body as text.

        Raises :class:`CalendarError` on any HTTP or network failure.
        """
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                if resp.status != 200:
                    raise CalendarError(
                        f"HTTP {resp.status} fetching {what} from {url}"
                    )
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            raise CalendarError(f"HTTP error fetching {what}: {exc}") from exc
        except OSError as exc:
            raise CalendarError(f"Network error fetching {what}: {exc}") from exc
        return raw_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Module-level parsing helpers (pure functions, easier to unit-test)
# ---------------------------------------------------------------------------


def _make_tab_name(year: int, month: int) -> str:
    """Return the spreadsheet tab name for *year*/*month*, e.g. ``"Jul26"``."""
    abbr = calendar.month_abbr[month]  # e.g. "Jul"
    return f"{abbr}{str(year)[2:]}"   # e.g. "Jul26"


def _months_between(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Return the ``(year, month)`` pairs from *start*'s month to *end*'s
    month, inclusive."""
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _sheet_name_matches(target: str, actual: str) -> bool:
    """Return ``True`` if the tab named *actual* corresponds to the canonical
    ``mmmYY`` name *target*.

    The spreadsheet tab names are not fully consistent: most are ``mmmYY``
    (``Aug26``), but some months are spelled out (``June26``, ``July26``,
    ``August2019``) and stray whitespace occurs (``" Feb24"``).  A tab
    matches when it starts with the same 3-letter month (optionally
    completed to the full month name) and ends with the same year, in
    2-digit or 4-digit form.
    """
    def _norm(s: str) -> str:
        return "".join(s.split()).lower()

    t = _norm(target)
    a = _norm(actual)
    if a == t:
        return True
    mon, yy = t[:3], t[3:]
    return re.fullmatch(rf"{re.escape(mon)}[a-z]*(?:{yy}|20{yy})", a) is not None


def _normalise_header(cell: str) -> str:
    """Collapse whitespace (including embedded newlines) and lowercase."""
    return " ".join(cell.split()).lower()


def _parse_time_window(cell: str) -> tuple[int, int, int, int] | None:
    """Parse a ``HHMM-HHMM`` cell into ``(h_start, m_start, h_end, m_end)``.

    Returns ``None`` if the cell does not match the expected format.
    """
    cell = cell.strip()
    if not _TIME_WINDOW_RE.match(cell):
        return None
    hhmm_start = int(cell[:4])
    hhmm_end = int(cell[5:])
    return (
        hhmm_start // 100,
        hhmm_start % 100,
        hhmm_end // 100,
        hhmm_end % 100,
    )


def _parse_date_range(date_str: str, year: int, month: int) -> tuple[int, int] | None:
    """Parse a date-range string like ``"14-15 Jul"`` into ``(day1, day2)``.

    Single-day observations (``"11 Aug"``) return ``(day, day)``.

    The month name in the string is used for a sanity check but the *month*
    argument (from the tab name) is authoritative.

    Returns ``None`` if the string cannot be parsed.
    """
    date_str = date_str.strip()
    # Expected: "D1-D2 Mmm" or "D Mmm" with possible extra whitespace
    parts = date_str.split()
    if len(parts) < 2:
        return None
    # parts[0] should be "D1-D2" or "D", parts[1] should be month abbreviation
    day_part = parts[0]
    dash_idx = day_part.find("-")
    try:
        if dash_idx < 1:
            day1 = day2 = int(day_part)
        else:
            day1 = int(day_part[:dash_idx])
            day2 = int(day_part[dash_idx + 1:])
    except ValueError:
        return None
    return day1, day2


def _parse_row(
    row: list[str],
    header: list[str],
    year: int,
    month: int,
) -> ObsWindow | None:
    """Parse a single data row into an :class:`ObsWindow`.

    Returns ``None`` when the row cannot be parsed (logged at DEBUG level).
    """
    if not row:
        return None

    project = row[0].strip() if len(row) > 0 else ""

    # The date lives in the column whose header is "Date" (the current
    # sheets have a "UT-range to observe" column in between); fall back to
    # column 1 when no such header exists.
    date_col = next(
        (i for i, h in enumerate(header) if _normalise_header(h) == "date"), 1
    )
    date_str = row[date_col].strip() if len(row) > date_col else ""

    if not project or not date_str:
        return None

    days = _parse_date_range(date_str, year, month)
    if days is None:
        log.debug("Could not parse date range %r in project %r.", date_str, project)
        return None
    day1, day2 = days

    # Find antenna columns: skip column 0 (project), the date column, and
    # any column whose (normalised) header is in _SKIP_HEADERS.
    antennas: list[str] = []
    first_window: tuple[int, int, int, int] | None = None

    for col_idx in range(1, len(row)):
        if col_idx == date_col:
            continue
        col_header = header[col_idx].strip() if col_idx < len(header) else ""
        if _normalise_header(col_header) in _SKIP_HEADERS:
            continue

        cell = row[col_idx].strip() if col_idx < len(row) else ""
        if not cell or set(cell) == {"-"}:
            continue

        parsed = _parse_time_window(cell)
        if parsed is None:
            continue

        if first_window is None:
            first_window = parsed

        antennas.append(col_header)

    if first_window is None:
        log.debug(
            "No valid telescope time window found in row for project %r.", project
        )
        return None

    h_start, m_start, h_end, m_end = first_window

    # A range like "31-1 Jul" crosses into the following month.
    end_year, end_month = year, month
    if day2 < day1:
        end_month += 1
        if end_month > 12:
            end_month = 1
            end_year += 1

    try:
        start_dt = datetime(year, month, day1, h_start, m_start, tzinfo=timezone.utc)
        end_dt = datetime(
            end_year, end_month, day2, h_end, m_end, tzinfo=timezone.utc
        )
    except ValueError as exc:
        log.debug("Invalid date/time for project %r: %s", project, exc)
        return None

    # Handle midnight crossing: if the end time-of-day is earlier than the
    # start time-of-day, the observation crosses midnight and end is on day2.
    # The end datetime already has the correct day (day2) — but if end_dt
    # ends up before start_dt, we need to add a day.
    if end_dt < start_dt:
        end_dt += timedelta(days=1)

    return ObsWindow(
        start=start_dt,
        end=end_dt,
        antennas=antennas,
        project=project,
    )
