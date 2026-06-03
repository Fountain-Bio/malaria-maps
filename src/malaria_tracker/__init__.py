"""Malaria region tracker — collect malaria endemic-region data from CDC/FDA primary sources.

The authoritative classification source is the CDC Travelers' Health feed. A place is
"malaria-endemic" for US blood-donor deferral exactly where CDC recommends antimalarial
chemoprophylaxis (FDA "Recommendations to Reduce the Risk of Transfusion-Transmitted
Malaria", final 12/2022). This package fetches that feed daily, versions it (SCD2) into
SQLite, and records a change feed.
"""

__version__ = "0.1.0"
