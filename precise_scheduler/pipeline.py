"""
Scheduling pipeline stages driven from the CLI.

``precise schedule`` runs the same sequence the daemon uses to reach a draft
schedule, with two deliberate differences:

* the Slack round-trip is replaced — the proposal that *would* have been
  posted is returned as Block Kit blocks for the caller to render, and the
  ``--targets`` override stands in for the interactive confirmation;
* nothing is committed or pushed, so a tentative run leaves the schedules
  repository untouched apart from the generated ``.key`` file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

# selector imports nothing of ours at runtime, so this cannot go circular; it
# is at module level only so the "nothing is pre-approved" message can name the
# activity window the selector actually applies.
from .selector import ACTIVITY_WINDOW_DAYS

if TYPE_CHECKING:
    from .calendar import ObsWindow
    from .config import Config
    from .targets import Target
    from .visibility import SourceObservability, VisibilityGrid

log = logging.getLogger(__name__)

ACTIVE_WINDOW_SHORT_DAYS = 15
ACTIVE_WINDOW_LONG_DAYS = 30

#: How far back the "previous runs" summary looks.
RECENT_RUNS_MAX_PROJECTS = 5
RECENT_RUNS_MAX_AGE = timedelta(days=90)

#: Project code used for an --epoch run when the caller supplies none.
TENTATIVE_PROJECT = "TENTATIVE"

EPOCH_FORMAT = "YYYY-MM-DD HH:MM DURATION_HOURS"
_EPOCH_EXAMPLE = "2026-08-01 18:00 8"


class ScheduleInputError(Exception):
    """Raised when the caller's scheduling inputs cannot be used."""


@dataclass
class TentativeSchedule:
    """Outcome of a :func:`run_tentative_schedule` call."""

    obs_window: ObsWindow
    visibility_grid: VisibilityGrid
    proposed: list[Target]          # what the selector chose on its own
    selected: list[Target]          # what was actually scheduled
    forced: bool                    # True when --targets overrode the selector
    from_calendar: bool             # False when --epoch supplied the window
    proposal_blocks: list[dict[str, Any]]
    key_file: Path | None


def parse_epoch_spec(spec: str) -> tuple[datetime, timedelta]:
    """Parse ``'YYYY-MM-DD HH:MM DURATION_HOURS'`` into ``(start, duration)``.

    The start is interpreted as UTC, matching every other time in the tool.
    """
    parts = spec.split()
    if len(parts) != 3:
        raise ScheduleInputError(
            f"Cannot parse epoch {spec!r}. "
            f"Expected {EPOCH_FORMAT!r}, e.g. {_EPOCH_EXAMPLE!r}."
        )

    date_str, time_str, duration_str = parts

    try:
        start = datetime.strptime(
            f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ScheduleInputError(
            f"Cannot parse epoch start {date_str + ' ' + time_str!r}: {exc}. "
            "Expected 'YYYY-MM-DD HH:MM' (UTC)."
        ) from exc

    try:
        hours = float(duration_str)
    except ValueError as exc:
        raise ScheduleInputError(
            f"Cannot parse epoch duration {duration_str!r}: "
            "expected a number of hours, e.g. 8 or 7.5."
        ) from exc

    if hours <= 0:
        raise ScheduleInputError(
            f"Epoch duration must be positive (got {duration_str!r})."
        )

    return start, timedelta(hours=hours)


def resolve_obs_window(
    config: Config,
    *,
    epoch: str | None = None,
    antennas: list[str] | None = None,
    project: str | None = None,
) -> tuple[ObsWindow, bool]:
    """Return the window to schedule and whether it came from the calendar.

    Without *epoch* the next observation is read from the Google Sheets
    calendar.  With *epoch* the calendar is not consulted at all, so
    *antennas* must be supplied — guessing the array would silently produce a
    schedule for the wrong telescopes.
    """
    from .calendar import ObservationCalendar, ObsWindow

    if epoch is None:
        if antennas:
            raise ScheduleInputError(
                "--antennas only applies together with --epoch; without it the "
                "participating antennas come from the calendar."
            )
        if project:
            raise ScheduleInputError(
                "--project only applies together with --epoch; without it the "
                "project code comes from the calendar."
            )

        calendar = ObservationCalendar(spreadsheet_id=config.google_spreadsheet_id)
        obs_window = calendar.get_next_observation(
            lookahead_days=config.lookahead_days
        )
        if obs_window is None:
            raise ScheduleInputError(
                f"No observation found in the calendar within the next "
                f"{config.lookahead_days} days. "
                f"Pass --epoch {_EPOCH_EXAMPLE!r} --antennas ... to plan a "
                "specific window instead."
            )
        return obs_window, True

    if not antennas:
        raise ScheduleInputError(
            "--epoch requires --antennas (e.g. --antennas Ef Wb Tr Jb): the "
            "calendar is not consulted, so the participating antennas are "
            "otherwise unknown."
        )

    start, duration = parse_epoch_spec(epoch)
    window = ObsWindow(
        start=start,
        end=start + duration,
        antennas=list(antennas),
        project=project or TENTATIVE_PROJECT,
    )
    return window, False


def resolve_targets(all_targets: list[Target], names: list[str]) -> list[Target]:
    """Map user-supplied *names* onto :class:`Target` objects.

    Accepts either the internal PRECISE name (``R200223_D``) or the canonical
    name (``FRB20200223D``), case-insensitively, and preserves the order the
    caller asked for.  Every unknown name is reported at once.
    """
    lookup: dict[str, Target] = {}
    ambiguous: dict[str, list[Target]] = {}
    for target in all_targets:
        lookup[target.name.upper()] = target
    # Canonical names are not unique: several FRBs appear twice in the
    # catalogue (two localisations, or a date name and an R-number name).
    # Register those separately so a canonical-name lookup fails loudly
    # instead of silently resolving to whichever entry came last.
    by_canonical: dict[str, list[Target]] = {}
    for target in all_targets:
        if target.canonical_name:
            by_canonical.setdefault(target.canonical_name.upper(), []).append(target)
    for canonical, matches in by_canonical.items():
        if canonical in lookup:
            continue
        if len(matches) == 1:
            lookup[canonical] = matches[0]
        else:
            ambiguous[canonical] = matches

    resolved: list[Target] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for raw in names:
        cleaned = raw.strip().upper()
        if cleaned in ambiguous:
            options = ", ".join(sorted(t.name for t in ambiguous[cleaned]))
            raise ScheduleInputError(
                f"{raw!r} matches more than one catalogue entry ({options}). "
                "Use the internal PRECISE name to say which one you mean."
            )
        target = lookup.get(cleaned)
        if target is None:
            unknown.append(raw)
        elif target.name not in seen:
            seen.add(target.name)
            resolved.append(target)

    if unknown:
        known = ", ".join(
            sorted(
                f"{t.canonical_name} ({t.name})" if t.canonical_name else t.name
                for t in all_targets
            )
        )
        raise ScheduleInputError(
            f"Unknown target(s): {', '.join(unknown)}.\nKnown targets: {known}"
        )

    return resolved


def run_tentative_schedule(
    config: Config,
    *,
    epoch: str | None = None,
    antennas: list[str] | None = None,
    project: str | None = None,
    forced_target_names: list[str] | None = None,
    fringe_finders: list[str] | None = None,
    pulsars: list[str] | None = None,
    write_key: bool = True,
) -> TentativeSchedule:
    """Run the daemon's scheduling stages without touching Slack or git.

    Parameters
    ----------
    config:
        Loaded :class:`~precise_scheduler.config.Config`.
    epoch:
        ``'YYYY-MM-DD HH:MM DURATION_HOURS'``.  When given the calendar is
        skipped and *antennas* is required.
    antennas:
        Participating antenna codenames; only valid with *epoch*.
    project:
        Project code; only valid with *epoch*.  Defaults to
        :data:`TENTATIVE_PROJECT`.
    forced_target_names:
        When given, these targets are scheduled instead of the selector's
        choice, bypassing the priority and activity filters.
    fringe_finders, pulsars:
        planobs-style specs — a count (``["3"]``) or explicit source names —
        passed to the planner.  ``None`` leaves each at its default.
    write_key:
        Generate the ``.key`` file.  The file is never committed or pushed.
    """
    from .activity import Activity
    from .selector import propose
    from .slack_scheduler import build_proposal_blocks
    from .targets import load_targets
    from .visibility import VisibilityPlanner

    obs_window, from_calendar = resolve_obs_window(
        config, epoch=epoch, antennas=antennas, project=project
    )
    log.info(
        "Scheduling %s: %s → %s (%s)",
        obs_window.project,
        obs_window.start.isoformat(),
        obs_window.end.isoformat(),
        ", ".join(obs_window.antennas),
    )

    targets = load_targets(config.targets_toml)
    activity = Activity(config.activity_db)

    schedulable = [t for t in targets if t.is_schedulable]

    forced_targets: list[Target] = []
    if forced_target_names:
        forced_targets = resolve_targets(targets, forced_target_names)

    # A forced target may be priority 0, which the selector never considers.
    # The grid still has to cover it, or its visibility row would be missing
    # from the proposal and generate_schedule would plan it blind.
    grid_targets = list(schedulable)
    for target in forced_targets:
        if target not in grid_targets:
            grid_targets.append(target)

    planner = VisibilityPlanner(
        config.targets_toml, fringe_finders=fringe_finders, pulsars=pulsars
    )
    visibility_grid = planner.get_visibility_grid(grid_targets, obs_window)

    proposed = propose(
        targets=schedulable,
        activity=activity,
        visibility_grid=visibility_grid,
    )
    selected = forced_targets if forced_targets else proposed

    if not selected:
        raise ScheduleInputError(
            "No sources are schedulable for this window: every candidate was "
            "filtered out by priority, activity or visibility. "
            "Use --targets to force specific sources."
        )

    active_short = activity.get_active_sources(since_days=ACTIVE_WINDOW_SHORT_DAYS)
    active_long = activity.get_active_sources(since_days=ACTIVE_WINDOW_LONG_DAYS)

    proposal_blocks = build_proposal_blocks(
        obs_window=obs_window,
        visibility_grid=visibility_grid,
        proposed=selected,
        active_15d=active_short,
        active_30d=active_long,
        activity=activity,
        candidates=grid_targets,
    )

    key_file: Path | None = None
    if write_key:
        key_file = planner.generate_schedule(
            targets=selected,
            obs_window=obs_window,
            output_path=Path(config.schedules_repo),
        )

    return TentativeSchedule(
        obs_window=obs_window,
        visibility_grid=visibility_grid,
        proposed=proposed,
        selected=selected,
        forced=bool(forced_targets),
        from_calendar=from_calendar,
        proposal_blocks=proposal_blocks,
        key_file=key_file,
    )


# --------------------------------------------------------------------------- #
# precise plan — which FRBs can actually be observed in this run               #
# --------------------------------------------------------------------------- #

#: A source is kept only if the whole array, or all but one antenna, can
#: observe it at the same time.  Anything patchier is not worth a PRECISE slot.
MAX_ANTENNAS_MISSING = 1


@dataclass
class PlanResult:
    """Outcome of :func:`run_plan`."""

    obs_window: ObsWindow
    from_calendar: bool
    array: list[str]              # antennas planobs resolved
    n_slots: int
    min_antennas: int             # the all/all-but-one threshold applied
    observable: list[SourceObservability]   # kept, best-covered first
    dropped: list[SourceObservability]      # candidates that did not make the cut
    n_candidates: int
    forced: bool = False          # True when --targets named the candidates
    # (canonical_name, detection count) so the report needs no Activity of
    # its own to render.
    active_15d: list[tuple[str, int]] = field(default_factory=list)
    active_30d: list[tuple[str, int]] = field(default_factory=list)
    recent_runs: list[Any] = field(default_factory=list)   # ObsRecord
    key_file: Path | None = None  # written only when --key was asked for

    @property
    def n_array(self) -> int:
        return len(self.array)


def _counted_activity(activity, since_days: int) -> list[tuple[str, int]]:
    """Active sources in the window, each with how many detections it has."""
    return [
        (name, len(activity.get_detections(name, since_days=since_days)))
        for name in activity.get_active_sources(since_days=since_days)
    ]


def run_plan(
    config: Config,
    *,
    obs_window: ObsWindow | None = None,
    epoch: str | None = None,
    antennas: list[str] | None = None,
    project: str | None = None,
    candidate_names: list[str] | None = None,
    fringe_finders: list[str] | None = None,
    pulsars: list[str] | None = None,
    write_key: bool = False,
) -> PlanResult:
    """Which of the candidate FRBs can be observed in the upcoming run.

    Takes the observation window from the calendar (or *epoch*) and the
    pre-approved FRBs from ``targets.toml`` — priority 2 always, priority 1
    only while the source is active (see
    :func:`~precise_scheduler.selector.eligible`), or just *candidate_names*
    when the caller names them.  planobs is then asked to set up an
    observation containing only those sources, and the ones the full array —
    or all but one antenna — can observe are kept.

    Also gathers what the report needs alongside the visibility: the activity
    summary, and PRECISE's own previous runs.

    *obs_window* short-circuits the calendar lookup for a caller that has
    already done it.

    With *write_key* the ``.key`` file is generated as well — planobs picks the
    fringe finders and lays out the scans.  Nothing is ever committed, pushed
    or posted from here.
    """
    from .activity import Activity
    from .obs_schedules import PRECISE_PROJECT_PREFIX, get_records
    from .selector import eligible
    from .targets import load_targets
    from .visibility import VisibilityPlanner

    if obs_window is None:
        obs_window, from_calendar = resolve_obs_window(
            config, epoch=epoch, antennas=antennas, project=project
        )
    else:
        # The daemon has already read the calendar; asking again would be a
        # second Sheets fetch for the same answer.
        from_calendar = True

    activity = Activity(config.activity_db)
    targets = load_targets(config.targets_toml)
    forced = bool(candidate_names)
    if forced:
        candidates = resolve_targets(targets, candidate_names)
    else:
        # Only pre-approved sources are worth a planobs query: a source the
        # campaign would not observe is not made observable by being up.
        candidates = eligible(targets, activity)
    if not candidates:
        raise ScheduleInputError(
            "No FRB is pre-approved for this run: every priority-2 source is "
            "gone from targets.toml, and no priority-1 source has a detection "
            f"in the last {ACTIVITY_WINDOW_DAYS} days. "
            "Record detections with 'precise add-detection', raise a source's "
            "priority, or name sources with --targets."
        )

    log.info(
        "Asking planobs about %d pre-approved candidate(s) for %s",
        len(candidates), obs_window.project,
    )
    planner = VisibilityPlanner(
        config.targets_toml, fringe_finders=fringe_finders, pulsars=pulsars
    )
    report = planner.observability(candidates, obs_window)

    min_antennas = max(1, report.n_array - MAX_ANTENNAS_MISSING)
    kept = sorted(
        report.observable(min_antennas),
        key=lambda s: (-s.n_antennas, -s.slots_at_best, s.target.key),
    )
    kept_names = {s.target.name for s in kept}
    dropped = sorted(
        (s for s in report.sources if s.target.name not in kept_names),
        key=lambda s: (-s.n_antennas, s.target.key),
    )

    log.info(
        "%d of %d candidates observable by >= %d of %d antennas",
        len(kept), len(report.sources), min_antennas, report.n_array,
    )

    # Only PRECISE's own runs, and only the FRB names within them: the
    # schedules repository also holds unrelated experiments, and every run is
    # full of calibrators nobody wants listed back at them.
    frb_names = {t.name for t in targets if t.is_schedulable}
    recent_runs = get_records(
        repo_path=config.schedules_repo,
        max_projects=RECENT_RUNS_MAX_PROJECTS,
        max_age=RECENT_RUNS_MAX_AGE,
        project_prefix=PRECISE_PROJECT_PREFIX,
        keep_sources=frb_names,
    )

    key_file: Path | None = None
    if write_key:
        key_file = planner.generate_schedule(
            targets=[s.target for s in kept],
            obs_window=obs_window,
            output_path=Path(config.schedules_repo),
        )

    return PlanResult(
        obs_window=obs_window,
        from_calendar=from_calendar,
        array=report.array,
        n_slots=report.n_slots,
        min_antennas=min_antennas,
        observable=kept,
        dropped=dropped,
        n_candidates=len(report.sources),
        forced=forced,
        active_15d=_counted_activity(activity, ACTIVE_WINDOW_SHORT_DAYS),
        active_30d=_counted_activity(activity, ACTIVE_WINDOW_LONG_DAYS),
        recent_runs=recent_runs,
        key_file=key_file,
    )
