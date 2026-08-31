"""Tests for precise_scheduler.selector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from precise_scheduler.activity import Detection
from precise_scheduler.selector import (
    ACTIVITY_WINDOW_DAYS,
    DEFAULT_MAX_SOURCES,
    eligible,
    propose,
)
from precise_scheduler.targets import Target


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _utc(days_ago: float = 0) -> datetime:
    """Return a UTC-aware datetime *days_ago* days before now."""
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def make_activity(detections: dict[str, list[datetime]]) -> MagicMock:
    """
    Build a mock Activity whose ``get_detections(name, since_days)`` returns
    ``Detection`` objects for *name* using the datetimes in *detections*, but
    only those whose ``utc_datetime`` falls within the requested window.

    Results are returned newest-first, mirroring the real implementation.
    """
    def _get_detections(canonical_name: str, since_days: int) -> list[Detection]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        raw = detections.get(canonical_name, [])
        results = [
            Detection(
                canonical_name=canonical_name,
                utc_datetime=dt,
                facility="CHIME",
            )
            for dt in raw
            if dt >= cutoff
        ]
        results.sort(key=lambda d: d.utc_datetime, reverse=True)
        return results

    mock = MagicMock()
    mock.get_detections.side_effect = _get_detections
    mock.get_active_sources.return_value = []
    return mock


def make_target(name: str, canonical: str, priority: int) -> Target:
    """Build a minimal Target, using MagicMock for irrelevant nested fields."""
    return Target(
        name=name,
        canonical_name=canonical,
        priority=priority,
        coordinates=MagicMock(),
        phasecal=MagicMock(),
        checksource=MagicMock(),
    )


def make_grid(*target_names: str, visible: bool = True) -> dict[str, list[int]]:
    """Return a VisibilityGrid with all-2 slots (visible) or all-0 (invisible)."""
    return {n: ([2, 2, 2, 2] if visible else [0, 0, 0, 0]) for n in target_names}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPriorityZeroExclusion:
    def test_priority_zero_always_excluded(self) -> None:
        target = make_target("R_P0", "FRB20000000A", priority=0)
        activity = make_activity({"FRB20000000A": [_utc(1)]})
        grid = make_grid("R_P0", visible=True)

        result = propose([target], activity, grid)

        assert result == []


class TestPriorityOneActivity:
    def test_priority_one_excluded_when_inactive(self) -> None:
        """A priority-1 source whose last detection is older than ACTIVITY_WINDOW_DAYS."""
        target = make_target("R_P1_OLD", "FRB20010101A", priority=1)
        # Detection is just outside the window.
        activity = make_activity({"FRB20010101A": [_utc(ACTIVITY_WINDOW_DAYS + 1)]})
        grid = make_grid("R_P1_OLD", visible=True)

        result = propose([target], activity, grid)

        assert result == []

    def test_priority_one_included_when_active(self) -> None:
        """A priority-1 source with a detection comfortably inside the window."""
        target = make_target("R_P1_NEW", "FRB20010101B", priority=1)
        activity = make_activity({"FRB20010101B": [_utc(5)]})
        grid = make_grid("R_P1_NEW", visible=True)

        result = propose([target], activity, grid)

        assert result == [target]


class TestPriorityTwoActivity:
    def test_priority_two_included_regardless_of_activity(self) -> None:
        """A priority-2 source with zero detections is still eligible."""
        target = make_target("R_P2_NODET", "FRB20020202A", priority=2)
        activity = make_activity({})  # no detections at all
        grid = make_grid("R_P2_NODET", visible=True)

        result = propose([target], activity, grid)

        assert result == [target]


class TestVisibilityExclusion:
    def test_zero_visibility_source_excluded(self) -> None:
        """A priority-2 source with all-zero slots is excluded."""
        target = make_target("R_INVIS", "FRB20030303A", priority=2)
        activity = make_activity({"FRB20030303A": [_utc(1)]})
        grid = make_grid("R_INVIS", visible=False)

        result = propose([target], activity, grid)

        assert result == []

    def test_absent_from_grid_excluded(self) -> None:
        """A target whose name is not in visibility_grid at all is excluded."""
        target = make_target("R_ABSENT", "FRB20030303B", priority=2)
        activity = make_activity({"FRB20030303B": [_utc(1)]})
        grid: dict[str, list[int]] = {}  # empty grid

        result = propose([target], activity, grid)

        assert result == []


class TestMaxSourcesCap:
    def test_max_sources_cap(self) -> None:
        """With 6 valid priority-2 candidates and max_sources=4, return exactly 4."""
        targets = [
            make_target(f"R_SRC{i}", f"FRB2000000{i}A", priority=2)
            for i in range(6)
        ]
        detections = {f"FRB2000000{i}A": [_utc(i + 1)] for i in range(6)}
        activity = make_activity(detections)
        grid = make_grid(*[t.name for t in targets], visible=True)

        result = propose(targets, activity, grid, max_sources=4)

        assert len(result) == 4


class TestRanking:
    def test_ranking_most_recent_first(self) -> None:
        """Two priority-2 sources; the one detected more recently ranks first."""
        t_recent = make_target("R_RECENT", "FRB20040404A", priority=2)
        t_old = make_target("R_OLD", "FRB20040404B", priority=2)
        activity = make_activity({
            "FRB20040404A": [_utc(2)],
            "FRB20040404B": [_utc(10)],
        })
        grid = make_grid("R_RECENT", "R_OLD", visible=True)

        result = propose([t_old, t_recent], activity, grid)

        assert result[0] == t_recent
        assert result[1] == t_old

    def test_ranking_priority_descending_tiebreaker(self) -> None:
        """Same recency: priority-2 should rank before priority-1."""
        t_p2 = make_target("R_P2", "FRB20050505A", priority=2)
        t_p1 = make_target("R_P1", "FRB20050505B", priority=1)
        # Both detected at the same time
        same_time = _utc(5)
        activity = make_activity({
            "FRB20050505A": [same_time],
            "FRB20050505B": [same_time],
        })
        grid = make_grid("R_P2", "R_P1", visible=True)

        result = propose([t_p1, t_p2], activity, grid)

        assert result[0] == t_p2
        assert result[1] == t_p1

    def test_ranking_alphabetical_final_tiebreaker(self) -> None:
        """Same recency and priority → alphabetical order by canonical_name."""
        t_alpha = make_target("R_ALPHA", "FRB20060606A", priority=2)
        t_beta = make_target("R_BETA", "FRB20060606B", priority=2)
        same_time = _utc(5)
        activity = make_activity({
            "FRB20060606A": [same_time],
            "FRB20060606B": [same_time],
        })
        grid = make_grid("R_ALPHA", "R_BETA", visible=True)

        result = propose([t_beta, t_alpha], activity, grid)

        assert result[0] == t_alpha   # "FRB20060606A" < "FRB20060606B"
        assert result[1] == t_beta


class TestEdgeCases:
    def test_empty_target_list_returns_empty(self) -> None:
        activity = make_activity({})
        result = propose([], activity, {})
        assert result == []

    def test_all_candidates_excluded_returns_empty(self) -> None:
        """Mix of exclusion reasons; nothing survives filtering."""
        t_p0 = make_target("R_P0", "FRB20070707A", priority=0)
        t_p1_stale = make_target("R_P1S", "FRB20070707B", priority=1)
        t_invis = make_target("R_INV", "FRB20070707C", priority=2)
        activity = make_activity({
            "FRB20070707A": [_utc(1)],
            "FRB20070707B": [_utc(ACTIVITY_WINDOW_DAYS + 5)],
            "FRB20070707C": [_utc(1)],
        })
        grid: dict[str, list[int]] = {
            "R_P0": [2, 2, 2, 2],
            "R_P1S": [2, 2, 2, 2],
            "R_INV": [0, 0, 0, 0],
        }

        result = propose([t_p0, t_p1_stale, t_invis], activity, grid)

        assert result == []

    def test_no_detections_sorts_last(self) -> None:
        """An undetected priority-2 source ranks after a recently-detected one."""
        t_detected = make_target("R_DET", "FRB20080808A", priority=2)
        t_undetected = make_target("R_UNDET", "FRB20080808B", priority=2)
        activity = make_activity({
            "FRB20080808A": [_utc(3)],
            # FRB20080808B has no detections
        })
        grid = make_grid("R_DET", "R_UNDET", visible=True)

        result = propose([t_undetected, t_detected], activity, grid)

        assert result[0] == t_detected
        assert result[1] == t_undetected


# ---------------------------------------------------------------------------
# eligible — pre-approval, independent of any observing window
# ---------------------------------------------------------------------------


class TestEligible:
    def test_priority_two_is_always_pre_approved(self) -> None:
        """A priority-2 source needs no recent detection."""
        target = make_target("R_P2", "FRB20080808A", priority=2)
        assert eligible([target], make_activity({})) == [target]

    def test_priority_zero_is_never_pre_approved(self) -> None:
        target = make_target("R_P0", "FRB20080808A", priority=0)
        assert eligible([target], make_activity({"FRB20080808A": [_utc(1)]})) == []

    def test_active_priority_one_is_pre_approved(self) -> None:
        target = make_target("R_P1", "FRB20080808A", priority=1)
        activity = make_activity({"FRB20080808A": [_utc(ACTIVITY_WINDOW_DAYS - 1)]})
        assert eligible([target], activity) == [target]

    def test_quiet_priority_one_is_not_pre_approved(self) -> None:
        target = make_target("R_P1", "FRB20080808A", priority=1)
        activity = make_activity({"FRB20080808A": [_utc(ACTIVITY_WINDOW_DAYS + 1)]})
        assert eligible([target], activity) == []

    def test_priority_one_without_canonical_name_is_not_pre_approved(self) -> None:
        """With no canonical name there is nothing to match an activity record."""
        target = Target(
            name="R_NONAME",
            canonical_name=None,
            priority=1,
            coordinates=MagicMock(),
        )
        assert eligible([target], make_activity({})) == []

    def test_empty_activity_leaves_only_priority_two(self) -> None:
        """The state of the campaign today: nothing recorded, so priority 2 only."""
        p2 = make_target("R_P2", "FRB20080808A", priority=2)
        p1 = make_target("R_P1", "FRB20080808B", priority=1)
        p0 = make_target("R_P0", "FRB20080808C", priority=0)

        assert eligible([p2, p1, p0], make_activity({})) == [p2]

    def test_catalogue_order_is_preserved(self) -> None:
        first = make_target("R_B", "FRB20080808B", priority=2)
        second = make_target("R_A", "FRB20080808A", priority=2)
        assert eligible([first, second], make_activity({})) == [first, second]

    def test_propose_agrees_with_eligible(self) -> None:
        """propose applies exactly the same pre-approval, so it is idempotent."""
        p2 = make_target("R_P2", "FRB20080808A", priority=2)
        p1_quiet = make_target("R_P1", "FRB20080808B", priority=1)
        activity = make_activity({})
        grid = make_grid("R_P2", "R_P1", visible=True)

        pre_approved = eligible([p2, p1_quiet], activity)

        assert propose([p2, p1_quiet], activity, grid) == propose(
            pre_approved, activity, grid
        )
