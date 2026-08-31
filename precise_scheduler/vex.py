"""VEX file parsing utilities and experiment metadata extraction.

Provides:
- parse_str_vex, get_obsdatetime, get_obsdate: low-level VEX text helpers
- ExperimentInfo, ScanInfo, load_experiment_info: structured metadata from .vex/.vax
"""
import os
import re
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .pyvexfile import Vex, Scan, Entry, EntryType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level VEX text helpers
# ---------------------------------------------------------------------------

def parse_str_vex(search_str: str, vaxfile: str, split_str: str | None = ':') -> str | list[str]:
    """Grep a VEX file for a string and return the parsed result.

    Args:
        search_str: String to search for in the vex file
        vaxfile: Path to the vex/vax file
        split_str: Delimiter to split on (None returns raw lines)

    Returns:
        Parsed string or list of strings from the vex file
    """
    if not os.path.isfile(vaxfile):
        raise FileNotFoundError(f"{vaxfile} could not be found in the current directory.")

    result = subprocess.run(["grep", search_str, vaxfile], capture_output=True)
    if result.returncode != 0:
        raise ValueError(f"grep {search_str} {vaxfile} returned error code {result.returncode}:"
                         f"\n{result.stdout}")

    if split_str is None:
        return result.stdout.decode("utf-8").strip().split('\n')
    else:
        temp = result.stdout.decode("utf-8").strip().split('\n')
        temp = [tt.split(split_str)[1].strip() for tt in temp]
        if len(temp) == 1:
            return temp[0]
        else:
            return temp


def get_obsdatetime(expname: str) -> datetime:
    """Get observation datetime from the .vix file, falling back to today.

    Args:
        expname: Experiment name

    Returns:
        datetime: Observation date
    """
    vixfile = f"{expname.upper()}.vix"
    if os.path.isfile(vixfile):
        return datetime.strptime(f"{' '.join(parse_str_vex('date', vixfile).split()[-3:])}",
                                 "%d %b %Y")
    else:
        return datetime.today()


def get_obsdate(expname: str, date_format: str = '%Y_%B') -> str:
    """Get observation date string from the .vix file.

    Args:
        expname: Experiment name
        date_format: strftime format (default '%Y_%B' -> e.g. '2024_february')

    Returns:
        str: Formatted observation date (lowercased)
    """
    return get_obsdatetime(expname).strftime(date_format).lower()


# ---------------------------------------------------------------------------
# Structured metadata dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScanInfo:
    """Information about a single scan from the VEX file."""
    name: str
    scanno: str
    start_time: Optional[datetime]
    stop_time: Optional[datetime]
    source: str
    has_grab: bool
    stations: list[str] = field(default_factory=list)
    # For grab scans: station code → filename expected on fringetest
    grab_files: dict[str, str] = field(default_factory=dict)
    # For grab scans: station code → expected file size in bytes
    expected_bytes: dict[str, int] = field(default_factory=dict)
    # For grab scans: station code → grabbed window duration in seconds
    grabbed_secs: dict[str, float] = field(default_factory=dict)


@dataclass
class ExperimentInfo:
    """All relevant experiment metadata extracted from the VEX file."""
    project_code: str
    start_time: Optional[datetime]
    stop_time: Optional[datetime]
    setup: str
    antennas: list[str]
    scans: list[ScanInfo]
    freq_ghz: float = 0.0
    n_channels: int = 0
    bandwidth_mhz: float = 0.0
    datarate_mbps: int = 0


# ---------------------------------------------------------------------------
# Internal VEX parsing helpers
# ---------------------------------------------------------------------------

def _parse_vex_time(time_str: str) -> Optional[datetime]:
    """Convert VEX time format (e.g. '2024y056d13h30m00s') to a datetime object."""
    time_str = time_str.strip()
    match = re.match(r'(\d+)y(\d+)d(\d+)h(\d+)m(\d+)s?', time_str)
    if match:
        year, doy, hour, minute, sec = [int(x) for x in match.groups()]
        dt = datetime.strptime(f"{year} {doy}", "%Y %j")
        return dt.replace(hour=hour, minute=minute, second=sec)
    return None


def _parse_vex_duration(dur_str: str) -> float:
    """Convert VEX duration string to seconds."""
    dur_str = dur_str.strip().replace('sec', '').strip()
    try:
        return float(dur_str)
    except ValueError:
        return 0.0


def _get_scan_stations(scan: Scan) -> list[str]:
    """Extract station names from a scan definition."""
    stations = []
    for key, entry in scan.entries.items():
        if key.startswith("comment"):
            continue
        entries = entry if isinstance(entry, list) else [entry]
        for e in entries:
            if not isinstance(e, Entry):
                continue
            if e.key == "station" and e.type == EntryType.parameter:
                v = e.value if isinstance(e.value, list) else [e.value]
                if v:
                    stations.append(v[0].strip())
    return stations


def _extract_grab_info(scan: Scan) -> tuple[dict[str, str], dict[str, float], dict[str, float]]:
    """Extract data_transfer filenames, grabbed window durations, and per-station data rates."""
    grab_files: dict[str, str] = {}
    transfer_start: dict[str, float] = {}
    transfer_stop: dict[str, float] = {}

    for key, entry in scan.entries.items():
        entries = entry if isinstance(entry, list) else [entry]
        for e in entries:
            if not isinstance(e, Entry) or e.key != "data_transfer":
                continue
            v = e.value if isinstance(e.value, list) else [e.value]
            if len(v) >= 3:
                station = v[0].strip()
                grab_files[station] = v[2].strip().rstrip(';')
            if len(v) >= 5:
                try:
                    transfer_start[station] = _parse_vex_duration(v[3])
                    transfer_stop[station] = _parse_vex_duration(v[4])
                except Exception:
                    pass

    if not grab_files:
        return {}, {}, {}

    grabbed_secs: dict[str, float] = {
        st: max(0.0, transfer_stop.get(st, 0.0) - transfer_start.get(st, 0.0))
        for st in grab_files
    }

    rate_bytes_per_sec: dict[str, float] = {}
    for key, entry in scan.entries.items():
        entries = entry if isinstance(entry, list) else [entry]
        for e in entries:
            if not isinstance(e, Entry) or e.key != "station" or e.type != EntryType.parameter:
                continue
            v = e.value if isinstance(e.value, list) else [e.value]
            if len(v) < 4:
                continue
            station = v[0].strip()
            if station not in grab_files:
                continue
            try:
                data_stop_sec = _parse_vex_duration(v[2])
                total_gb = float(v[3].strip().replace('GB', '').strip())
            except (ValueError, IndexError):
                continue
            if data_stop_sec > 0 and total_gb > 0:
                rate_bytes_per_sec[station] = total_gb * 1e9 / data_stop_sec

    return grab_files, grabbed_secs, rate_bytes_per_sec


def _scan_has_grab(scan: Scan) -> bool:
    """Determine if a scan has a data_transfer (grab) entry."""
    for key, entry in scan.entries.items():
        if key == "data_transfer":
            return True
        if isinstance(entry, list):
            for e in entry:
                if hasattr(e, 'key') and e.key == "data_transfer":
                    return True
        elif isinstance(entry, Entry):
            if entry.key == "data_transfer":
                return True
    return False


def _extract_scan_times(scan: Scan) -> tuple[Optional[datetime], Optional[datetime]]:
    """Extract start and stop times from a scan."""
    start_dt: Optional[datetime] = None
    stop_dt: Optional[datetime] = None

    for key, entry in scan.entries.items():
        if key == "start" and isinstance(entry, Entry):
            val = entry.value if isinstance(entry.value, str) else (
                entry.value[0] if isinstance(entry.value, list) else str(entry.value)
            )
            start_dt = _parse_vex_time(val.strip())

    if start_dt is not None:
        for key, entry in scan.entries.items():
            entries = entry if isinstance(entry, list) else [entry]
            for e in entries:
                if not isinstance(e, Entry):
                    continue
                if e.key == "station" and e.type == EntryType.parameter:
                    v = e.value if isinstance(e.value, list) else [e.value]
                    if len(v) >= 3:
                        dur = _parse_vex_duration(v[2])
                        if dur > 0:
                            stop_dt = start_dt + timedelta(seconds=dur)
                            break
            if stop_dt is not None:
                break

    return start_dt, stop_dt


def _extract_source(scan: Scan) -> str:
    """Extract source name from a scan."""
    for key, entry in scan.entries.items():
        if key == "start" and isinstance(entry, Entry):
            val = entry.value if isinstance(entry.value, str) else str(entry.value)
            m = re.search(r'source\s*=\s*(\S+)', val)
            if m:
                return m.group(1).rstrip(';').strip()
        if key == "source" and isinstance(entry, Entry):
            val = entry.value
            if isinstance(val, list):
                for v in val:
                    v = v.strip().rstrip(';')
                    if not re.match(r'^\d', v) and v:
                        return v
                return val[-1].strip().rstrip(';')
            return str(val).strip().rstrip(';')
    return "Unknown"


def _extract_setup_info(vex: Vex) -> tuple[str, float, int, float, int]:
    """Extract setup information from the VEX FREQ section."""
    setup_str = "Unknown setup"
    freq_ghz = 0.0
    n_bbc = 0
    bw_mhz = 0.0
    datarate = 0

    if "FREQ" not in vex:
        return setup_str, freq_ghz, 0, bw_mhz, datarate

    freq_section = vex["FREQ"]
    freqs = []
    bandwidths = []

    for def_name in freq_section.keys():
        if def_name.startswith("comment"):
            continue
        freq_def = freq_section[def_name]
        for entry_key, entry in freq_def.entries.items():
            entries = entry if isinstance(entry, list) else [entry]
            for e in entries:
                if not hasattr(e, 'key'):
                    continue
                if e.key == "chan_def":
                    val = e.value if isinstance(e.value, list) else [e.value]
                    for v in val:
                        v = str(v).strip()
                        freq_match = re.search(r'(\d+\.?\d*)\s*MHz', v)
                        if freq_match:
                            f = float(freq_match.group(1))
                            if f > 100:
                                freqs.append(f)
                            else:
                                bandwidths.append(f)
        break  # Only need the first FREQ definition

    if freqs:
        freq_ghz = (max(freqs) + min(freqs)) / 2000.0
    n_bbc = max(len(freqs) // 2, 1)
    if bandwidths:
        bw_mhz = bandwidths[0]
    datarate = int(n_bbc * bw_mhz * 4)
    setup_str = f"{datarate} Mbps, {n_bbc} BBC channels, {int(bw_mhz)} MHz filters, 2-bit sampling"
    return setup_str, freq_ghz, n_bbc, bw_mhz, datarate


def _extract_antennas(vex: Vex) -> list[str]:
    """Extract antenna/station names from the STATION section."""
    antennas = []
    if "STATION" in vex:
        for def_name in vex["STATION"].keys():
            if not def_name.startswith("comment"):
                antennas.append(def_name.capitalize())
    elif "SITE" in vex:
        site_section = vex["SITE"]
        for def_name in site_section.keys():
            if def_name.startswith("comment"):
                continue
            site_def = site_section[def_name]
            if "site_name" in site_def:
                entry = site_def["site_name"]
                val = entry.value if isinstance(entry.value, str) else entry.value[0]
                antennas.append(val.strip().rstrip(';').capitalize())
    return antennas


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_experiment_info(vexfile: str | Path) -> ExperimentInfo:
    """Load and parse experiment information from a VEX file.

    Args:
        vexfile: Path to the .vex/.vax file

    Returns:
        ExperimentInfo dataclass with all extracted metadata
    """
    vexfile = Path(vexfile)
    if not vexfile.exists():
        raise FileNotFoundError(f"VEX file not found: {vexfile}")

    vex = Vex(vexfile.stem, str(vexfile))
    project_code = vexfile.stem.upper()

    setup_str, freq_ghz, n_channels, bw_mhz, datarate = _extract_setup_info(vex)
    antennas = _extract_antennas(vex)

    scans: list[ScanInfo] = []
    overall_start: Optional[datetime] = None
    overall_stop: Optional[datetime] = None

    if "SCHED" in vex:
        sched_section = vex["SCHED"]
        for scan_name in sched_section.keys():
            if scan_name.startswith("comment"):
                continue
            scan_def = sched_section[scan_name]
            if not isinstance(scan_def, Scan):
                continue

            start_time, stop_time = _extract_scan_times(scan_def)
            source = _extract_source(scan_def)
            has_grab = _scan_has_grab(scan_def)
            stations = _get_scan_stations(scan_def)
            grab_files, grabbed_secs, rate_bps = (
                _extract_grab_info(scan_def) if has_grab else ({}, {}, {})
            )

            scanno_match = re.search(r'(\d+)', scan_name)
            scanno = scanno_match.group(1).lstrip('0') or '1' if scanno_match else scan_name
            scanno = scanno.zfill(2)

            scans.append(ScanInfo(
                name=scan_name, scanno=scanno, start_time=start_time,
                stop_time=stop_time, source=source, has_grab=has_grab,
                stations=stations, grab_files=grab_files,
                grabbed_secs=grabbed_secs,
            ))

            if overall_start is None and start_time is not None:
                overall_start = start_time
            if stop_time is not None:
                overall_stop = stop_time
            elif start_time is not None:
                overall_stop = start_time

    reference_rate: dict[str, float] = {}
    for scan in scans:
        if not scan.has_grab or not scan.grab_files:
            continue
        if not reference_rate:
            sched_section2 = vex["SCHED"]
            scan_def2 = sched_section2[scan.name]
            _, _, reference_rate = _extract_grab_info(scan_def2)
        scan.expected_bytes = {
            station: int(reference_rate[station] * scan.grabbed_secs.get(station, 2.0))
            for station in scan.grab_files
            if station in reference_rate
        }

    logger.info("Loaded experiment info: %s with %d scans, %d antennas",
                project_code, len(scans), len(antennas))
    return ExperimentInfo(
        project_code=project_code, start_time=overall_start, stop_time=overall_stop,
        setup=setup_str, antennas=antennas, scans=scans,
        freq_ghz=freq_ghz, n_channels=n_channels,
        bandwidth_mhz=bw_mhz, datarate_mbps=datarate,
    )
