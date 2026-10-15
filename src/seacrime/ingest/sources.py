"""Registry of real data sources and their download definitions.

See docs/DATA_SOURCES.md for the human-readable catalogue (coverage, delays,
licensing, leakage risks).
"""

from __future__ import annotations

from .socrata import SocrataQuery

# SPD Crime Data: 2008-Present (NIBRS offenses; one row per offense)
SPD_CRIME = SocrataQuery(
    dataset_id="tazs-3rd5",
    select=(
        "report_number,offense_id,report_date_time,offense_date,nibrs_group_a_b,"
        "nibrs_crime_against_category,offense_sub_category,shooting_type_group,"
        "block_address,latitude,longitude,beat,precinct,sector,neighborhood,reporting_area,"
        "offense_category,nibrs_offense_code_description,nibrs_offense_code,census_block_2020"
    ),
    order=":id",
    page_size=200_000,
)

# SPD Call Data (CAD calls for service). One row per dispatched call sign, so
# events repeat; we keep the event-level columns and de-duplicate locally.
SPD_CALLS = SocrataQuery(
    dataset_id="33kz-ixgy",
    select=(
        "cad_event_number,cad_event_original_time_queued,initial_call_type,final_call_type,"
        "call_type,priority,call_type_indicator,event_group,dispatch_latitude,dispatch_longitude,"
        "dispatch_beat,dispatch_neighborhood"
    ),
    where="cad_event_original_time_queued >= '2019-06-01T00:00:00'",
    order=":id",
    page_size=200_000,
)

# Seattle Real Time Fire 911 Calls (updated every ~5 minutes) — the only truly
# near-real-time public signal available at forecast time.
FIRE_911 = SocrataQuery(
    dataset_id="kzjm-xkqj",
    select="incident_number,datetime,type,latitude,longitude,address",
    where="datetime >= '2019-06-01T00:00:00'",
    order=":id",
    page_size=200_000,
)

SOCRATA_SOURCES: dict[str, SocrataQuery] = {
    "spd_crime": SPD_CRIME,
    "spd_calls": SPD_CALLS,
    "fire_911": FIRE_911,
}
