"""
VOEvent ingester — parses CHIME (and compatible) VOEvent XML and writes
detection records to the Activity database.

This module is the integration point called by the external VOEvent-to-Slack
handler.  The public API is :func:`ingest`; :func:`parse_voevent` is exposed
separately so callers can inspect extracted fields without touching the DB.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .activity import Activity

log = logging.getLogger(__name__)

_VOE_NS = "http://www.ivoa.net/xml/VOEvent/v2.0"

# Datetime formats tried in order when fromisoformat() is not applicable.
_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class VOEventParseError(Exception):
    """Raised when a required field cannot be extracted from the VOEvent XML."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest(voevent_xml: str, activity: Activity) -> None:
    """Parse a VOEvent XML string, extract detection info, and write to Activity.

    This is the integration point called by the external VOEvent handler.
    If :meth:`Activity.add_detection` raises, the error is logged but NOT
    re-raised (fail-safe: the Slack post must not be blocked by a DB write
    error).

    Raises
    ------
    VOEventParseError
        If the XML cannot be parsed or required fields are missing.
    """
    canonical_name, utc_datetime, facility = parse_voevent(voevent_xml)
    try:
        activity.add_detection(canonical_name, utc_datetime, facility)
    except Exception:
        log.exception(
            "Failed to write detection for %s at %s to the activity database; "
            "continuing without re-raising so the Slack post is not blocked.",
            canonical_name,
            utc_datetime.isoformat(),
        )


def parse_voevent(voevent_xml: str) -> tuple[str, datetime, str]:
    """Parse a VOEvent XML string.

    Returns
    -------
    tuple[str, datetime, str]
        ``(canonical_name, utc_datetime, facility)`` where *utc_datetime* is
        always timezone-aware (UTC).

    Raises
    ------
    VOEventParseError
        On malformed XML or missing required fields.
    """
    try:
        root = ET.fromstring(voevent_xml)
    except ET.ParseError as exc:
        raise VOEventParseError(f"Failed to parse VOEvent XML: {exc}") from exc

    canonical_name = _extract_canonical_name(root)
    utc_datetime = _extract_utc_datetime(root)
    facility = _extract_facility(root)

    return canonical_name, utc_datetime, facility


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------


def _extract_canonical_name(root: ET.Element) -> str:
    """Extract the FRB canonical name.

    Priority:
    1. ``<Param name="tns_name" value="..."/>`` inside ``<What>``.
    2. The segment after the last ``/`` in the IVORN attribute fragment
       (i.e. the part after ``#``).

    Raises :exc:`VOEventParseError` if neither source yields a value.
    """
    # 1. Try <What><Param name="tns_name" .../> (no namespace on <Param>)
    what = root.find(f"{{{_VOE_NS}}}What")
    if what is not None:
        for param in what.iter("Param"):
            if param.get("name") == "tns_name":
                value = param.get("value", "").strip()
                if value:
                    log.debug("canonical_name from tns_name Param: %s", value)
                    return value

    # Also handle <What> with no namespace (defensive)
    what_no_ns = root.find("What")
    if what_no_ns is not None:
        for param in what_no_ns.iter("Param"):
            if param.get("name") == "tns_name":
                value = param.get("value", "").strip()
                if value:
                    log.debug("canonical_name from tns_name Param (no-ns): %s", value)
                    return value

    # 2. Fall back to the IVORN attribute, whose form is
    #    ivo://ca.chimenet.frb/FRB-DETECTION#<timestamp>/FRB20201124A
    #    The source name only ever appears in the fragment after '#'.  Without
    #    one, the last path segment is the packet type ('FRB-DETECTION'), not a
    #    source — taking it would file detections against a source that does
    #    not exist.
    ivorn = root.get("ivorn", "")
    if "#" in ivorn:
        name = ivorn.split("#", 1)[1].rsplit("/", 1)[-1].strip()
        if name:
            log.debug("canonical_name from IVORN %r: %s", ivorn, name)
            return name

    log.warning("Could not determine canonical_name from VOEvent (ivorn=%r)", ivorn)
    raise VOEventParseError(
        "Could not extract canonical_name: no tns_name Param found and the "
        f"IVORN has no source name after '#' (ivorn={ivorn!r})."
    )


def _extract_utc_datetime(root: ET.Element) -> datetime:
    """Extract the detection UTC datetime.

    Priority:
    1. ``<Param name="timestamp_utc" value="..."/>`` inside ``<What>``.
    2. ``<ISOTime>`` inside ``<WhereWhen>``.
    3. ``<Date>`` inside ``<Who>``.

    All values are returned as UTC-aware :class:`datetime` objects.

    Raises :exc:`VOEventParseError` if none of the sources yields a value.
    """
    # 1. timestamp_utc Param in <What>
    for what in _find_elements(root, "What"):
        for param in what.iter("Param"):
            if param.get("name") == "timestamp_utc":
                raw = param.get("value", "").strip()
                if raw:
                    dt = _parse_datetime_str(raw, "timestamp_utc Param")
                    log.debug("utc_datetime from timestamp_utc Param: %s", dt)
                    return dt

    # 2. <ISOTime> in <WhereWhen>
    for where_when in _find_elements(root, "WhereWhen"):
        iso_time = where_when.find(".//{*}ISOTime")
        if iso_time is None:
            iso_time = where_when.find(".//ISOTime")
        if iso_time is not None and iso_time.text:
            raw = iso_time.text.strip()
            if raw:
                dt = _parse_datetime_str(raw, "ISOTime")
                log.debug("utc_datetime from ISOTime: %s", dt)
                return dt

    # 3. <Date> in <Who>
    for who in _find_elements(root, "Who"):
        date_el = who.find(f"{{{_VOE_NS}}}Date")
        if date_el is None:
            date_el = who.find("Date")
        if date_el is not None and date_el.text:
            raw = date_el.text.strip()
            if raw:
                dt = _parse_datetime_str(raw, "Who/Date")
                log.debug("utc_datetime from Who/Date: %s", dt)
                return dt

    raise VOEventParseError(
        "Could not extract utc_datetime: no timestamp_utc Param, ISOTime, "
        "or Who/Date element found in the VOEvent."
    )


def _extract_facility(root: ET.Element) -> str:
    """Extract the observatory/facility name.

    Looks for ``<ObservatoryLocation id="..."/>`` inside ``<WhereWhen>``.
    Defaults to ``"CHIME"`` if not found.
    """
    for where_when in _find_elements(root, "WhereWhen"):
        obs = where_when.find(".//{*}ObservatoryLocation")
        if obs is None:
            obs = where_when.find(".//ObservatoryLocation")
        if obs is not None:
            facility = obs.get("id", "").strip()
            if facility:
                log.debug("facility from ObservatoryLocation id: %s", facility)
                return facility

    log.debug("ObservatoryLocation id not found; defaulting facility to CHIME")
    return "CHIME"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _find_elements(root: ET.Element, local_name: str):
    """Yield matching child elements by local name, trying both the VOEvent
    namespace and no namespace (defensive fallback)."""
    el = root.find(f"{{{_VOE_NS}}}{local_name}")
    if el is not None:
        yield el
        return
    el = root.find(local_name)
    if el is not None:
        yield el


def _parse_datetime_str(raw: str, source: str) -> datetime:
    """Parse *raw* as a UTC-aware datetime.

    Tries :func:`datetime.fromisoformat` first (after normalising trailing
    ``Z``), then falls back to strptime with common CHIME formats.

    Raises :exc:`VOEventParseError` if all attempts fail.
    """
    # Normalise: replace trailing Z with +00:00 so fromisoformat handles it.
    normalised = raw.rstrip("Z")
    if raw.endswith("Z"):
        normalised = normalised + "+00:00"

    try:
        dt = datetime.fromisoformat(normalised)
        return _ensure_utc(dt)
    except ValueError:
        pass

    # Fallback strptime attempts (naive → assume UTC).
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(raw.rstrip("Z"), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    raise VOEventParseError(
        f"Could not parse datetime string {raw!r} from {source}."
    )


def _ensure_utc(dt: datetime) -> datetime:
    """Return *dt* as a UTC-aware datetime.

    If *dt* is naive, treat it as UTC.  If it is already tz-aware, convert to
    UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
