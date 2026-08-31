"""A fake ``vlbiplanobs`` for the test suite.

``precise_scheduler.visibility`` is a thin adapter over planobs, reaching it
through two seams: :meth:`VisibilityPlanner._import_planobs` and
:meth:`VisibilityPlanner._import_scheduler`.  :func:`install` replaces what
comes back through both, so tests can drive visibility answers and key-file
generation without planobs computing anything.

The fakes mirror the real API as observed from vlbiplanobs:

* ``Observation(band=, stations=, scans=, times=, [duration=], datarate=)``;
* ``Observation.per_source_observable()`` → ``{source: {station: bool array}}``,
  which includes each scan block's calibrators alongside the target itself;
* ``Stations(stations=[...])`` → ``.station_codenames``, ``.max_datarate(band)``;
* ``_STATIONS`` — the full station catalogue, indexed by codename;
* ``SourceCatalog(personal_catalog=path)`` → ``.blocks``, ``.pulsars``;
* ``ObservationScheduler(observation, fringefinder_spec=)`` → ``.schedule()``
  and ``.generate_key_file()``, which is what picks the fringe finders and
  returns the key file's text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import SimpleNamespace

import numpy as np

#: Stations the fake catalogue knows, codename → full name.
KNOWN_STATIONS: dict[str, str] = {
    "Ef": "Effelsberg",
    "Wb": "Westerbork",
    "Jb": "Jodrell Bank",
    "Tr": "Torun",
    "O8": "Onsala",
    "Ir": "Irbene",
    "Sr": "Sardinia",
}

#: Datarate the fake network reports for any band.
DATARATE: int = 2048


class FakeSourceType(Enum):
    """Mirrors ``vlbiplanobs.sources.SourceType``."""

    TARGET = auto()
    PHASECAL = auto()
    FRINGEFINDER = auto()
    AMPLITUDECAL = auto()
    CHECKSOURCE = auto()
    POLCAL = auto()
    PULSAR = auto()
    UNKNOWN = auto()


@dataclass
class FakeSource:
    name: str
    coordinates: object = None
    source_type: FakeSourceType = FakeSourceType.UNKNOWN

    @property
    def type(self) -> FakeSourceType:
        return self.source_type


@dataclass
class FakeScan:
    source: FakeSource
    duration: object = None
    every: int = -1


@dataclass
class FakeScanBlock:
    scans: list


def scan_block(*sources, duration=None) -> FakeScanBlock:
    """A block of one scan per (name, type) pair, with no duration set.

    Mirrors what planobs produces from a ``targets.toml`` entry: the catalogue
    carries no durations, so every scan arrives with ``duration=None``.
    """
    return FakeScanBlock(
        [
            FakeScan(source=FakeSource(name=n, source_type=t), duration=duration)
            for n, t in sources
        ]
    )


def target_block(name: str) -> FakeScanBlock:
    """A typical FRB block: phase calibrator, check source and the target."""
    return scan_block(
        (f"J{name}_pcal", FakeSourceType.PHASECAL),
        (f"J{name}_check", FakeSourceType.CHECKSOURCE),
        (name, FakeSourceType.TARGET),
    )


class FakeScheduler:
    """Stands in for ``vlbiplanobs.scheduler.ObservationScheduler``.

    planobs' scheduler is what picks the fringe finders and writes the key
    file, so the fake records the spec it was handed and replays canned key
    text.  It also mirrors the real one's side effect of registering the
    fringe finders it chose into the observation's scans.
    """

    #: Text ``generate_key_file`` returns.
    key_content: str = "! fake key file\n"

    #: Every construction: ``{"observation": obs, "fringefinder_spec": spec}``.
    calls: list[dict] = []

    #: Every ``generate_key_file`` call, in order.
    key_calls: list[dict] = []

    #: When set, ``schedule`` and ``generate_key_file`` raise it.
    raises: Exception | None = None

    #: Fringe finders the fake scheduler "picks", as ``{block: source}``.
    picks: dict = {}

    def __init__(self, observation, fringefinder_spec=None, **kwargs) -> None:
        type(self).calls.append(
            {"observation": observation, "fringefinder_spec": fringefinder_spec}
        )
        self.observation = observation
        self.fringefinder_spec = fringefinder_spec

    def schedule(self) -> dict:
        cls = type(self)
        if cls.raises is not None:
            raise cls.raises
        for name, source in cls.picks.items():
            self.observation.scans[name] = FakeScanBlock([FakeScan(source=source)])
        return self.observation.scans

    def generate_key_file(self, experiment_code: str = "EXCODE", **kwargs) -> str:
        cls = type(self)
        if cls.raises is not None:
            raise cls.raises
        cls.key_calls.append({"experiment_code": experiment_code, **kwargs})
        return cls.key_content


class FakeStation:
    def __init__(self, codename: str, name: str) -> None:
        self.codename = codename
        self.name = name


class FakeStationCatalogue:
    """Stands in for ``observation._STATIONS``."""

    def __init__(self, stations: dict[str, FakeStation]) -> None:
        self._stations = stations

    def __getitem__(self, code: str) -> FakeStation:
        return self._stations[code]

    @property
    def station_codenames(self) -> list[str]:
        return list(self._stations)


class FakeNetwork:
    """Stands in for the ``Stations`` instance built for one observation."""

    def __init__(self, stations: list[FakeStation]) -> None:
        self.stations = stations

    @property
    def station_codenames(self) -> list[str]:
        return [s.codename for s in self.stations]

    def max_datarate(self, band: str) -> int:
        return DATARATE


class FakeObservation:
    """Records how it was constructed; replays a canned observability answer.

    State is per-class and reset by :func:`install`, because the adapter
    constructs the class itself rather than receiving an instance.
    """

    #: ``{source: {station: list[bool]}}``; empty means "planobs said nothing".
    response: dict[str, dict[str, list[bool]]] = {}

    #: When set, every source not in ``response`` is visible to the whole array
    #: for the whole window.  Simpler than spelling out a full grid.
    all_visible: bool = False

    #: Every construction, in order, for assertions about what was passed.
    calls: list[dict] = []

    #: When set, ``per_source_observable`` and ``schedule_file`` raise it.
    raises: Exception | None = None

    #: Every ``schedule_file`` call, in order.
    schedule_calls: list[dict] = []

    #: Text ``schedule_file`` returns.
    key_content: str = "! fake key file\n"

    #: ``{block_name: source}`` that ``schedule_file`` registers into
    #: ``self.scans`` as it runs, the way planobs' scheduler registers the
    #: fringe finders it picked.
    extra_scans: dict = {}

    def __init__(self, **kwargs) -> None:
        type(self).calls.append(kwargs)
        self.kwargs = kwargs

    @property
    def scans(self) -> dict:
        return self.kwargs["scans"]

    def _n_slots(self) -> int:
        times = self.kwargs.get("times")
        try:
            return len(times)
        except TypeError:      # a single Time, as generate_schedule passes
            return 1

    def per_source_observable(self) -> dict[str, dict[str, np.ndarray]]:
        cls = type(self)
        if cls.raises is not None:
            raise cls.raises

        result = {
            source: {
                station: np.asarray(flags, dtype=bool)
                for station, flags in per_station.items()
            }
            for source, per_station in cls.response.items()
        }
        if cls.all_visible:
            stations = self.kwargs["stations"].station_codenames
            up = np.ones(self._n_slots(), dtype=bool)
            for source in self.kwargs["scans"]:
                result.setdefault(source, {s: up.copy() for s in stations})
        return result

    def schedule_file(self, experiment_code: str = "EXCODE", **kwargs) -> str:
        cls = type(self)
        if cls.raises is not None:
            raise cls.raises
        cls.schedule_calls.append({"experiment_code": experiment_code, **kwargs})
        for name, source in cls.extra_scans.items():
            self.scans[name] = FakeScanBlock([FakeScan(source=source)])
        return cls.key_content


def install(
    monkeypatch,
    *,
    blocks: list[str] | None = None,
    all_visible: bool = False,
    key_dir: Path | None = None,
    pulsars: list[str] | None = None,
) -> SimpleNamespace:
    """Point ``VisibilityPlanner`` at the fakes and return a handle to drive them.

    Parameters
    ----------
    blocks:
        Source names planobs has a scan block for.  Anything else is treated as
        unknown to planobs, exactly as a source missing from targets.toml is.
        ``None`` means "every source asked for is known".
    all_visible:
        Report every scheduled source as visible to the whole array, for the
        whole window.
    key_dir:
        Unused; kept so callers written against the old signature still work.
    pulsars:
        Pulsar names the catalogue offers.  ``None`` means no pulsar section at
        all, which is what ``SourceCatalog.pulsars`` reports as None.

    The handle exposes ``.observation`` and ``.scheduler`` (the recording fake
    classes), ``.blocks`` and ``.pulsars`` (both mutable) and
    ``.catalog_error`` (append an exception to make reading targets.toml fail).

    Fringe finders are not modelled: planobs' own scheduler picks and places
    them, so nothing in this project selects them.
    """
    from precise_scheduler.visibility import VisibilityPlanner

    FakeObservation.response = {}
    FakeObservation.all_visible = all_visible
    FakeObservation.calls = []
    FakeObservation.schedule_calls = []
    FakeObservation.raises = None
    FakeObservation.key_content = "! fake key file\n"
    FakeObservation.extra_scans = {}

    FakeScheduler.key_content = "! fake key file\n"
    FakeScheduler.calls = []
    FakeScheduler.key_calls = []
    FakeScheduler.raises = None
    FakeScheduler.picks = {}

    catalogue = FakeStationCatalogue(
        {code: FakeStation(code, name) for code, name in KNOWN_STATIONS.items()}
    )
    observation_mod = SimpleNamespace(
        Observation=FakeObservation,
        Stations=lambda stations: FakeNetwork(stations),
        _STATIONS=catalogue,
    )

    known: dict[str, FakeScanBlock] = (
        _AnyBlock()
        if blocks is None
        else {name: target_block(name) for name in blocks}
    )
    catalog_error: list[Exception] = []

    pulsar_blocks: dict[str, FakeScanBlock] | None = (
        None if pulsars is None
        else {n: scan_block((n, FakeSourceType.PULSAR)) for n in pulsars}
    )

    def _source_catalog(personal_catalog: str):
        if catalog_error:
            raise catalog_error[0]
        return SimpleNamespace(blocks=known, pulsars=pulsar_blocks)

    sources_mod = SimpleNamespace(
        SourceCatalog=_source_catalog,
        Scan=FakeScan,
        ScanBlock=FakeScanBlock,
        Source=FakeSource,
        SourceType=FakeSourceType,
    )
    scheduler_mod = SimpleNamespace(ObservationScheduler=FakeScheduler)

    monkeypatch.setattr(
        VisibilityPlanner,
        "_import_planobs",
        staticmethod(lambda: (observation_mod, sources_mod)),
    )
    monkeypatch.setattr(
        VisibilityPlanner, "_import_scheduler", staticmethod(lambda: scheduler_mod)
    )

    return SimpleNamespace(
        observation=FakeObservation,
        scheduler=FakeScheduler,
        sources=sources_mod,
        blocks=known,
        pulsars=pulsar_blocks,
        catalog_error=catalog_error,
        catalogue=catalogue,
    )


class _AnyBlock(dict):
    """A block catalogue that claims to know every source it is asked about.

    Each lookup yields a typical FRB block — phase calibrator, check source
    and target — so callers that only care that *some* block exists do not
    have to spell one out.
    """

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: object) -> FakeScanBlock:
        return target_block(str(key))
