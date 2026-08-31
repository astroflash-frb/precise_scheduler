"""Tests for the targets loader (precise_scheduler.targets)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precise_scheduler.targets import (
    DEFAULT_PRIORITY,
    Calibrator,
    Coordinates,
    Target,
    TargetSchemaError,
    load_targets,
)


# --------------------------------------------------------------------------- #
# TOML building blocks                                                        #
# --------------------------------------------------------------------------- #

def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "targets.toml"
    p.write_text(content, encoding="utf-8")
    return p


# A complete FRB entry: every scheduler field present.
_ENTRY_A = textwrap.dedent("""\
    [[target]]
    name = "R200223_D"
    canonical_name = "FRB20200223B"
    r_name = "R49"
    priority = 2
    dm = 202.268
    coordinates.RA = "00:33:03.600"
    coordinates.Dec = "28:49:51.600"
    coordinates.equinox = "J2000"
    phasecal.name = "J0034+2754"
    phasecal.coordinates.RA = "00:34:43.486105"
    phasecal.coordinates.Dec = "+27:54:25.72030"
    phasecal.coordinates.equinox = "J2000"
    phasecal.flux = "0.200"
    checksource.name = "J0019+2956"
    checksource.coordinates.RA = "00:19:37.790320"
    checksource.coordinates.Dec = "+29:56:01.92209"
    checksource.coordinates.equinox = "J2000"
""")

# A second FRB entry, different source.
_ENTRY_B = textwrap.dedent("""\
    [[target]]
    name = "R67_D"
    canonical_name = "FRB20201124A"
    priority = 1
    coordinates.RA = "05:08:03.500"
    coordinates.Dec = "+26:03:38.000"
    coordinates.equinox = "J2000"
    phasecal.name = "J0502+2516"
    phasecal.coordinates.RA = "05:02:58.474771"
    phasecal.coordinates.Dec = "+25:16:25.27542"
    phasecal.coordinates.equinox = "J2000"
    phasecal.flux = "0.155"
    checksource.name = "J0501+2530"
    checksource.coordinates.RA = "05:01:35.612880"
    checksource.coordinates.Dec = "+25:30:38.76578"
    checksource.coordinates.equinox = "J2000"
""")

# A non-FRB catalogue entry: coordinates only, no scheduler metadata.
_ENTRY_PULSAR_LIKE = textwrap.dedent("""\
    [[target]]
    name = "SGR1830"
    coordinates.RA = "18:30:41.5"
    coordinates.Dec = "-10:31:40.0"
    coordinates.equinox = "J2000"
""")


# --------------------------------------------------------------------------- #
# Fully specified entries                                                     #
# --------------------------------------------------------------------------- #

class TestLoadValidTargets:
    def test_load_valid_single_target(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_A))

        assert len(targets) == 1
        t = targets[0]
        assert isinstance(t, Target)
        assert t.name == "R200223_D"
        assert t.canonical_name == "FRB20200223B"
        assert t.r_name == "R49"
        assert t.priority == 2
        assert t.dm == pytest.approx(202.268)
        assert isinstance(t.coordinates, Coordinates)
        assert t.coordinates.ra == "00:33:03.600"
        assert t.coordinates.dec == "28:49:51.600"
        assert t.coordinates.equinox == "J2000"
        assert isinstance(t.phasecal, Calibrator)
        assert t.phasecal.name == "J0034+2754"
        assert isinstance(t.checksource, Calibrator)
        assert t.checksource.name == "J0019+2956"

    def test_load_multiple_targets(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_A + "\n" + _ENTRY_B))

        assert {t.name for t in targets} == {"R200223_D", "R67_D"}

    def test_flux_is_a_float(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_A))

        assert targets[0].phasecal.flux == pytest.approx(0.200)

    def test_unmeasured_flux_sentinel_becomes_none(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.replace('phasecal.flux = "0.200"', 'phasecal.flux = "-9.99"')
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].phasecal.flux is None

    def test_checksource_has_no_flux(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_A))

        assert targets[0].checksource.flux is None

    def test_equinox_defaults_to_j2000(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.replace('coordinates.equinox = "J2000"\n', "", 1)
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].coordinates.equinox == "J2000"


# --------------------------------------------------------------------------- #
# Optional scheduler metadata                                                 #
# --------------------------------------------------------------------------- #

class TestOptionalMetadata:
    def test_entry_without_priority_defaults_to_zero(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_PULSAR_LIKE))

        assert targets[0].priority == DEFAULT_PRIORITY == 0
        assert targets[0].is_schedulable is False

    def test_entry_without_canonical_name_loads(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.replace('canonical_name = "FRB20200223B"\n', "")
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].canonical_name is None

    def test_key_falls_back_to_internal_name(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.replace('canonical_name = "FRB20200223B"\n', "")
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].key == "R200223_D"

    def test_key_prefers_canonical_name(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_A))

        assert targets[0].key == "FRB20200223B"

    def test_non_frb_entries_are_still_returned(self, tmp_path: Path) -> None:
        content = _ENTRY_A + "\n" + _ENTRY_PULSAR_LIKE
        targets = load_targets(_write_toml(tmp_path, content))

        assert {t.name for t in targets} == {"R200223_D", "SGR1830"}


# --------------------------------------------------------------------------- #
# Partial calibrator information                                              #
# --------------------------------------------------------------------------- #

class TestPartialCalibrators:
    def test_target_without_checksource_loads(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.split("checksource.name")[0]
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].phasecal is not None
        assert targets[0].checksource is None

    def test_target_without_any_calibrator_loads(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.split("phasecal.name")[0]
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].phasecal is None
        assert targets[0].checksource is None

    def test_calibrator_without_coordinates_loads(self, tmp_path: Path) -> None:
        entry = textwrap.dedent("""\
            [[target]]
            name = "R179_D"
            canonical_name = "FRB20251229A"
            priority = 1
            coordinates.RA = "20:41:23.856"
            coordinates.Dec = "+16:01:30.72"
            coordinates.equinox = "J2000"
            phasecal.name = "J2045+1547"
            phasecal.flux = "0.237"
            checksource.name = "J2052+1619"
        """)
        targets = load_targets(_write_toml(tmp_path, entry))

        t = targets[0]
        assert t.phasecal.name == "J2045+1547"
        assert t.phasecal.coordinates is None
        assert t.phasecal.flux == pytest.approx(0.237)
        assert t.checksource.coordinates is None

    def test_scan_sources_lists_target_and_calibrators(self, tmp_path: Path) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_A))

        assert targets[0].scan_sources == ["R200223_D", "J0034+2754", "J0019+2956"]

    def test_scan_sources_is_target_only_without_calibrators(
        self, tmp_path: Path
    ) -> None:
        targets = load_targets(_write_toml(tmp_path, _ENTRY_PULSAR_LIKE))

        assert targets[0].scan_sources == ["SGR1830"]

    def test_scan_sources_deduplicates_shared_calibrator(
        self, tmp_path: Path
    ) -> None:
        # Several catalogue entries use the same source as phasecal and
        # checksource; it must not be scheduled twice.
        entry = _ENTRY_A.replace('checksource.name = "J0019+2956"',
                                 'checksource.name = "J0034+2754"')
        targets = load_targets(_write_toml(tmp_path, entry))

        assert targets[0].scan_sources == ["R200223_D", "J0034+2754"]


# --------------------------------------------------------------------------- #
# Schema errors                                                               #
# --------------------------------------------------------------------------- #

class TestSchemaErrors:
    def test_missing_coordinates_raises(self, tmp_path: Path) -> None:
        entry = textwrap.dedent("""\
            [[target]]
            name = "R200223_D"
            priority = 1
        """)
        with pytest.raises(TargetSchemaError, match="R200223_D"):
            load_targets(_write_toml(tmp_path, entry))

    def test_incomplete_coordinates_raises(self, tmp_path: Path) -> None:
        entry = textwrap.dedent("""\
            [[target]]
            name = "R200223_D"
            priority = 1
            coordinates.RA = "00:33:03.600"
            coordinates.equinox = "J2000"
        """)
        with pytest.raises(TargetSchemaError, match="Dec"):
            load_targets(_write_toml(tmp_path, entry))

    def test_missing_name_raises(self, tmp_path: Path) -> None:
        entry = textwrap.dedent("""\
            [[target]]
            priority = 1
            coordinates.RA = "00:33:03.600"
            coordinates.Dec = "28:49:51.600"
        """)
        with pytest.raises(TargetSchemaError, match="name"):
            load_targets(_write_toml(tmp_path, entry))

    def test_invalid_priority_value_raises(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.replace("priority = 2", "priority = 3")
        with pytest.raises(TargetSchemaError, match="priority"):
            load_targets(_write_toml(tmp_path, entry))

    def test_calibrator_without_name_raises(self, tmp_path: Path) -> None:
        entry = textwrap.dedent("""\
            [[target]]
            name = "R200223_D"
            priority = 1
            coordinates.RA = "00:33:03.600"
            coordinates.Dec = "28:49:51.600"
            phasecal.flux = "0.200"
        """)
        with pytest.raises(TargetSchemaError, match="phasecal"):
            load_targets(_write_toml(tmp_path, entry))

    def test_non_numeric_dm_raises(self, tmp_path: Path) -> None:
        entry = _ENTRY_A.replace("dm = 202.268", 'dm = "not a number"')
        with pytest.raises(TargetSchemaError, match="dm"):
            load_targets(_write_toml(tmp_path, entry))

    def test_multiple_errors_reported_at_once(self, tmp_path: Path) -> None:
        bad_a = _ENTRY_A.replace("priority = 2", "priority = 99")
        bad_b = _ENTRY_B.replace('coordinates.Dec = "+26:03:38.000"\n', "")

        with pytest.raises(TargetSchemaError) as exc_info:
            load_targets(_write_toml(tmp_path, bad_a + "\n" + bad_b))

        message = str(exc_info.value)
        assert "R200223_D" in message
        assert "R67_D" in message
