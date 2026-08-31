"""Tests for precise_scheduler/voevent_ingester.py."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from precise_scheduler.voevent_ingester import (
    VOEventParseError,
    ingest,
    parse_voevent,
)

# ---------------------------------------------------------------------------
# VOEvent XML fixtures
# ---------------------------------------------------------------------------

_CHIME_VOEVENT = """\
<?xml version='1.0' encoding='UTF-8'?>
<voe:VOEvent xmlns:voe="http://www.ivoa.net/xml/VOEvent/v2.0"
             ivorn="ivo://ca.chimenet.frb/FRB-DETECTION#2024-01-15T10:30:00.000000Z/FRB20201124A"
             role="observation"
             version="2.0">
  <Who>
    <AuthorIVORN>ivo://ca.chimenet.frb</AuthorIVORN>
    <Date>2024-01-15T10:30:00</Date>
  </Who>
  <What>
    <Param name="tns_name" value="FRB20201124A" utype="chimefrbcat1:tns_name"/>
    <Param name="event_no" value="123456"/>
    <Param name="bonsai_dm" value="413.52"/>
    <Param name="timestamp_utc" value="2024-01-15T10:30:00.000000"/>
  </What>
  <WhereWhen>
    <ObsDataLocation>
      <ObservatoryLocation id="CHIME"/>
      <ObservationLocation>
        <AstroCoordSystem id="UTC-FK5-GEO"/>
        <AstroCoords coord_system_id="UTC-FK5-GEO">
          <Time unit="s">
            <TimeInstant>
              <ISOTime>2024-01-15T10:30:00.000000</ISOTime>
            </TimeInstant>
          </Time>
        </AstroCoords>
      </ObservationLocation>
    </ObsDataLocation>
  </WhereWhen>
</voe:VOEvent>
"""

_VOEVENT_NO_TNS_NAME = """\
<?xml version='1.0' encoding='UTF-8'?>
<voe:VOEvent xmlns:voe="http://www.ivoa.net/xml/VOEvent/v2.0"
             ivorn="ivo://ca.chimenet.frb/FRB-DETECTION#2024-01-15T10:30:00.000000Z/FRB20220912A"
             role="observation"
             version="2.0">
  <Who>
    <AuthorIVORN>ivo://ca.chimenet.frb</AuthorIVORN>
    <Date>2024-01-15T10:30:00</Date>
  </Who>
  <What>
    <Param name="event_no" value="789"/>
    <Param name="timestamp_utc" value="2024-01-15T10:30:00.000000"/>
  </What>
  <WhereWhen>
    <ObsDataLocation>
      <ObservatoryLocation id="CHIME"/>
      <ObservationLocation>
        <AstroCoordSystem id="UTC-FK5-GEO"/>
        <AstroCoords coord_system_id="UTC-FK5-GEO">
          <Time unit="s">
            <TimeInstant>
              <ISOTime>2024-01-15T10:30:00.000000</ISOTime>
            </TimeInstant>
          </Time>
        </AstroCoords>
      </ObservationLocation>
    </ObsDataLocation>
  </WhereWhen>
</voe:VOEvent>
"""

_VOEVENT_NO_TIMESTAMP_UTC = """\
<?xml version='1.0' encoding='UTF-8'?>
<voe:VOEvent xmlns:voe="http://www.ivoa.net/xml/VOEvent/v2.0"
             ivorn="ivo://ca.chimenet.frb/FRB-DETECTION#2024-06-01T08:00:00.000000Z/FRB20201124A"
             role="observation"
             version="2.0">
  <Who>
    <AuthorIVORN>ivo://ca.chimenet.frb</AuthorIVORN>
    <Date>2024-06-01T08:00:00</Date>
  </Who>
  <What>
    <Param name="tns_name" value="FRB20201124A"/>
    <Param name="event_no" value="42"/>
  </What>
  <WhereWhen>
    <ObsDataLocation>
      <ObservatoryLocation id="CHIME"/>
      <ObservationLocation>
        <AstroCoordSystem id="UTC-FK5-GEO"/>
        <AstroCoords coord_system_id="UTC-FK5-GEO">
          <Time unit="s">
            <TimeInstant>
              <ISOTime>2024-06-01T08:00:00.000000</ISOTime>
            </TimeInstant>
          </Time>
        </AstroCoords>
      </ObservationLocation>
    </ObsDataLocation>
  </WhereWhen>
</voe:VOEvent>
"""

_VOEVENT_NO_OBSERVATORY_LOCATION = """\
<?xml version='1.0' encoding='UTF-8'?>
<voe:VOEvent xmlns:voe="http://www.ivoa.net/xml/VOEvent/v2.0"
             ivorn="ivo://ca.chimenet.frb/FRB-DETECTION#2024-03-10T12:00:00Z/FRB20210912F"
             role="observation"
             version="2.0">
  <Who>
    <AuthorIVORN>ivo://ca.chimenet.frb</AuthorIVORN>
    <Date>2024-03-10T12:00:00</Date>
  </Who>
  <What>
    <Param name="tns_name" value="FRB20210912F"/>
    <Param name="timestamp_utc" value="2024-03-10T12:00:00.000000"/>
  </What>
  <WhereWhen>
    <ObsDataLocation>
      <ObservationLocation>
        <AstroCoordSystem id="UTC-FK5-GEO"/>
        <AstroCoords coord_system_id="UTC-FK5-GEO">
          <Time unit="s">
            <TimeInstant>
              <ISOTime>2024-03-10T12:00:00.000000</ISOTime>
            </TimeInstant>
          </Time>
        </AstroCoords>
      </ObservationLocation>
    </ObsDataLocation>
  </WhereWhen>
</voe:VOEvent>
"""

_VOEVENT_NO_CANONICAL_NAME = """\
<?xml version='1.0' encoding='UTF-8'?>
<voe:VOEvent xmlns:voe="http://www.ivoa.net/xml/VOEvent/v2.0"
             ivorn="ivo://ca.chimenet.frb/FRB-DETECTION"
             role="observation"
             version="2.0">
  <Who>
    <AuthorIVORN>ivo://ca.chimenet.frb</AuthorIVORN>
    <Date>2024-01-15T10:30:00</Date>
  </Who>
  <What>
    <Param name="event_no" value="99"/>
    <Param name="timestamp_utc" value="2024-01-15T10:30:00.000000"/>
  </What>
  <WhereWhen>
    <ObsDataLocation>
      <ObservatoryLocation id="CHIME"/>
    </ObsDataLocation>
  </WhereWhen>
</voe:VOEvent>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_activity() -> MagicMock:
    """Return a MagicMock that mimics the Activity interface."""
    activity = MagicMock()
    activity.add_detection = MagicMock()
    return activity


# ---------------------------------------------------------------------------
# ingest() — integration / fail-safe behaviour
# ---------------------------------------------------------------------------


class TestIngest:
    def test_ingest_writes_to_activity(self) -> None:
        """Full CHIME VOEvent: add_detection called with correct args."""
        activity = _mock_activity()
        ingest(_CHIME_VOEVENT, activity)

        activity.add_detection.assert_called_once()
        call_args = activity.add_detection.call_args
        name, dt, facility = call_args.args

        assert name == "FRB20201124A"
        assert dt == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert facility == "CHIME"

    def test_ingest_activity_error_does_not_raise(self) -> None:
        """If add_detection() raises, ingest() must not propagate the error."""
        activity = _mock_activity()
        activity.add_detection.side_effect = RuntimeError("DB is locked")

        # Must not raise even though the Activity call fails.
        ingest(_CHIME_VOEVENT, activity)

    def test_ingest_bad_xml_raises_voevent_parse_error(self) -> None:
        """Malformed XML must raise VOEventParseError before touching Activity."""
        activity = _mock_activity()

        with pytest.raises(VOEventParseError, match="Failed to parse VOEvent XML"):
            ingest("<not valid xml <<< >>>", activity)

        activity.add_detection.assert_not_called()

    def test_ingest_missing_canonical_name_raises(self) -> None:
        """No tns_name Param and IVORN yields no usable name → VOEventParseError."""
        activity = _mock_activity()

        with pytest.raises(VOEventParseError, match="canonical_name"):
            ingest(_VOEVENT_NO_CANONICAL_NAME, activity)

        activity.add_detection.assert_not_called()


# ---------------------------------------------------------------------------
# parse_voevent() — field extraction
# ---------------------------------------------------------------------------


class TestParseVoeventCanonicalName:
    def test_parse_voevent_extracts_tns_name(self) -> None:
        """tns_name Param is present → canonical_name taken from it."""
        name, _, _ = parse_voevent(_CHIME_VOEVENT)
        assert name == "FRB20201124A"

    def test_parse_voevent_falls_back_to_ivorn(self) -> None:
        """No tns_name Param → canonical_name extracted from IVORN."""
        name, _, _ = parse_voevent(_VOEVENT_NO_TNS_NAME)
        assert name == "FRB20220912A"


class TestParseVoeventDatetime:
    def test_parse_voevent_extracts_timestamp_utc(self) -> None:
        """timestamp_utc Param present → datetime parsed from it."""
        _, dt, _ = parse_voevent(_CHIME_VOEVENT)
        assert dt == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_parse_voevent_falls_back_to_isotime(self) -> None:
        """No timestamp_utc Param → ISOTime element used instead."""
        _, dt, _ = parse_voevent(_VOEVENT_NO_TIMESTAMP_UTC)
        assert dt == datetime(2024, 6, 1, 8, 0, 0, tzinfo=timezone.utc)

    def test_datetime_is_utc_aware(self) -> None:
        """Returned datetime must always carry tzinfo=timezone.utc."""
        _, dt, _ = parse_voevent(_CHIME_VOEVENT)
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc


class TestParseVoeventFacility:
    def test_parse_voevent_facility_from_observatorylocation(self) -> None:
        """Facility taken from ObservatoryLocation id attribute."""
        _, _, facility = parse_voevent(_CHIME_VOEVENT)
        assert facility == "CHIME"

    def test_parse_voevent_facility_defaults_to_chime(self) -> None:
        """When ObservatoryLocation is absent, facility defaults to 'CHIME'."""
        _, _, facility = parse_voevent(_VOEVENT_NO_OBSERVATORY_LOCATION)
        assert facility == "CHIME"
