"""Offline tests for the contextual ingesters (weather, events, GTFS, news).

Everything here runs on small inline fixtures — no network access.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from seacrime.ingest import events as ev
from seacrime.ingest import gtfs, news
from seacrime.ingest import weather as wx
from seacrime.ingest._context_common import local_to_utc

TZ = "America/Los_Angeles"
UTC_ = "UTC"


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=UTC_)


# --------------------------------------------------------------------------- timezone handling


def test_local_to_utc_regular_dst_gap_and_ambiguous_hour():
    vals = ["2024-07-01 12:00", "2024-01-15 12:00", "2024-03-10 02:30", "2024-11-03 01:30"]
    out = local_to_utc(vals, TZ)
    assert out.iloc[0] == ts("2024-07-01 19:00")  # PDT = UTC-7
    assert out.iloc[1] == ts("2024-01-15 20:00")  # PST = UTC-8
    assert out.iloc[2] == ts("2024-03-10 10:00")  # nonexistent 02:30 -> shifted to 03:00 PDT
    assert out.iloc[3] == ts("2024-11-03 08:30")  # ambiguous -> earliest (PDT) by default
    late = local_to_utc(["2024-11-03 01:30"], TZ, ambiguous="latest")
    assert late.iloc[0] == ts("2024-11-03 09:30")
    assert str(out.dtype) == "datetime64[ns, UTC]"


def test_local_to_utc_passes_through_aware_values():
    aware = pd.Series(pd.to_datetime(["2024-07-01T12:00:00-07:00"], utc=True))
    assert local_to_utc(aware, TZ).iloc[0] == ts("2024-07-01 19:00")


# --------------------------------------------------------------------------- weather


def test_isd_field_parsers():
    assert wx.parse_isd_scaled(pd.Series(["+0078,1", "+9999,9", "+0100,3", "-0056,5"])).tolist()[0] == 7.8
    vals = wx.parse_isd_scaled(pd.Series(["+0078,1", "+9999,9", "+0100,3", "-0056,5"]))
    assert np.isnan(vals[1]) and np.isnan(vals[2]) and vals[3] == pytest.approx(-5.6)
    wind = wx.parse_isd_wind_speed(pd.Series(["360,1,N,0015,1", "999,9,C,0000,1", "999,9,9,9999,9"]))
    assert wind[0] == 1.5 and wind[1] == 0.0 and np.isnan(wind[2])
    period, depth = wx.parse_isd_precip(pd.Series(["01,0013,9,5", "06,0100,9,1", "", "01,9999,9,9"]))
    assert period.tolist()[:2] == [1.0, 6.0] and depth[0] == 1.3 and np.isnan(depth[3])


def test_isd_to_hourly_prefers_routine_metar_and_hourly_precip():
    raw = pd.DataFrame(
        {
            "DATE": ["2024-01-01T00:00:00", "2024-01-01T00:53:00", "2024-01-01T01:20:00", "2024-01-01T01:53:00", "2024-01-01T02:00:00"],
            "REPORT_TYPE": ["FM-12", "FM-15", "FM-16", "FM-15", "FM-12"],
            "TMP": ["+0090,1", "+0078,5", "+0070,5", "+0061,5", "+0050,1"],
            "DEW": ["+0039,1", "+0039,5", "+0039,5", "+0033,5", "+0030,1"],
            "WND": ["999,9,C,0000,1", "360,5,N,0015,5", "360,5,N,0020,5", "350,5,N,0021,5", "350,1,N,0030,1"],
            "AA1": [None, "01,0005,9,5", "01,0002,9,5", "01,0000,9,5", "06,0100,9,1"],
        }
    )
    h = wx.isd_to_hourly(raw).set_index("ts_utc")
    assert h.index.tz is not None and len(h) == 3
    assert h.loc[ts("2024-01-01 00:00"), "temp_c"] == 7.8  # FM-15 wins over FM-12
    assert h.loc[ts("2024-01-01 00:00"), "precip_mm"] == 0.5
    assert h.loc[ts("2024-01-01 01:00"), "precip_mm"] == 0.0  # SPECI partial accumulation ignored
    assert np.isnan(h.loc[ts("2024-01-01 02:00"), "precip_mm"])  # only a 6-h SYNOP amount
    assert h.loc[ts("2024-01-01 02:00"), "temp_c"] == 5.0  # falls back to non-routine report
    assert 0 < h.loc[ts("2024-01-01 00:00"), "rel_humidity"] <= 100


def test_ghcnh_precip_only_from_metar_and_zero_fill():
    raw = pd.DataFrame(
        {
            "DATE": ["2026-07-01T00:53:00", "2026-07-01T01:53:00", "2026-07-01T02:53:00", "2026-07-01T03:00:00"],
            "temperature": ["20.0", "19.0", "18.0", "17.5"],
            "temperature_Quality_Code": [None, "1", None, None],
            "temperature_Report_Type": ["FM15", "FM15", "FM15", "FM12"],
            "dew_point_temperature": ["10.0", "10.0", "10.0", "10.0"],
            "dew_point_temperature_Quality_Code": [None] * 4,
            "wind_speed": ["2.1", "1.5", "0.0", "1.0"],
            "wind_speed_Quality_Code": [None] * 4,
            "precipitation": [None, "1.3", "4.0", "2.0"],
            "precipitation_Quality_Code": [None] * 4,
            "precipitation_Report_Type": [None, "FM15", "FM94_1", "FM12"],
        }
    )
    h = wx.zero_fill_routine_precip(wx.ghcnh_to_hourly(raw)).set_index("ts_utc")
    assert h.loc[ts("2026-07-01 01:00"), "precip_mm"] == 1.3
    assert h.loc[ts("2026-07-01 00:00"), "precip_mm"] == 0.0 and h.loc[ts("2026-07-01 00:00"), "precip_zero_filled"]
    assert h.loc[ts("2026-07-01 02:00"), "precip_mm"] == 0.0  # FM94_1 amount rejected, routine hour -> 0
    assert np.isnan(h.loc[ts("2026-07-01 03:00"), "precip_mm"])  # no routine METAR: not zero-filled


def test_combine_stations_fills_gaps_from_secondary():
    idx = pd.date_range("2024-01-01", periods=3, freq="h", tz=UTC_)
    a = pd.DataFrame(
        {
            "ts_utc": idx,
            "temp_c": [1.0, np.nan, 3.0],
            "dewpoint_c": 0.0,
            "rel_humidity": 50.0,
            "wind_ms": 1.0,
            "precip_mm": [0.0, 0.0, np.nan],
        }
    )
    b = a.assign(temp_c=[9.0, 2.0, 9.0], precip_mm=[5.0, 5.0, 0.4])
    out = wx.combine_stations({"isd:KSEA": a, "isd:KBFI": b}, idx[0], idx[-1])
    assert out["temp_c"].tolist() == [1.0, 2.0, 3.0]
    assert out["precip_mm"].tolist() == [0.0, 0.0, 0.4]
    assert out.loc[1, "source"] == "noaa_ncei:isd:KBFI+isd:KSEA" and out.loc[0, "source"] == "noaa_ncei:isd:KSEA"
    assert out.loc[2, "precip_source"] == "isd:KBFI"


def test_parse_mos_csv_units_and_issue_time():
    text = (
        "runtime,ftime,model,tmp,dpt,cld,wdr,wsp,p06,p12,q06,typ\n"
        "2024-01-01,2024-01-01 06:00:00,GFS,32,23,OV,270,10,40,,1,R \n"
        "2024-01-01 06:00:00,2024-01-01 12:00:00,GFS,50,41,BK,180,4,,60,,R \n"
    )
    df = wx.parse_mos_csv(text, "GFS", delay_h=6.0)
    assert df.loc[0, "run_utc"] == ts("2024-01-01 00:00")  # date-only runtime == 00Z
    assert df.loc[0, "issued_at_utc"] == ts("2024-01-01 06:00")
    assert df.loc[0, "lead_h"] == 6.0 and df.loc[1, "lead_h"] == 6.0
    assert df.loc[0, "temp_c"] == 0.0 and df.loc[1, "temp_c"] == 10.0
    assert df.loc[0, "wind_ms"] == pytest.approx(5.14, abs=0.01)
    assert df.loc[0, "pop_6h_pct"] == 40 and df.loc[1, "pop_12h_pct"] == 60


def test_parse_nws_hourly_and_qpf_spread():
    hourly = {
        "properties": {
            "updateTime": "2026-10-14T07:30:42+00:00",
            "generatedAt": "2026-10-14T18:25:42+00:00",
            "periods": [
                {
                    "startTime": "2026-10-14T11:00:00-07:00",
                    "temperature": 14,
                    "temperatureUnit": "C",
                    "probabilityOfPrecipitation": {"value": 20},
                    "dewpoint": {"value": 10},
                    "relativeHumidity": {"value": 76},
                    "windSpeed": "18 km/h",
                    "windDirection": "S",
                },
                {
                    "startTime": "2026-10-14T12:00:00-07:00",
                    "temperature": 59,
                    "temperatureUnit": "F",
                    "probabilityOfPrecipitation": {"value": 0},
                    "windSpeed": "5 mph",
                },
            ],
        }
    }
    grid = {"properties": {"quantitativePrecipitation": {"values": [{"validTime": "2026-10-14T18:00:00+00:00/PT6H", "value": 6.0}]}}}
    df = wx.parse_nws_hourly(hourly, grid, datetime(2026, 10, 14, 18, 30, tzinfo=UTC))
    assert df.loc[0, "valid_ts_utc"] == ts("2026-10-14 18:00")
    assert df.loc[0, "wind_ms"] == pytest.approx(5.0) and df.loc[1, "wind_ms"] == pytest.approx(2.235, abs=1e-3)
    assert df.loc[1, "temp_c"] == pytest.approx(15.0)
    assert df["qpf_mm"].tolist() == [1.0, 1.0]
    assert df.loc[0, "issued_at_utc"] == ts("2026-10-14 07:30:42")


def test_validate_monthly_precip_compares_complete_months():
    idx = pd.date_range("2025-01-01 08:00", "2025-02-01 07:00", freq="h", tz=UTC_)  # Jan in LST
    hourly = pd.DataFrame({"ts_utc": idx, "precip_mm": 0.1})
    daily = pd.DataFrame({"DATE": pd.date_range("2025-01-01", "2025-01-31").strftime("%Y-%m-%d"), "PRCP": 2.4})
    v = wx.validate_monthly_precip(hourly, daily)
    assert v["months_compared"] == 1 and v["max_monthly_rel_diff"] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- events


def _espn_event(eid, date_s, home, away, venue, city, neutral=False, status="STATUS_FINAL", att=30000, stype=2):
    return {
        "id": eid,
        "date": date_s,
        "name": f"{away} at {home}",
        "timeValid": True,
        "season": {"year": 2024},
        "seasonType": {"type": stype, "name": "Postseason" if stype == 3 else "Regular Season"},
        "competitions": [
            {
                "attendance": att,
                "neutralSite": neutral,
                "venue": {"fullName": venue, "address": {"city": city}},
                "status": {"type": {"name": status}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": home}},
                    {"homeAway": "away", "team": {"displayName": away}},
                ],
            }
        ],
    }


def test_espn_games_keep_only_seattle_venues():
    rows = [
        ev.parse_espn_event(
            _espn_event("1", "2024-09-08T20:05Z", "Seattle Seahawks", "Denver Broncos", "Lumen Field", "Seattle"), "nfl", "nfl"
        ),
        ev.parse_espn_event(
            _espn_event("2", "2024-09-15T20:05Z", "New England Patriots", "Seattle Seahawks", "Gillette Stadium", "Foxborough"),
            "nfl",
            "nfl",
        ),
        ev.parse_espn_event(
            _espn_event("3", "2024-06-01T02:00Z", "Seattle Storm", "Las Vegas Aces", "Angel of the Winds Arena", "Everett"), "wnba", "wnba"
        ),
        ev.parse_espn_event(
            _espn_event("4", "2023-11-25T01:30Z", "Washington Huskies", "Washington State", "Lumen Field", "Seattle", neutral=True),
            "ncaaf",
            "college-football",
        ),
        ev.parse_espn_event(
            _espn_event(
                "5",
                "2026-12-26T01:15Z",
                "Seattle Seahawks",
                "LA Rams",
                "Lumen Field",
                "Seattle, Washington",
                status="STATUS_SCHEDULED",
                att=0,
                stype=3,
            ),
            "nfl",
            "nfl",
        ),
    ]
    coords = {"Lumen Field": (47.5953, -122.3316)}
    out = ev.games_to_events(rows, "espn", coords).set_index("source_id")
    assert sorted(out.index) == ["1", "4", "5"]  # away game + Everett "home" game dropped
    assert out.loc["1", "end_utc"] - out.loc["1", "start_utc"] == pd.Timedelta(hours=ev.DURATION_H["nfl"])
    assert out.loc["1", "attendance"] == 30000 and pd.isna(out.loc["5", "attendance"])  # future: no attendance
    assert out.loc["5", "status"] == "scheduled" and bool(out.loc["5", "is_postseason"])
    assert out.loc["4", "lat"] == 47.5953


def test_mlb_parse_actual_end_and_postseason():
    payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 1,
                        "gameType": "D",
                        "season": "2022",
                        "gameDate": "2022-10-15T20:07:00Z",
                        "status": {"detailedState": "Final", "startTimeTBD": False},
                        "teams": {"home": {"team": {"name": "Seattle Mariners"}}, "away": {"team": {"name": "Houston Astros"}}},
                        "venue": {"id": 680, "name": "T-Mobile Park"},
                        "gameInfo": {
                            "attendance": 47690,
                            "firstPitch": "2022-10-15T20:08:00.000Z",
                            "gameDurationMinutes": 382,
                            "delayDurationMinutes": 0,
                        },
                    }
                ]
            }
        ]
    }
    rows = ev.parse_mlb_games(payload)
    out = ev.games_to_events(rows, "mlb_statsapi", {"T-Mobile Park": (47.59, -122.33)})
    assert bool(out.loc[0, "is_postseason"]) and out.loc[0, "attendance"] == 47690
    assert out.loc[0, "actual_end_utc"] == ts("2022-10-16 02:30")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Final", "final"),
        ("STATUS_FINAL_AET", "final"),
        ("STATUS_FULL_TIME", "final"),
        ("Postponed", "postponed"),
        ("STATUS_CANCELED", "cancelled"),
        ("Withdrawn", "cancelled"),
        ("Issued", "scheduled"),
        ("STATUS_SCHEDULED", "scheduled"),
        ("In Process", "pending"),
        (None, "unknown"),
    ],
)
def test_normalize_status(raw, expected):
    assert ev.normalize_status(raw) == expected


def test_schedule_known_at_rules():
    start = pd.Series([ts("2024-09-08 20:00")] * 5)
    cat = pd.Series(["mlb", "nfl", "nhl", "permit", "permit"])
    post = pd.Series([False, False, True, False, False])
    app = pd.Series([pd.NaT, pd.NaT, pd.NaT, ts("2024-08-01"), ts("2024-09-08 18:00")])
    k = ev.schedule_known_at(start, cat, post, app)
    assert k[0] == start[0] - pd.Timedelta(days=7)
    assert k[1] == start[1] - pd.Timedelta(days=5)
    assert k[2] == start[2] - pd.Timedelta(hours=12)
    assert k[3] == start[3] - pd.Timedelta(hours=24)  # early application -> start - 24 h
    assert k[4] == ts("2024-09-08 18:00")  # late application -> application time


def test_permits_to_events_local_dates_and_filtering():
    p = pd.DataFrame(
        {
            "year_month_app": ["A1", "A2", "A3"],
            "permit_type": ["Special Event", "Valet Parking", "Special Event"],
            "name_of_event": ["Pride Parade", "Valet", "Winter Run"],
            "event_category": ["Community", None, "Athletic"],
            "event_start_date": ["2023-06-25T00:00:00.000", "2023-06-25T00:00:00.000", "2023-12-02T00:00:00.000"],
            "event_end_date": ["2023-06-25T00:00:00.000", "2023-06-25T00:00:00.000", "2023-12-03T00:00:00.000"],
            "application_date": ["2023-01-10T00:00:00.000"] * 3,
            "event_location_neighborhood": ["Downtown", "Queen Anne", "Ballard"],
            "permit_status": ["Complete", "Complete", "Cancelled"],
            "attendance": ["300000", "50", "800"],
        }
    )
    out = ev.permits_to_events(p, TZ).set_index("source_id")
    assert list(out.index) == ["A1", "A3"]
    assert out.loc["A1", "start_utc"] == ts("2023-06-25 07:00") and out.loc["A1", "end_utc"] == ts("2023-06-26 07:00")
    assert out.loc["A3", "start_utc"] == ts("2023-12-02 08:00") and out.loc["A3", "end_utc"] == ts("2023-12-04 08:00")
    assert out.loc["A3", "status"] == "cancelled" and out.loc["A1", "attendance_kind"] == "applicant_estimate"


def test_holidays_observed_and_actual_dates_and_state_filter():
    payload = {
        2021: [
            {"date": "2021-07-05", "name": "Independence Day", "global": True, "counties": None, "types": ["Public"]},
            {"date": "2021-11-26", "name": "Native American Heritage Day", "global": False, "counties": ["US-WA"], "types": ["Public"]},
            {"date": "2021-04-02", "name": "Good Friday", "global": False, "counties": ["US-TX"], "types": ["Public"]},
            {"date": "2021-10-11", "name": "Columbus Day", "global": True, "counties": None, "types": ["Bank"]},
        ]
    }
    out = ev.holidays_to_events(payload, TZ)
    names = set(out["name"])
    assert names == {"Independence Day", "Independence Day (actual date)", "Native American Heritage Day"}
    actual = out[out["name"] == "Independence Day (actual date)"].iloc[0]
    assert actual["start_utc"] == ts("2021-07-04 07:00")


def test_assign_neighborhood_centroids_single_neighborhood_only():
    e = pd.DataFrame(
        {"lat": [np.nan, np.nan, np.nan], "lon": [np.nan] * 3, "_neighborhoods": ["Ballard", "Belltown;Downtown", "Duwamish / SODO"]}
    )
    hoods = pd.DataFrame(
        {
            "s_hood": ["Ballard", "SODO"],
            "l_hood": ["Ballard", "Greater Duwamish"],
            "centroid_lat": [47.67, 47.58],
            "centroid_lon": [-122.38, -122.33],
        }
    )
    out = ev.assign_neighborhood_centroids(e, hoods)
    assert out.loc[0, "lat"] == 47.67 and np.isnan(out.loc[1, "lat"]) and out.loc[2, "lat"] == 47.58


# --------------------------------------------------------------------------- GTFS


def _tiny_feed() -> zipfile.ZipFile:
    files = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n1,Metro,https://x,America/Los_Angeles\n",
        "feed_info.txt": "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version\n"
        "Metro,https://x,en,20261019,20261231,v1\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20261019,20261231\nSU,0,0,0,0,0,0,1,20261019,20261231\n",
        # Wednesday service removed, an extra Saturday added
        "calendar_dates.txt": "service_id,date,exception_type\nWK,20261021,2\nWK,20261024,1\n",
        "routes.txt": "route_id,agency_id,route_short_name,route_type\nR1,1,1,3\n",
        "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\nR1,SU,T2\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "A,Stop A,47.60,-122.33,0,\nB,Stop B,47.61,-122.33,,\nC,Stop C,47.62,-122.33,0,\n"
        "FAR,Far Away,46.00,-120.00,0,\n",
        # T1: A 08:10, B untimed (inherits 08:10), C no pickup; T2: Sunday-night trip crossing midnight
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type\n"
        "T1,08:10:00,08:10:00,A,1,0\nT1,,,B,2,0\nT1,08:30:00,08:30:00,C,3,1\n"
        "T2,23:50:00,23:50:00,A,1,0\nT2,24:20:00,24:20:00,B,2,0\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_gtfs_hour_of_week_counts_and_wraparound():
    feed = gtfs.Feed("t", "Test", ("https://x",))
    stops, how, info = gtfs.summarize_feed(feed, _tiny_feed(), TZ, [-122.44, 47.495, -122.235, 47.735], date(2026, 10, 14))
    assert info["representative_week_start"] == "2026-10-19"
    h = how.set_index(["stop_id", "hour_of_week"])["scheduled_departures"]
    # T1 runs Mon, Tue, Thu, Fri (Wed removed) + Sat (added) at 08h; stop C has no pickup
    for dow in (0, 1, 3, 4, 5):
        assert h[("t:A", dow * 24 + 8)] == 1 and h[("t:B", dow * 24 + 8)] == 1
    assert ("t:A", 2 * 24 + 8) not in h.index and not any(i[0] == "t:C" for i in h.index)
    # Sunday 23:50 at A; 24:20 at B wraps to Monday 00h (hour_of_week 0)
    assert h[("t:A", 6 * 24 + 23)] == 1 and h[("t:B", 0)] == 1
    s = stops.set_index("stop_id")
    assert "t:FAR" not in s.index  # outside bbox
    assert s.loc["t:A", "trips_per_week"] == 6 and s.loc["t:C", "trips_per_week"] == 0
    assert s.loc["t:A", "modes"] == "bus" and s.loc["t:A", "agency"] == "Metro"


def test_representative_week_skips_dst_and_holiday_weeks():
    # 2026-10-26 week contains the DST change (Sun 2026-11-01) -> next week
    assert gtfs.representative_week(date(2026, 10, 26), date(2026, 12, 31), TZ) == date(2026, 11, 2)
    # 2026-11-09 week contains Veterans Day (Wed 11-11) -> 11-16
    assert gtfs.representative_week(date(2026, 11, 9), date(2026, 12, 31), TZ) == date(2026, 11, 16)


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_len(field: int, payload: bytes) -> bytes:
    return _pb_varint(field << 3 | 2) + _pb_varint(len(payload)) + payload


def _pb_int(field: int, value: int) -> bytes:
    return _pb_varint(field << 3) + _pb_varint(value)


def test_decode_gtfs_rt_alert():
    translated = _pb_len(1, _pb_len(1, b"Route 7 detour: demonstration") + _pb_len(2, b"en"))
    alert = (
        _pb_len(1, _pb_int(1, 1_760_000_000) + _pb_int(2, 1_760_003_600))
        + _pb_len(5, _pb_len(2, b"100264") + _pb_len(5, b"1234"))
        + _pb_int(6, 5)
        + _pb_int(7, 4)
        + _pb_len(10, translated)
    )
    msg = _pb_len(1, _pb_len(1, b"2.0") + _pb_int(3, 1_760_000_100)) + _pb_len(2, _pb_len(1, b"A1") + _pb_len(5, alert))
    header_ts, alerts = gtfs.decode_gtfs_rt_alerts(msg)
    assert header_ts == 1_760_000_100 and len(alerts) == 1
    a = alerts[0]
    assert a["alert_id"] == "A1" and a["cause"] == "DEMONSTRATION" and a["effect"] == "DETOUR"
    assert a["header_text"] == "Route 7 detour: demonstration" and a["route_ids"] == {"100264"}
    df = gtfs.alerts_to_frame(header_ts, alerts, "kcm", datetime(2026, 10, 14, tzinfo=UTC))
    assert df.loc[0, "active_start_utc"] == pd.Timestamp(1_760_000_000, unit="s", tz=UTC_)
    with pytest.raises(ValueError):
        list(gtfs.pb_fields(b"\x0a\x05ab"))  # truncated length-delimited field


def test_merge_alert_history_tracks_first_and_last_seen():
    t1, t2 = datetime(2026, 10, 14, 1, tzinfo=UTC), datetime(2026, 10, 14, 2, tzinfo=UTC)
    base = {
        "alert_id": "A1",
        "cause": "X",
        "effect": "Y",
        "route_ids": "",
        "stop_ids": "",
        "description_text": None,
        "url": None,
        "n_active_periods": 1,
        "active_start_utc": ts("2026-10-14"),
        "active_end_utc": pd.NaT,
        "feed_header_utc": pd.NaT,
    }
    a = gtfs.alerts_to_frame(None, [], "kcm", t1)
    h1 = pd.DataFrame(
        [{**base, "feed": "kcm", "header_text": "h", "first_seen_at_utc": pd.Timestamp(t1), "last_seen_at_utc": pd.Timestamp(t1)}]
    )
    h2 = h1.assign(first_seen_at_utc=pd.Timestamp(t2), last_seen_at_utc=pd.Timestamp(t2))
    m = gtfs.merge_alert_history(h1, h2)
    assert a.empty and len(m) == 1
    assert m.loc[0, "first_seen_at_utc"] == pd.Timestamp(t1) and m.loc[0, "last_seen_at_utc"] == pd.Timestamp(t2)


# --------------------------------------------------------------------------- news

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Protest march closes 3rd Ave in Belltown</title>
<link>https://example.com/a?utm_source=rss#top</link>
<pubDate>Wed, 14 Oct 2026 10:00:00 -0700</pubDate>
<description>&lt;p&gt;Police say the &lt;b&gt;rally&lt;/b&gt; drew hundreds.&lt;/p&gt;</description></item>
<item><title>Power outage hits Ballard</title><link>https://example.com/b/</link>
<pubDate>Wed, 14 Oct 2026 17:30:00 GMT</pubDate></item>
<item><title>No link item</title></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
<entry><title>Festival this weekend at Seattle Center</title><link rel="alternate" href="https://example.org/f"/>
<published>2026-10-13T09:00:00-07:00</published><summary>Street fair</summary></entry></feed>"""

FETCHED = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


def test_parse_feed_rss_and_atom():
    r = news.parse_feed(RSS, "src", FETCHED)
    assert len(r) == 2  # item without link dropped
    assert r.loc[0, "published_at_utc"] == ts("2026-10-14 17:00")
    assert r.loc[0, "summary"] == "Police say the rally drew hundreds."
    assert r.loc[0, "fetched_at_utc"] == ts("2026-10-14 18:00") == r.loc[0, "available_at_utc"]
    assert bool(r.loc[1, "observed_live"]) and bool(r.loc[0, "observed_live"])
    a = news.parse_feed(ATOM, "atom", FETCHED)
    assert a.loc[0, "url"] == "https://example.org/f" and a.loc[0, "published_at_utc"] == ts("2026-10-13 16:00")
    assert not bool(a.loc[0, "observed_live"])  # >6 h old when first fetched


def test_canonical_url_and_merge_keeps_first_seen():
    assert news.canonical_url("HTTPS://Example.com/a/?utm_source=x&id=3#frag") == "https://example.com/a?id=3"
    first = news.parse_feed(RSS, "src", FETCHED)
    later_rss = RSS.replace(b"Protest march closes", b"UPDATED: Protest march closes").replace(b"?utm_source=rss#top", b"?utm_medium=email")
    later = news.parse_feed(later_rss, "src", datetime(2026, 10, 14, 20, 0, tzinfo=UTC))
    m = news.merge_news(first, later)
    assert len(m) == 2 and m["item_id"].is_unique
    row = m[m["url"].str.contains("/a")].iloc[0]
    assert row["title"].startswith("Protest march")  # first-seen version kept
    assert row["fetched_at_utc"] == ts("2026-10-14 18:00") and row["last_seen_at_utc"] == ts("2026-10-14 20:00")


def test_usable_news_strict_vs_labelled_backfill():
    items = news.merge_news(None, news.parse_feed(ATOM, "atom", FETCHED))  # published 16:00Z on 10-13
    assert news.usable_news(items, "2026-10-14T12:00Z").empty  # strict: only after fetched_at
    assert len(news.usable_news(items, "2026-10-14T18:00Z")) == 1
    bf = news.usable_news(items, "2026-10-13T17:30Z", mode="backfill")  # published + 1 h margin
    assert len(bf) == 1 and bf.iloc[0]["availability_basis"] == "backfill_published_plus_margin"
    assert news.usable_news(items, "2026-10-13T16:30Z", mode="backfill").empty
    with pytest.raises(ValueError):
        news.usable_news(items, "2026-10-14", mode="oracle")


def test_parse_gdelt_artlist():
    payload = {"articles": [{"url": "https://k.com/x", "title": "Seattle news", "seendate": "20261014T183000Z", "domain": "k.com"}]}
    g = news.parse_gdelt_artlist(payload, FETCHED)
    assert g.loc[0, "published_at_utc"] == ts("2026-10-14 18:30") and g.loc[0, "outlet"] == "k.com"


def test_keyword_signals_and_llm_hooks(monkeypatch):
    items = news.parse_feed(RSS, "src", FETCHED)
    base = news.extract_news_signals(items, None, ["Ballard", "Belltown"])
    r0, r1 = base.iloc[0], base.iloc[1]
    assert r0["flag_protest"] and r0["flag_closure"] and r0["flag_police_activity"] and r0["neighborhoods"] == "Belltown"
    assert r1["flag_power_outage"] and r1["neighborhoods"] == "Ballard" and not r1["flag_protest"]
    assert set(base["extraction_method"]) == {"keyword"} and "llm_event_type" not in base

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auto = news.extract_news_signals(items, "auto", ["Ballard"])  # no key -> keyword fallback
    assert "llm_event_type" not in auto and auto["flag_power_outage"].iloc[1]

    def stub(prompt: str) -> str:
        ids = [d["item_id"] for d in json.loads(prompt[prompt.index("[") :])]
        return "Sure:\n" + json.dumps(
            [{"item_id": ids[0], "event_type": "protest", "neighborhood": "Belltown", "expected_crowd": 500, "is_future_event": False}]
        )

    llm = news.extract_news_signals(items, stub, ["Ballard"])
    assert llm.loc[0, "llm_event_type"] == "protest" and llm.loc[0, "llm_expected_crowd"] == 500
    assert llm.loc[0, "extraction_method"] == "llm+keyword" and llm.loc[1, "extraction_method"] == "keyword"

    def broken(prompt: str) -> str:
        raise RuntimeError("API down")

    assert news.extract_news_signals(items, broken)["llm_event_type"].isna().all()
