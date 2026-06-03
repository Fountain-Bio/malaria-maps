"""CDC client: unwrap the real XML-enveloped payload fixture."""

from pathlib import Path

from malaria_tracker.sources.cdc_travel import parse_payload

FIXTURE = Path(__file__).parent / "fixtures" / "cdc_yellowfever_raw.txt"


def test_parse_real_payload_unwraps_xml():
    dests = parse_payload(FIXTURE.read_text(encoding="utf-8"))
    assert len(dests) > 200
    by_name = {d.name: d for d in dests}
    assert "Afghanistan" in by_name
    afg = by_name["Afghanistan"]
    assert afg.malaria.has_transmission is True
    assert "2,500" in (afg.malaria.area_of_risk or "")


def test_direct_json_fallback():
    dests = parse_payload('[{"DestinationId":1,"Name":"Testland","FriendlyName":"testland",'
                          '"Malaria":{"HasTransmission":false}}]')
    assert dests[0].name == "Testland"
    assert dests[0].malaria.has_transmission is False
