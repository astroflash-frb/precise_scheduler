"""
Activity database — reads/writes for the FRB detection activity CSV.

The CSV has a fixed header: canonical_name,utc_datetime,facility
All datetimes are stored as UTC-aware ISO 8601 strings and are always
returned as UTC-aware :class:`datetime` objects.
"""

from __future__ import annotations

import csv
import fcntl
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CSV_HEADER = ["canonical_name", "utc_datetime", "facility"]

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    """A single FRB detection record."""

    canonical_name: str
    utc_datetime: datetime  # always UTC-aware
    facility: str


# ---------------------------------------------------------------------------
# Activity class
# ---------------------------------------------------------------------------


class Activity:
    """Read/write interface for the FRB detection activity CSV database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_detection(
        self,
        canonical_name: str,
        utc_datetime: datetime,
        facility: str = "CHIME",
    ) -> None:
        """Append a detection row.

        Creates the file with a header row if it does not yet exist.
        The write is atomic (temp file + :func:`os.replace`) and protected
        by a file-level exclusive lock to prevent concurrent corruption.
        """
        _require_utc(utc_datetime)
        new_row = {
            "canonical_name": canonical_name,
            "utc_datetime": utc_datetime.isoformat(),
            "facility": facility,
        }

        if self._db_path.exists():
            existing_rows = self._read_rows()
            all_rows = existing_rows + [new_row]
        else:
            all_rows = [new_row]

        self._write_rows_atomic(all_rows)

    def get_active_sources(self, since_days: int) -> list[str]:
        """Return a sorted list of canonical names with at least one detection
        within the last *since_days* days."""
        cutoff = _cutoff(since_days)
        names: set[str] = set()
        for row in self._read_rows():
            dt = _parse_dt(row["utc_datetime"])
            if dt >= cutoff:
                names.add(row["canonical_name"])
        return sorted(names)

    def get_detections(self, canonical_name: str, since_days: int) -> list[Detection]:
        """Return detections for *canonical_name* within the last *since_days*
        days, ordered newest first."""
        cutoff = _cutoff(since_days)
        results: list[Detection] = []
        for row in self._read_rows():
            if row["canonical_name"] != canonical_name:
                continue
            dt = _parse_dt(row["utc_datetime"])
            if dt >= cutoff:
                results.append(Detection(
                    canonical_name=row["canonical_name"],
                    utc_datetime=dt,
                    facility=row["facility"],
                ))
        results.sort(key=lambda d: d.utc_datetime, reverse=True)
        return results

    def delete_detection(self, canonical_name: str, utc_datetime: datetime) -> bool:
        """Remove the row that exactly matches *canonical_name* and *utc_datetime*.

        Returns
        -------
        bool
            ``True`` if a row was found and removed, ``False`` otherwise.
        """
        _require_utc(utc_datetime)
        target_iso = utc_datetime.isoformat()
        rows = self._read_rows()
        kept: list[dict[str, str]] = []
        deleted = False
        for row in rows:
            if (
                not deleted
                and row["canonical_name"] == canonical_name
                and row["utc_datetime"] == target_iso
            ):
                deleted = True
            else:
                kept.append(row)

        if deleted:
            self._write_rows_atomic(kept)
        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_rows(self) -> list[dict[str, str]]:
        """Return all rows from the CSV as a list of dicts.  Returns an empty
        list when the file does not exist."""
        if not self._db_path.exists():
            return []
        with self._db_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return list(reader)

    def _write_rows_atomic(self, rows: list[dict[str, str]]) -> None:
        """Write *rows* to the database file atomically.

        Steps:
          1. Open a NamedTemporaryFile in the same directory as the target.
          2. Acquire an exclusive lock on that temp file.
          3. Write header + rows.
          4. Flush/fsync.
          5. Replace the target file with the temp file via :func:`os.replace`.
        """
        parent = self._db_path.parent
        parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=parent, prefix=".activity_tmp_", suffix=".csv"
        )
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                # Acquire exclusive lock for the duration of the write.
                fcntl.flock(fh, fcntl.LOCK_EX)
                writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
                writer.writeheader()
                writer.writerows(rows)
                fh.flush()
                os.fsync(fh.fileno())
                # Lock is released when fh closes at end of with block.
            os.replace(tmp_path, self._db_path)
        except Exception:
            # Clean up the temp file if anything goes wrong.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Module-level helper used by the CLI
# ---------------------------------------------------------------------------

_SINCE_RE = re.compile(
    r"^\s*(?P<value>\d+)\s*(?P<unit>d|day|days|month|months|yr|year|years)\s*$",
    re.IGNORECASE,
)

_UNIT_TO_DAYS: dict[str, int] = {
    "d": 1,
    "day": 1,
    "days": 1,
    "month": 30,
    "months": 30,
    "yr": 365,
    "year": 365,
    "years": 365,
}


def _parse_since(since_str: str) -> int:
    """Parse a human-readable look-back window string into days.

    Accepted formats
    ----------------
    - ``'15d'``, ``'15day'``, ``'15days'``  → 15
    - ``'2month'``, ``'2months'``            → 60
    - ``'1yr'``, ``'1year'``, ``'1years'``  → 365

    The numeric prefix and suffix are both case-insensitive and optional
    whitespace is ignored.

    Raises
    ------
    ValueError
        If *since_str* does not match any recognised pattern.
    """
    m = _SINCE_RE.match(since_str)
    if m is None:
        raise ValueError(
            f"Unrecognised time window: {since_str!r}. "
            "Expected a number followed by d/day/days, month/months, or yr/year/years "
            "(e.g. '15d', '2month', '1yr')."
        )
    value = int(m.group("value"))
    unit = m.group("unit").lower()
    return value * _UNIT_TO_DAYS[unit]


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------


def _cutoff(since_days: int) -> datetime:
    """Return the UTC cutoff datetime for a *since_days* look-back window."""
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def _parse_dt(iso_str: str) -> datetime:
    """Parse an ISO 8601 string and ensure the result is UTC-aware."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        # Treat naive datetimes stored in the CSV as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _require_utc(dt: datetime) -> None:
    """Raise :exc:`ValueError` if *dt* is not timezone-aware."""
    if dt.tzinfo is None:
        raise ValueError(
            f"utc_datetime must be timezone-aware; got naive datetime {dt!r}."
        )
