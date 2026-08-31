"""
Observation schedule parser for PRECISE Scheduler.

Reads VEX files from the PRECISE schedules repository and returns structured
records describing past observing runs.  The reading itself is
:mod:`precise_scheduler.vex`'s job — it parses a VEX file into a document
rather than grepping it, which is the only way to cope with the layouts the
repository actually contains.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SourceTime:
    name: str              # source name as the VEX file records it
    total_seconds: float   # sum of scan durations across all scans
    antennas: list[str]    # unique antennas that observed this source (sorted)


@dataclass
class ObsRecord:
    project: str           # project code, e.g. "PR410A"
    start: datetime        # UTC-aware start of first scan
    sources: list[SourceTime]


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------


def _build_record(vex_path: Path) -> ObsRecord:
    """Parse a single VEX file and return an :class:`ObsRecord`.

    Parsing is :mod:`precise_scheduler.vex`'s job, which reads a VEX file as
    the structured document it is.  This function only reshapes the result
    into the per-source totals the summary needs.

    Raises ``ValueError`` or ``OSError`` on any problem.
    """
    from .vex import load_experiment_info

    info = load_experiment_info(vex_path)
    if not info.scans:
        raise ValueError("No scans found in VEX file")

    src_seconds: dict[str, float] = {}
    src_antennas: dict[str, set[str]] = {}
    earliest: datetime | None = None

    for scan in info.scans:
        if scan.start_time is None:
            continue
        if earliest is None or scan.start_time < earliest:
            earliest = scan.start_time

        name = scan.source
        if scan.stop_time is not None:
            duration = (scan.stop_time - scan.start_time).total_seconds()
            src_seconds[name] = src_seconds.get(name, 0.0) + duration
        else:
            src_seconds.setdefault(name, 0.0)
        src_antennas.setdefault(name, set()).update(scan.stations)

    if earliest is None:
        raise ValueError("No scan carries a start time")

    return ObsRecord(
        project=info.project_code or vex_path.stem.upper(),
        # VEX times are UTC; vex.py returns them naive, and every other time
        # in this tool is aware.
        start=earliest.replace(tzinfo=timezone.utc),
        sources=[
            SourceTime(
                name=name,
                total_seconds=seconds,
                antennas=sorted(src_antennas.get(name, ())),
            )
            for name, seconds in src_seconds.items()
        ],
    )


# ---------------------------------------------------------------------------
# Reading many files
# ---------------------------------------------------------------------------

#: Below this many files, a process pool costs more to start than it saves.
PARALLEL_THRESHOLD = 32

#: Files handed to a worker at a time.  VEX files parse in a couple of
#: milliseconds, so sending them one by one would be mostly IPC.
PARALLEL_CHUNKSIZE = 24


def _read_one(vex_path: Path) -> tuple[Path, ObsRecord | None, str | None]:
    """Parse one file, returning the failure rather than raising it.

    Runs in a worker process, where an exception would surface as an opaque
    pool error and lose which file caused it — so problems come back as data
    and the parent does the logging.
    """
    try:
        return vex_path, _build_record(vex_path), None
    except Exception as exc:  # noqa: BLE001
        return vex_path, None, f"{exc}"


def _read_all(
    vex_files: list[Path], max_workers: int | None = None
) -> list[ObsRecord]:
    """Parse *vex_files*, in parallel when there are enough to be worth it.

    Parsing is CPU-bound Python, so threads are no help — measured against the
    real repository they are exactly as slow as running serially, while
    processes are several times faster.  A pool that cannot start (a sandbox
    with no fork, say) falls back to serial rather than failing the command.
    """
    if len(vex_files) < PARALLEL_THRESHOLD or max_workers == 1:
        results = [_read_one(path) for path in vex_files]
    else:
        try:
            with ProcessPoolExecutor(max_workers=max_workers) as pool:
                results = list(
                    pool.map(_read_one, vex_files, chunksize=PARALLEL_CHUNKSIZE)
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("Parallel VEX reading unavailable (%s); reading serially.", exc)
            results = [_read_one(path) for path in vex_files]

    records: list[ObsRecord] = []
    for path, record, error in results:
        if record is None:
            log.warning("Skipping malformed VEX file %s: %s", path, error)
        else:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: PRECISE's own observations all carry a project code starting with this.
#: The schedules repository also holds unrelated experiments (EV026B, cmag41,
#: eb081a, ...), which are none of this tool's business.
PRECISE_PROJECT_PREFIX = "PR"


def _has_content(path: Path) -> bool:
    """Is there anything in this file to parse?

    The repository holds zero-byte placeholders; an empty file is not a
    malformed one and is no reason to warn.
    """
    try:
        if path.stat().st_size > 0:
            return True
    except OSError:
        return True   # let the parse attempt report the real problem
    log.debug("Skipping empty VEX file %s.", path)
    return False


def _wanted_file(
    path: Path, *, project: str | None, project_prefix: str | None
) -> bool:
    """Is this VEX file worth opening at all?

    :class:`ObsRecord` takes its project code from the filename stem, so the
    caller's name filters can be answered without reading the file.
    """
    stem = path.stem.upper()
    if project is not None and stem != project.upper():
        return False
    if project_prefix is not None and not stem.startswith(project_prefix.upper()):
        return False
    return True


def get_records(
    repo_path: Path,
    project: str | None = None,
    source: str | None = None,
    max_projects: int | None = None,
    max_age: timedelta | None = None,
    project_prefix: str | None = None,
    keep_sources: set[str] | None = None,
    max_workers: int | None = None,
) -> list[ObsRecord]:
    """Parse all ``*.vex`` files under *repo_path* and return a list of
    :class:`ObsRecord` objects, newest first.

    Parameters
    ----------
    repo_path:
        Root directory to search for ``*.vex`` files (recursive).
    project:
        If given, return only the record whose project name matches
        (case-insensitive).
    source:
        If given, keep only records that contain a
        :class:`SourceTime` whose ``name`` matches (case-insensitive).
    max_projects:
        Cap the result list to at most *N* most-recent records (applied
        after all other filters).
    max_age:
        Exclude records whose ``start`` is older than ``now(UTC) - max_age``.
    project_prefix:
        Keep only projects whose code starts with this, case-insensitively —
        pass :data:`PRECISE_PROJECT_PREFIX` to ignore the unrelated
        experiments that share the schedules repository.
    keep_sources:
        Keep only these source names within each record, dropping the rest.
        Pass the FRB target names so calibrators, check sources and pulsars do
        not clutter a summary of what was observed.  A record left with no
        sources is dropped entirely.
    max_workers:
        Processes to parse with.  ``None`` lets the pool decide; ``1`` forces
        serial reading.
    """
    # The project code is the filename stem, so both name filters can be
    # applied before anything is opened.  Doing it afterwards means parsing
    # every unrelated experiment in the repository and warning about each one
    # that does not fit this parser -- hundreds of lines of noise about files
    # we were always going to discard.
    vex_files = [
        path
        for path in sorted(Path(repo_path).rglob("*.vex"))
        if _wanted_file(path, project=project, project_prefix=project_prefix)
        and _has_content(path)
    ]
    if not vex_files:
        return []

    records = _read_all(vex_files, max_workers=max_workers)

    # Sort newest first (by start datetime)
    records.sort(key=lambda r: r.start, reverse=True)

    # --- Filtering ----------------------------------------------------------

    if max_age is not None:
        cutoff = datetime.now(timezone.utc) - max_age
        records = [r for r in records if r.start >= cutoff]

    if source is not None:
        source_lower = source.lower()
        records = [
            r
            for r in records
            if any(st.name.lower() == source_lower for st in r.sources)
        ]

    if keep_sources is not None:
        wanted = {name.lower() for name in keep_sources}
        trimmed: list[ObsRecord] = []
        for record in records:
            sources = [st for st in record.sources if st.name.lower() in wanted]
            if sources:
                trimmed.append(
                    ObsRecord(
                        project=record.project, start=record.start, sources=sources
                    )
                )
        records = trimmed

    if max_projects is not None:
        records = records[:max_projects]

    return records
