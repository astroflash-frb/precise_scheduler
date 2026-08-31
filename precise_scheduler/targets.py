"""
Target loader for PRECISE Scheduler.

Reads a ``targets.toml`` file in which each source is declared as an
``[[target]]`` array-of-tables entry.  Returns strongly-typed dataclasses and
collects *all* schema violations before raising so the caller sees the complete
picture in one go.

Schema
------
``targets.toml`` is a shared source catalogue: it holds pulsars, magnetars,
calibrators and dummy positions alongside the PRECISE FRB targets.  Only the
FRB entries carry scheduling metadata, so every scheduler-specific field is
optional and defaults to "not part of the campaign":

======================  ========  ====================================
field                   required  meaning
======================  ========  ====================================
``name``                yes       internal PRECISE name, e.g. R200223_D
``coordinates``         yes       RA / Dec / equinox
``priority``            no (0)    0 = never schedule, 1 = schedule when
                                  active, 2 = schedule regardless
``canonical_name``      no        TNS/CHIME name, e.g. FRB20200223B —
                                  the join key with the activity database
``r_name``              no        CHIME repeater number, e.g. R49
``dm``                  no        dispersion measure, pc/cm^3
``phasecal``            no        phase calibrator (name required, its
                                  coordinates and flux optional)
``checksource``         no        check source (name required)
======================  ========  ====================================

A target with no ``phasecal`` and/or no ``checksource`` is legal: it is
scheduled with whatever calibrators it has, which may be none at all.  See
:attr:`Target.scan_sources`.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Priority assigned to an entry that does not declare one.  Zero means the
#: source is not part of the PRECISE campaign and is never scheduled.
DEFAULT_PRIORITY: int = 0

#: Valid values for ``priority``.
VALID_PRIORITIES: tuple[int, ...] = (0, 1, 2)

#: Sentinel used in targets.toml for "flux not measured".
UNKNOWN_FLUX: float = -9.99


class TargetSchemaError(Exception):
    """Raised when entries are missing required fields or have invalid values."""


@dataclass(frozen=True)
class Coordinates:
    ra: str      # sexagesimal string e.g. "00:33:03.600"
    dec: str
    equinox: str = "J2000"


@dataclass(frozen=True)
class Calibrator:
    name: str
    coordinates: Coordinates | None = None
    flux: float | None = None          # None when absent or the -9.99 sentinel


@dataclass(frozen=True)
class Target:
    name: str                          # internal PRECISE name, e.g. "R200223_D"
    coordinates: Coordinates
    priority: int = DEFAULT_PRIORITY
    canonical_name: str | None = None  # TNS/CHIME id, e.g. "FRB20200223B"
    r_name: str | None = None          # CHIME repeater number, e.g. "R49"
    dm: float | None = None
    phasecal: Calibrator | None = None
    checksource: Calibrator | None = None

    @property
    def key(self) -> str:
        """Identifier to use wherever a stable label is needed.

        The canonical name when CHIME has published one, else the internal
        PRECISE name.  Never ``None``, so it is safe as a sort key and in
        message rendering.
        """
        return self.canonical_name or self.name

    @property
    def is_schedulable(self) -> bool:
        """True when the selector may consider this target at all."""
        return self.priority >= 1

    @property
    def scan_sources(self) -> list[str]:
        """Source names planobs has to schedule for this target.

        The target itself, plus whichever calibrators exist.  A target with no
        phase calibrator is scheduled on its own — that is a real case in the
        catalogue, not an error.
        """
        names = [self.name]
        if self.phasecal is not None:
            names.append(self.phasecal.name)
        if self.checksource is not None and (
            self.phasecal is None or self.checksource.name != self.phasecal.name
        ):
            names.append(self.checksource.name)
        return names


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #

def _parse_coordinates(raw: dict, label: str, errors: list[str]) -> Coordinates | None:
    """Build a Coordinates from a raw TOML sub-table, or record an error."""
    missing = [k for k in ("RA", "Dec") if k not in raw]
    if missing:
        errors.append(f"  '{label}': coordinates missing {', '.join(missing)}")
        return None
    return Coordinates(
        ra=str(raw["RA"]),
        dec=str(raw["Dec"]),
        equinox=str(raw.get("equinox", "J2000")),
    )


def _parse_flux(raw: dict, label: str, errors: list[str]) -> float | None:
    """Parse ``flux`` to a float, mapping the -9.99 sentinel to None."""
    if "flux" not in raw:
        return None
    try:
        value = float(raw["flux"])
    except (TypeError, ValueError):
        errors.append(f"  '{label}': flux {raw['flux']!r} is not a number")
        return None
    return None if value == UNKNOWN_FLUX else value


def _parse_calibrator(
    raw: dict | None, label: str, errors: list[str]
) -> Calibrator | None:
    """Build a Calibrator, or None when the entry declares none.

    Coordinates and flux are optional: several catalogue entries record only a
    calibrator name.  A sub-table without a ``name`` is an error, because there
    is then nothing to schedule.
    """
    if not raw:
        return None
    if "name" not in raw:
        errors.append(f"  '{label}': calibrator sub-table has no 'name'")
        return None

    coordinates = None
    if "coordinates" in raw:
        coordinates = _parse_coordinates(
            raw["coordinates"], f"{label}.coordinates", errors
        )

    return Calibrator(
        name=str(raw["name"]),
        coordinates=coordinates,
        flux=_parse_flux(raw, label, errors),
    )


def _parse_priority(entry: dict, label: str, errors: list[str]) -> int:
    if "priority" not in entry:
        return DEFAULT_PRIORITY
    value = entry["priority"]
    if value not in VALID_PRIORITIES:
        errors.append(
            f"  '{label}': 'priority' must be one of "
            f"{', '.join(str(p) for p in VALID_PRIORITIES)} (got {value!r})"
        )
        return DEFAULT_PRIORITY
    return int(value)


def _parse_dm(entry: dict, label: str, errors: list[str]) -> float | None:
    if "dm" not in entry:
        return None
    try:
        return float(entry["dm"])
    except (TypeError, ValueError):
        errors.append(f"  '{label}': dm {entry['dm']!r} is not a number")
        return None


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def load_targets(path: Path) -> list[Target]:
    """
    Parse *path* (a ``targets.toml`` file) and return a list of
    :class:`Target` objects.

    Every ``[[target]]`` entry is returned, including the non-FRB sources that
    carry no ``priority`` — those come back with ``priority == 0`` so the
    selector ignores them while ``planobs`` can still resolve them by name.

    Raises
    ------
    TargetSchemaError
        If one or more entries are missing a name or coordinates, or have a
        ``priority`` outside 0-2, or declare a calibrator without a name.  All
        problems are reported in a single exception so the user can fix them
        all at once.
    FileNotFoundError
        If *path* does not exist (propagated from :func:`open`).
    tomllib.TOMLDecodeError
        If the file is not valid TOML (propagated from :func:`tomllib.load`).
    """
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    entries: list[dict] = raw.get("target", [])
    errors: list[str] = []
    targets: list[Target] = []

    for index, entry in enumerate(entries):
        label: str = entry.get("name", f"<entry {index}>")

        if "name" not in entry:
            errors.append(f"  '{label}': missing 'name'")
            continue
        if "coordinates" not in entry:
            errors.append(f"  '{label}': missing 'coordinates'")
            continue

        entry_errors: list[str] = []
        coordinates = _parse_coordinates(
            entry["coordinates"], label, entry_errors
        )
        priority = _parse_priority(entry, label, entry_errors)
        dm = _parse_dm(entry, label, entry_errors)
        phasecal = _parse_calibrator(
            entry.get("phasecal"), f"{label}.phasecal", entry_errors
        )
        checksource = _parse_calibrator(
            entry.get("checksource"), f"{label}.checksource", entry_errors
        )

        if entry_errors:
            errors.extend(entry_errors)
            continue

        canonical_name = entry.get("canonical_name")
        if priority >= 1 and not canonical_name:
            # DEBUG, not WARNING: these are standing gaps in the shared
            # catalogue, not problems with this run.  Every command loads
            # targets.toml, so warning here buries real output under ~60 lines.
            log.debug(
                "Target %r is schedulable (priority %d) but has no "
                "canonical_name; it can never be matched to an activity "
                "record or a CHIME VOEvent.",
                entry["name"],
                priority,
            )
        if priority >= 1 and phasecal is None:
            log.debug(
                "Target %r is schedulable but has no phase calibrator; it "
                "will be scheduled on its own.",
                entry["name"],
            )

        targets.append(
            Target(
                name=str(entry["name"]),
                coordinates=coordinates,
                priority=priority,
                canonical_name=str(canonical_name) if canonical_name else None,
                r_name=str(entry["r_name"]) if entry.get("r_name") else None,
                dm=dm,
                phasecal=phasecal,
                checksource=checksource,
            )
        )

    if errors:
        raise TargetSchemaError(
            "targets.toml has schema errors:\n" + "\n".join(errors)
        )

    return targets
