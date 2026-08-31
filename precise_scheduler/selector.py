"""
Target selector for PRECISE Scheduler.

Produces a ranked shortlist of FRB targets to include in an EVN proposal,
applying eligibility filters and a deterministic ranking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .targets import Target
    from .activity import Activity
    from .visibility import VisibilityGrid

# ---------------------------------------------------------------------------
# Named constants — all thresholds live here, never as magic numbers
# ---------------------------------------------------------------------------

ACTIVITY_WINDOW_DAYS: int = 30   # priority-1 sources need a detection within this window
DEFAULT_MAX_SOURCES: int = 4     # default cap on proposal size

# Look-back window used when fetching detections for ranking (≈100 years).
_RANKING_WINDOW_DAYS: int = 36_500


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def eligible(targets: list[Target], activity: Activity) -> list[Target]:
    """Return the targets that are pre-approved for observation.

    Pre-approval is the campaign's standing decision about a source, and is
    independent of any particular observing window:

    * priority 0 — never scheduled;
    * priority 1 — scheduled only while the source is active, i.e. it has a
      detection in the last :data:`ACTIVITY_WINDOW_DAYS` days;
    * priority 2 — always scheduled.

    A priority-1 target with no ``canonical_name`` can never be matched to an
    activity record, so it is never active by construction.

    Catalogue order is preserved.  This is the filter to apply *before* asking
    planobs anything: there is no point computing visibility for a source the
    campaign would not observe either way.
    """
    approved: list[Target] = []
    for target in targets:
        if target.priority == 0:
            continue
        if target.priority == 1:
            if target.canonical_name is None:
                continue
            if not activity.get_detections(
                target.canonical_name, since_days=ACTIVITY_WINDOW_DAYS
            ):
                continue
        approved.append(target)
    return approved


def propose(
    targets: list[Target],
    activity: Activity,
    visibility_grid: VisibilityGrid,
    max_sources: int = DEFAULT_MAX_SOURCES,
) -> list[Target]:
    """
    Produce a ranked proposal of up to *max_sources* targets.

    Rules (applied in order):
    1. Keep only the pre-approved sources — see :func:`eligible`: priority-0 is
       dropped unconditionally, priority-1 needs a detection in the last
       ``ACTIVITY_WINDOW_DAYS`` days.
    2. Exclude sources whose slots in *visibility_grid* sum to zero, or that
       are entirely absent from the grid.
    3. Rank remaining candidates by:
       a. Days since last detection — ascending (most recently active first).
          Sources with no detection at all sort last (treated as
          ``datetime.max``).
       b. Priority — descending (2 before 1).
       c. ``Target.key`` — alphabetical tiebreaker (the canonical
          name when known, else the internal PRECISE name).
    4. Return the top *max_sources* candidates.

    Callers that pre-filtered with :func:`eligible` lose nothing by passing the
    result here: rule 1 is idempotent.

    This is a pure function — no file I/O, no network calls, no side effects.
    """
    # Rules 1-2: pre-approved, and visible at some point in this window.
    candidates = [
        target
        for target in eligible(targets, activity)
        if _is_visible(target, visibility_grid)
    ]

    # Rule 3: rank candidates.
    now = datetime.now(timezone.utc)
    ranked = sorted(candidates, key=lambda t: _ranking_key(t, activity, now))

    # Rule 4: cap the list.
    return ranked[:max_sources]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_visible(target: Target, visibility_grid: VisibilityGrid) -> bool:
    """Return True iff the target has at least one non-zero slot in the grid."""
    return sum(visibility_grid.get(target.name, [])) > 0


def _ranking_key(
    target: Target,
    activity: Activity,
    now: datetime,
) -> tuple[float, int, str]:
    """
    Return a sort key for *target*.

    Sort order (all ascending in sorted()):
      (days_since_last_detection, -priority, target.key)

    Sources never detected get ``float('inf')`` for days_since, so they
    sort after all detected sources.  Priority is negated so that higher
    priority sorts first when days_since is equal.
    """
    records = (
        activity.get_detections(
            target.canonical_name, since_days=_RANKING_WINDOW_DAYS
        )
        if target.canonical_name is not None
        else []
    )

    if records:
        last_dt = records[0].utc_datetime
        days_since: float = (now - last_dt).total_seconds() / 86_400.0
    else:
        days_since = float("inf")

    return (days_since, -target.priority, target.key)
