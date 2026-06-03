"""Pydantic models: raw CDC payload + the normalized derived classification.

The derived model carries `canonical_for_hash()`, the stable serialization used as the
SCD2 content hash input. It deliberately excludes the CDC "Updated" date, so a date-only
republish with identical substance does not create a spurious version.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- raw CDC
class CdcMalaria(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    has_transmission: bool = Field(alias="HasTransmission", default=False)
    area_of_risk: str | None = Field(alias="AreaOfRisk", default=None)
    relative_risk: str | None = Field(alias="RelativeRisk", default=None)
    chloroquine_resistance: str | None = Field(alias="ChloroquineResistance", default=None)
    species: str | None = Field(alias="Species", default=None)
    recommended_prophylaxis: str | None = Field(alias="RecommendedProphylaxis", default=None)


class CdcYellowFever(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    has_requirements: bool | None = Field(alias="HasRequirements", default=None)
    requirements: str | None = Field(alias="Requirements", default=None)
    has_recommendations: bool | None = Field(alias="HasRecommendations", default=None)
    recommendations: str | None = Field(alias="Recommendations", default=None)


class CdcDestination(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    destination_id: int = Field(alias="DestinationId")
    name: str = Field(alias="Name")
    long_name: str | None = Field(alias="LongName", default=None)
    friendly_name: str = Field(alias="FriendlyName")
    malaria: CdcMalaria = Field(alias="Malaria", default_factory=CdcMalaria)
    yellow_fever: CdcYellowFever = Field(alias="YellowFever", default_factory=CdcYellowFever)
    other_vaccines: str | None = Field(alias="OtherVaccinesToConsider", default=None)
    map_links: str | None = Field(alias="MapLinks", default=None)
    map_html: str | None = Field(alias="MapHtml", default=None)


# --------------------------------------------------------------------------- derived
class AreaStatement(BaseModel):
    seq: int
    raw_text: str
    polarity: str          # 'include' | 'exclude'
    tier: str              # 'prophylaxis' | 'sporadic' | 'none'
    scope: str | None = None       # 'all' | 'area'
    place_name: str | None = None
    elev_max_m: int | None = None
    elev_min_m: int | None = None
    season_text: str | None = None


class MalariaDerived(BaseModel):
    has_transmission: bool
    is_endemic: bool                         # = country_has_any_prophylaxis_area (FDA deferral bit)
    whole_country_endemic: bool
    country_has_any_prophylaxis_area: bool
    screening_class: str                     # 'whole_country' | 'partial' | 'none'
    prophylaxis_drugs: list[str]
    species: list[str]
    chloroquine_resistant: bool | None
    area_summary: str | None
    cdc_updated_date: str | None             # excluded from hash
    area_statements: list[AreaStatement]

    def canonical_for_hash(self) -> str:
        payload = {
            "has_transmission": self.has_transmission,
            "is_endemic": self.is_endemic,
            "whole_country_endemic": self.whole_country_endemic,
            "country_has_any_prophylaxis_area": self.country_has_any_prophylaxis_area,
            "screening_class": self.screening_class,
            "prophylaxis_drugs": sorted(self.prophylaxis_drugs),
            "species": sorted(self.species),
            "chloroquine_resistant": self.chloroquine_resistant,
            "areas": [
                {
                    "polarity": a.polarity, "tier": a.tier, "scope": a.scope,
                    "place_name": a.place_name, "elev_max_m": a.elev_max_m,
                    "elev_min_m": a.elev_min_m, "season_text": a.season_text,
                    "raw_text": " ".join(a.raw_text.split()),
                }
                for a in self.area_statements
            ],
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_for_hash().encode("utf-8")).hexdigest()


class YellowFeverDerived(BaseModel):
    has_requirements: bool | None
    has_recommendations: bool | None

    def content_hash(self, requirements_html: str | None, recommendations_html: str | None) -> str:
        payload = json.dumps(
            {"req": (requirements_html or "").strip(), "rec": (recommendations_html or "").strip()},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
