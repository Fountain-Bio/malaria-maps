"""Parser tiering and content-hash tests against real CDC field shapes."""

from malaria_tracker.models import CdcMalaria
from malaria_tracker.parse import derive_malaria, parse_area_statements

AFGHANISTAN = CdcMalaria(
    HasTransmission=True,
    AreaOfRisk="<ul>\r\n  <li>All areas &lt;2,500 m (&lt;8,200 ft) elevation (April&ndash;December)</li>\r\n</ul>",
    ChloroquineResistance="<ul><li>Chloroquine</li></ul>",
    Species="<ul><li><em>P. vivax</em> (primarily)</li><li><em>P. falciparum</em> (less commonly)</li></ul>",
    RecommendedProphylaxis="<ul><li>Atovaquone-proguanil, doxycycline, mefloquine, tafenoquine<sup>2</sup></li></ul>"
                           '<div class="updatedate"><em>Updated April 23, 2025</em></div>',
)

BOTSWANA = CdcMalaria(
    HasTransmission=True,
    AreaOfRisk="<ul><li>Districts of Bobirwa, Boteti, Chobe, Ngamiland</li>"
               "<li>Rare cases or sporadic foci of transmission in districts of Kgalagadi North</li>"
               "<li>No malaria transmission in Gaborone (the capital)</li></ul>",
    ChloroquineResistance="<ul><li>Chloroquine</li></ul>",
    Species="<ul><li><em>P. falciparum</em> (primarily)</li></ul>",
    RecommendedProphylaxis="<ul><li>Districts of Bobirwa ...: Atovaquone-proguanil, doxycycline, mefloquine</li>"
                           "<li>Areas with rare cases or sporadic foci of transmission: no chemoprophylaxis "
                           "recommended (insect bite precautions and mosquito avoidance only)<sup>4</sup></li></ul>",
)

SYRIA = CdcMalaria(HasTransmission=True, AreaOfRisk=None, RecommendedProphylaxis="")

GREECE = CdcMalaria(
    HasTransmission=True,
    AreaOfRisk="<ul><li>Rare, local transmission in agricultural areas (May&ndash;November)</li>"
               "<li>No malaria transmission in tourist areas</li></ul>",
    RecommendedProphylaxis="<ul><li>None</li></ul>",
)

NO_TRANSMISSION = CdcMalaria(HasTransmission=False)


def test_afghanistan_whole_country():
    d = derive_malaria(AFGHANISTAN)
    assert d.has_transmission and d.is_endemic
    assert d.whole_country_endemic
    assert d.screening_class == "whole_country"
    assert d.country_has_any_prophylaxis_area
    assert d.species == ["P. falciparum", "P. vivax"]
    assert "doxycycline" in d.prophylaxis_drugs
    assert d.chloroquine_resistant is True
    assert d.cdc_updated_date == "2025-04-23"
    a0 = d.area_statements[0]
    assert a0.scope == "all" and a0.elev_max_m == 2500 and a0.season_text == "April–December"


def test_botswana_partial_with_tiers():
    d = derive_malaria(BOTSWANA)
    assert d.is_endemic and d.screening_class == "partial"
    tiers = [a.tier for a in d.area_statements]
    assert tiers == ["prophylaxis", "sporadic", "none"]
    assert d.area_statements[2].polarity == "exclude"


def test_syria_transmission_but_not_deferrable():
    d = derive_malaria(SYRIA)
    assert d.has_transmission and not d.is_endemic
    assert d.screening_class == "none"
    assert "no chemoprophylaxis" in (d.area_summary or "").lower()


def test_greece_no_prophylaxis():
    d = derive_malaria(GREECE)
    assert d.has_transmission and not d.is_endemic
    assert d.screening_class == "none"


def test_no_transmission():
    d = derive_malaria(NO_TRANSMISSION)
    assert not d.has_transmission and not d.is_endemic
    assert d.screening_class == "none"


def test_content_hash_excludes_updated_date():
    base = AFGHANISTAN.model_copy()
    later = AFGHANISTAN.model_copy(update={
        "recommended_prophylaxis": AFGHANISTAN.recommended_prophylaxis.replace(
            "April 23, 2025", "May 1, 2025")
    })
    assert derive_malaria(base).content_hash() == derive_malaria(later).content_hash()


def test_content_hash_changes_on_real_change():
    changed = AFGHANISTAN.model_copy(update={
        "recommended_prophylaxis": "<ul><li>Atovaquone-proguanil only</li></ul>"
    })
    assert derive_malaria(AFGHANISTAN).content_hash() != derive_malaria(changed).content_hash()


# --------------------------------------------------------------------------- region_specific scope
def test_region_specific_all_areas_is_area_not_all():
    # "All areas in the states of ..." names specific states, so it is region-specific (scope
    # "area"), not country-wide; a country with drugs in prophylaxis is therefore "partial".
    brazil = CdcMalaria(
        HasTransmission=True,
        AreaOfRisk="<ul><li>All areas in the states of Acre, Amapá, and Rondônia</li></ul>",
        RecommendedProphylaxis="<ul><li>Atovaquone-proguanil, doxycycline</li></ul>",
    )
    d = derive_malaria(brazil)
    assert d.screening_class == "partial"
    assert d.area_statements[0].scope == "area"


def test_region_specific_district_and_province_alternation():
    # The "in the (district|province|...)" alternation also yields scope "area".
    for phrase in ("in the district of Foo", "in the province of Bar"):
        d = derive_malaria(CdcMalaria(
            HasTransmission=True,
            AreaOfRisk=f"<ul><li>All areas {phrase}</li></ul>",
            RecommendedProphylaxis="<ul><li>doxycycline</li></ul>",
        ))
        assert d.screening_class == "partial", phrase
        assert d.area_statements[0].scope == "area", phrase


def test_country_wide_all_areas_keeps_scope_all():
    # "All areas <2,500 m" (no named region) is genuinely country-wide.
    s = parse_area_statements("<ul><li>All areas &lt;2,500 m elevation</li></ul>")
    assert s[0].scope == "all" and s[0].elev_max_m == 2500
    d = derive_malaria(CdcMalaria(
        HasTransmission=True,
        AreaOfRisk="<ul><li>All areas &lt;2,500 m elevation</li></ul>",
        RecommendedProphylaxis="<ul><li>doxycycline</li></ul>",
    ))
    assert d.area_statements[0].scope == "all"


def test_throughout_the_country_keeps_scope_all():
    s = parse_area_statements("<ul><li>Throughout the country including the capital</li></ul>")
    assert s[0].scope == "all"
