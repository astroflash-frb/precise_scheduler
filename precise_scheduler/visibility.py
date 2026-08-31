"""
Visibility for PRECISE Scheduler — a thin adapter over ``planobs``.

Everything about where antennas are, when a source is up, and which stations
can observe it is answered by ``planobs`` (the ``vlbiplanobs`` distribution).
This module holds no station catalogue, no coordinate maths and no elevation
model: it builds a :class:`vlbiplanobs.observation.Observation` from the
pre-selected sources and reads the answer back.

Note the import name: the package is ``vlbiplanobs``, the command-line tool it
installs is ``planobs``.  ``import planobs`` does not work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .calendar import ObsWindow
    from .targets import Target

log = logging.getLogger(__name__)

#: Per-source, per-15-min antenna counts, exactly as planobs reported them.
VisibilityGrid = dict[str, list[int]]

#: Resolution of the visibility sampling, in minutes.
SLOT_MINUTES: float = 15.0

#: PRECISE observes at 21 cm.
DEFAULT_BAND: str = "21cm"

#: Frequency setup written into the ``.key`` file, and the one to use when SRT
#: observes alone.  Both are named ``SETINI`` groups, not setup files: the
#: matching ``SETINI = <name> / ... /`` block is added to the key file
#: externally, so pySCHED cannot resolve them from a freshly generated
#: schedule on its own.
DEFAULT_SETUP: str = "EFF_BAND_32"
SRT_ONLY_SETUP: str = "SRT_BAND_2G_32"
SRT_CODES = frozenset({"SR", "SRT", "SARDINIA"})

#: Fringe finders are planobs' business: ``ObservationScheduler`` picks the
#: source and places the scans itself (its ``fringefinder_spec`` defaults to
#: two scans).  Nothing here selects them — see :meth:`generate_schedule`.

#: How many pulsars to add, for the pulsar back-end check.
DEFAULT_PULSARS: int = 1

#: Scan lengths, by planobs source type.  ``targets.toml`` is a shared source
#: catalogue and carries no durations, so the observing strategy lives here:
#: a 21 cm phase-referencing cycle of 1.5 min on the calibrator and 4 min on
#: the target, with the check source visited every few cycles.
SCAN_MINUTES: dict[str, float] = {
    "TARGET": 4.0,
    "PHASECAL": 1.5,
    "CHECKSOURCE": 2.0,
    "FRINGEFINDER": 5.0,
    "PULSAR": 5.0,
}

#: Scan length for a source whose type is none of the above.
DEFAULT_SCAN_MINUTES: float = 4.0


class VisibilityError(Exception):
    """Raised when planobs is unavailable or cannot answer."""


@dataclass(frozen=True)
class SourceObservability:
    """What planobs says about one source over one observation window."""

    target: Target
    n_antennas: int            # most antennas that can observe it simultaneously
    antennas: list[str]        # station codenames that can observe it at some point
    slots: list[int]           # antennas on source in each 15-min slot
    slots_at_best: int         # how many slots reach n_antennas
    n_slots: int

    @property
    def hours_at_best(self) -> float:
        return self.slots_at_best * SLOT_MINUTES / 60.0


@dataclass(frozen=True)
class ObservabilityReport:
    """planobs' answer for every source asked about."""

    sources: list[SourceObservability]
    array: list[str]           # station codenames planobs resolved for this run
    n_slots: int

    @property
    def n_array(self) -> int:
        return len(self.array)

    @property
    def grid(self) -> dict[str, list[int]]:
        """Per-source, per-slot antenna counts, as planobs reported them."""
        return {s.target.name: s.slots for s in self.sources}

    def observable(self, min_antennas: int) -> list[SourceObservability]:
        """Sources reaching at least *min_antennas* simultaneously."""
        return [s for s in self.sources if s.n_antennas >= min_antennas]


class VisibilityPlanner:
    """Ask planobs what is observable, and let it write the schedule.

    Parameters
    ----------
    targets_toml_path:
        Path to ``targets.toml``.  planobs reads it itself — the phase
        calibrator and check source of each entry come along with the target.
    band, setup:
        Observing setup passed through to planobs.
    fringe_finders:
        planobs' ``fringefinder_spec``, passed through untouched: a single
        count (``["3"]``) or explicit source names (``["J0927+3902"]``).
        ``None`` leaves planobs on its own default of two scans.
    pulsars:
        Same shape, for the pulsar check — a count (``["2"]``), explicit names
        from ``targets.toml`` (``["B0329+54_D"]``), or ``["0"]`` for none.
        ``None`` means :data:`DEFAULT_PULSARS`.
    """

    def __init__(
        self,
        targets_toml_path: Path,
        band: str = DEFAULT_BAND,
        setup: str | None = None,
        fringe_finders: list[str] | None = None,
        pulsars: list[str] | None = None,
    ) -> None:
        self.targets_toml_path = Path(targets_toml_path)
        self.band = band
        self.setup = setup
        self.fringe_finders = fringe_finders
        self.pulsars = pulsars

    # ------------------------------------------------------------------ #

    def setup_for(self, antennas: list[str]) -> str:
        """Frequency setup for this array: SRT observing alone gets its own."""
        if self.setup:
            return self.setup
        codes = {a.strip().upper() for a in antennas}
        return SRT_ONLY_SETUP if codes and codes <= SRT_CODES else DEFAULT_SETUP

    def observability(
        self, targets: list[Target], obs_window: ObsWindow
    ) -> ObservabilityReport:
        """Ask planobs which of *targets* can be observed, and by how many antennas.

        Only the given targets are put in the observation — the rest of
        ``targets.toml`` is not scheduled, even though planobs parses the whole
        file to find them.
        """
        observation_mod, sources_mod = self._import_planobs()
        import astropy.units as u
        import numpy as np
        from astropy.time import Time

        catalog = self._catalog(sources_mod)
        blocks = catalog.blocks

        missing = [t.name for t in targets if t.name not in blocks]
        if missing:
            log.warning(
                "planobs has no scan block for %d source(s): %s",
                len(missing), ", ".join(missing),
            )
        wanted = [t for t in targets if t.name in blocks]
        if not wanted:
            raise VisibilityError(
                "planobs found none of the requested sources in "
                f"{self.targets_toml_path}."
            )

        network = self._network(observation_mod, obs_window.antennas)
        n_slots = max(
            1,
            int(
                (obs_window.end - obs_window.start).total_seconds()
                // (SLOT_MINUTES * 60.0)
            ),
        )
        times = Time(
            obs_window.start.strftime("%Y-%m-%d %H:%M:%S"), scale="utc"
        ) + np.arange(n_slots) * SLOT_MINUTES * u.min

        try:
            observation = observation_mod.Observation(
                band=self.band,
                stations=network,
                scans={t.name: blocks[t.name] for t in wanted},
                times=times,
                datarate=network.max_datarate(self.band),
            )
            per_source = observation.per_source_observable()
        except Exception as exc:
            raise VisibilityError(f"planobs visibility query failed: {exc}") from exc

        array = list(network.station_codenames)
        results: list[SourceObservability] = []
        for target in wanted:
            per_station = per_source.get(target.name) or {}
            if not per_station:
                results.append(
                    SourceObservability(target, 0, [], [0] * n_slots, 0, n_slots)
                )
                continue
            codenames = list(per_station)
            stacked = np.array(
                [np.asarray(per_station[c], dtype=bool) for c in codenames]
            )
            per_slot = stacked.sum(axis=0)
            best = int(per_slot.max()) if per_slot.size else 0
            results.append(
                SourceObservability(
                    target=target,
                    n_antennas=best,
                    antennas=[c for c, row in zip(codenames, stacked) if row.any()],
                    slots=[int(v) for v in per_slot],
                    slots_at_best=int((per_slot == best).sum()) if best else 0,
                    n_slots=n_slots,
                )
            )

        return ObservabilityReport(sources=results, array=array, n_slots=n_slots)

    def get_visibility_grid(
        self, targets: list[Target], obs_window: ObsWindow
    ) -> VisibilityGrid:
        """Per-source, per-15-min antenna counts, as planobs reported them."""
        return self.observability(targets, obs_window).grid

    def generate_schedule(
        self,
        targets: list[Target],
        obs_window: ObsWindow,
        output_path: Path,
        *,
        experiment_code: str | None = None,
    ) -> Path:
        """Have planobs write the ``.key`` schedule file for *targets*.

        Alongside the confirmed FRBs, :data:`DEFAULT_PULSARS` pulsar is added
        from the ``[[pulsar]]`` entries of ``targets.toml`` — the one the whole
        array can see for longest — as the pulsar back-end check.  If no pulsar
        is up the schedule is still written without one.

        Fringe finders are *not* chosen here.  planobs'
        ``ObservationScheduler`` selects the sources and places the scans;
        :attr:`fringe_finders` is passed straight through to it as its
        ``fringefinder_spec``.  It skips that selection entirely if the
        observation already contains fringe-finder blocks, so adding our own
        would silently override it.

        Returns the path of the ``.key`` file written.
        """
        observation_mod, sources_mod = self._import_planobs()
        import astropy.units as u
        import numpy as np
        from astropy.time import Time

        catalog = self._catalog(sources_mod)
        blocks = catalog.blocks

        scans = {
            t.name: self._with_durations(sources_mod, blocks[t.name])
            for t in targets
            if t.name in blocks
        }
        if not scans:
            raise VisibilityError("planobs has no scan block for any confirmed target.")

        network = self._network(observation_mod, obs_window.antennas)
        duration_hours = (
            obs_window.end - obs_window.start
        ).total_seconds() / 3600.0
        start = Time(
            obs_window.start.strftime("%Y-%m-%d %H:%M:%S"), scale="utc"
        )
        n_slots = max(1, int(duration_hours * 60.0 // SLOT_MINUTES))
        times = start + np.arange(n_slots) * SLOT_MINUTES * u.min

        for name, block in self._pulsars(
            observation_mod, sources_mod, catalog, network, times, np
        ).items():
            scans[name] = block

        project = experiment_code or obs_window.project
        output_path = Path(output_path)
        try:
            observation = observation_mod.Observation(
                band=self.band,
                stations=network,
                scans=scans,
                times=times,
                duration=duration_hours * u.hour,
                datarate=network.max_datarate(self.band),
            )
            # ObservationScheduler is planobs' own scheduler: it picks the
            # fringe finders, lays out the scans and writes the key file's
            # text.  Going through it rather than Observation.schedule_file is
            # what makes `fringe_finders` reachable at all, and it writes the
            # source catalogue after scheduling, so the fringe finders it
            # chose are in it.
            scheduler_mod = self._import_scheduler()
            scheduler = scheduler_mod.ObservationScheduler(
                observation, fringefinder_spec=self.fringe_finders
            )
            scheduler.schedule()
            key_content = scheduler.generate_key_file(
                experiment_code=project,
                setup_file=self.setup_for(obs_window.antennas),
            )
        except Exception as exc:
            raise VisibilityError(f"planobs schedule generation failed: {exc}") from exc

        if not key_content or not key_content.strip():
            raise VisibilityError(
                f"planobs returned an empty schedule for {project}."
            )


        self._warn_if_dropped(key_content, scans, targets)

        output_path.mkdir(parents=True, exist_ok=True)
        key_file = output_path / f"{project}.key"
        key_file.write_text(key_content, encoding="utf-8")
        log.info(
            "Generated schedule file: %s (%d scans over %.1f h)",
            key_file, key_content.count("gap="), duration_hours,
        )
        return key_file

    # ------------------------------------------------------------------ #
    # Scan-block assembly                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _scan_minutes(source_type) -> float:
        """Scan length for a planobs source type."""
        return SCAN_MINUTES.get(getattr(source_type, "name", ""), DEFAULT_SCAN_MINUTES)

    def _with_durations(self, sources_mod, block):
        """Return *block* with a duration on every scan.

        ``targets.toml`` carries no durations, so planobs parses every scan
        with ``duration=None`` — and its scheduler cannot lay out a scan whose
        length is unknown.  Fill them in from :data:`SCAN_MINUTES`, leaving any
        duration the catalogue does specify untouched.
        """
        import astropy.units as u

        scans = [
            sources_mod.Scan(
                source=scan.source,
                duration=(
                    scan.duration
                    if scan.duration is not None
                    else self._scan_minutes(scan.source.type) * u.min
                ),
                every=scan.every,
            )
            for scan in block.scans
        ]
        return sources_mod.ScanBlock(scans)

    def _warn_if_dropped(self, key_content: str, scans: dict, targets) -> None:
        """Report sources that were asked for but did not reach the key file.

        planobs' scheduler lays out what fits the window and quietly drops the
        rest, so a named pulsar or fringe finder can vanish even when it is up.
        Silence there would be the worst outcome: the schedule looks fine and
        the source is simply absent.
        """
        def _absent(name: str) -> bool:
            return f"source='{name}'" not in key_content

        dropped = [t.name for t in targets if _absent(t.name)]
        if dropped:
            log.warning(
                "planobs dropped %d confirmed target(s) from the schedule: %s",
                len(dropped), ", ".join(dropped),
            )

        _, named_pulsars = self._parse_spec(self.pulsars, DEFAULT_PULSARS)
        target_names = {t.name for t in targets}
        pulsars_asked = named_pulsars or [
            n for n in scans if n not in target_names
        ]
        for name in pulsars_asked:
            if _absent(name):
                log.warning(
                    "planobs did not schedule the pulsar %s; the key file has "
                    "no pulsar check.", name,
                )

        _, named_ffs = self._parse_spec(self.fringe_finders, 0)
        for name in named_ffs:
            if _absent(name):
                log.warning(
                    "planobs did not schedule the fringe finder %s.", name
                )

    @staticmethod
    def _parse_spec(spec: list[str] | None, default_count: int) -> tuple[int, list[str]]:
        """Split a planobs-style spec into ``(count, explicit_names)``.

        Mirrors ``ObservationScheduler``'s ``fringefinder_spec`` convention so
        both options behave the same way on the command line: a lone number is
        how many to pick, anything else is the sources to use.
        """
        if spec is None:
            return default_count, []
        cleaned = [s.strip() for s in spec if s and s.strip()]
        if not cleaned:
            return default_count, []
        if len(cleaned) == 1 and cleaned[0].isdigit():
            return int(cleaned[0]), []
        return len(cleaned), cleaned

    def _pulsars(
        self, observation_mod, sources_mod, catalog, network, times, np
    ) -> dict:
        """Pick the pulsars for the back-end check.

        Pulsars come from the ``[[pulsar]]`` entries of ``targets.toml``; the
        catalogue holds several dozen, of which only some are up.  planobs has
        no pulsar selection of its own, so this is ours: by default the one the
        whole array can see for longest, or exactly the ones named in
        :attr:`pulsars`.
        """
        wanted, named = self._parse_spec(self.pulsars, DEFAULT_PULSARS)
        if wanted <= 0:
            log.info("Scheduling without a pulsar check, as asked.")
            return {}

        available = catalog.pulsars
        if not available:
            log.warning(
                "No pulsars in %s: scheduling without a pulsar check.",
                self.targets_toml_path,
            )
            return {}

        if named:
            missing = [n for n in named if n not in available]
            if missing:
                raise VisibilityError(
                    f"No [[pulsar]] entry in {self.targets_toml_path} for: "
                    f"{', '.join(missing)}. "
                    f"Known pulsars: {', '.join(sorted(available))}"
                )
            for name in named:
                log.info("Pulsar check: %s (named explicitly).", name)
            return {
                name: self._with_durations(sources_mod, available[name])
                for name in named
            }

        try:
            probe = observation_mod.Observation(
                band=self.band,
                stations=network,
                scans=dict(available),
                times=times,
                datarate=network.max_datarate(self.band),
            )
            visible = probe.per_source_observable()
        except Exception as exc:
            log.warning("Could not check pulsar visibility: %s", exc)
            return {}

        ranked: list[tuple[int, str]] = []
        for name in available:
            per_station = visible.get(name) or {}
            if not per_station:
                continue
            stacked = np.array(
                [np.asarray(v, dtype=bool) for v in per_station.values()]
            )
            # Slots where every antenna is on source at once.
            ranked.append((int(stacked.all(axis=0).sum()), name))

        ranked.sort(key=lambda pair: (-pair[0], pair[1]))
        chosen = [name for slots, name in ranked[:wanted] if slots > 0]
        if not chosen:
            log.warning("No pulsar is up for the whole array in this window.")
            return {}

        for slots, name in ranked[:wanted]:
            if slots > 0:
                log.info(
                    "Pulsar check: %s (whole array on source for %.1f h)",
                    name, slots * SLOT_MINUTES / 60.0,
                )
        return {
            name: self._with_durations(sources_mod, available[name])
            for name in chosen
        }

    # ------------------------------------------------------------------ #
    # planobs adapter — the only place that imports vlbiplanobs           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _import_scheduler():
        """planobs' own scheduler, which lays out scans and writes key files."""
        try:
            from vlbiplanobs import scheduler
        except ImportError as exc:
            raise VisibilityError(
                f"planobs has no scheduler module ({exc})."
            ) from exc
        return scheduler

    @staticmethod
    def _import_planobs():
        try:
            from vlbiplanobs import observation as observation_mod
            from vlbiplanobs import sources as sources_mod
        except ImportError as exc:
            raise VisibilityError(
                "planobs is not available: could not import 'vlbiplanobs' "
                f"({exc}). Install it with `pip install vlbiplanobs`."
            ) from exc
        return observation_mod, sources_mod

    def _catalog(self, sources_mod):
        try:
            return sources_mod.SourceCatalog(
                personal_catalog=str(self.targets_toml_path)
            )
        except Exception as exc:
            raise VisibilityError(
                f"planobs could not read {self.targets_toml_path}: {exc}"
            ) from exc

    def _network(self, observation_mod, antenna_labels: list[str]):
        """Resolve antenna labels against planobs' own station catalogue."""
        catalogue = observation_mod._STATIONS
        picked, unknown = [], []
        for label in antenna_labels:
            name = label.strip()
            station = None
            try:
                station = catalogue[name]
            except (KeyError, TypeError):
                station = next(
                    (
                        catalogue[code]
                        for code in catalogue.station_codenames
                        if code.upper() == name.upper()
                        or catalogue[code].name.upper() == name.upper()
                    ),
                    None,
                )
            if station is None:
                unknown.append(name)
            else:
                picked.append(station)

        if unknown:
            log.warning("planobs does not know these antennas: %s", ", ".join(unknown))
        if not picked:
            raise VisibilityError(
                f"planobs recognised none of the antennas {antenna_labels}."
            )
        return observation_mod.Stations(stations=picked)
