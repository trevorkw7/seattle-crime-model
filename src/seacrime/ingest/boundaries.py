"""Static geography for Seattle: city limits, census tracts (+2020 population),
zoning, street network, SPD beats and neighborhoods.

Sources (all public, no key):

* City of Seattle GIS open data (ArcGIS Online org ``ZOyb2t4B0UYuYNYH``,
  discovered via the data-seattlecitygis.opendata.arcgis.com search API):
  ``Seattle_Area_Polygon`` (city limits polygon), ``2020_Census_Tracts_Seattle``
  (the 177 tracts the City assigns to Seattle, shoreline-clipped),
  ``Current_Land_Use_Zoning_Detail_2``, ``Seattle_Streets_1`` (SDOT street
  segments with arterial classification), ``Current_Beats`` (SPD beats),
  ``nma_nhoods_sub`` (Neighborhood Map Atlas neighborhoods).
* U.S. Census Bureau TIGERweb (``tigerWMS_Census2020``): 2020 tract
  population (POP100) and housing units (HU100) from the 2020 decennial
  census redistricting file, and the Census "Seattle city" place polygon used
  as a cross-check. (api.census.gov now requires a key — see docs.)

All layers are requested as GeoJSON in EPSG:4326 with 6-decimal coordinate
precision (~0.1 m) and written as GeoParquet under ``data/processed/``.
These are *current* vintages; zoning and beats change over time (see docs).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely

from ..config import Config
from ._context_common import RawSnapshot, http_get, session, write_parquet

log = logging.getLogger(__name__)

ARCGIS = "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services"
TIGERWEB = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Census2020/MapServer"
METRIC_CRS = "EPSG:32610"  # UTM 10N, matches the modelling grid


@dataclass(frozen=True)
class Layer:
    url: str
    out_fields: str = "*"
    where: str = "1=1"
    page_size: int = 2000
    extra: dict[str, Any] = field(default_factory=dict)


LAYERS: dict[str, Layer] = {
    "city_limits": Layer(f"{ARCGIS}/Seattle_Area_Polygon/FeatureServer/0", "OBJECTID,Note"),
    "seattle_tracts": Layer(
        f"{ARCGIS}/2020_Census_Tracts_Seattle/FeatureServer/3",
        "GEOID20,NAME,TRACT_NUMB,LAND_ACRES,WATER_ACRES,CRA_NO,CRA_GRP,GEN_ALIAS,DETL_NAMES,C_DISTRICT",
    ),
    "tiger_tracts": Layer(
        f"{TIGERWEB}/6",
        "GEOID,NAME,POP100,HU100,AREALAND,AREAWATER,INTPTLAT,INTPTLON",
        where="STATE='53' AND COUNTY='033'",
        extra={"returnGeometry": "false"},
    ),
    "tiger_place": Layer(f"{TIGERWEB}/26", "GEOID,NAME,POP100,HU100,AREALAND,AREAWATER", where="GEOID='5363000'"),
    "zoning": Layer(
        f"{ARCGIS}/Current_Land_Use_Zoning_Detail_2/FeatureServer/0",
        "ZONEID,ZONING,BASE_ZONE,CLASS_DESC,CATEGORY_DESC,ZONELUT,ZONELUT_DESC,EFFECTIVE,OVERLAY,LIGHTRAIL,MIO_NAME,MHA",
    ),
    "streets": Layer(
        f"{ARCGIS}/Seattle_Streets_1/FeatureServer/0",
        "COMPKEY,STNAME_ORD,ARTCLASS,ARTDESCRIPT,SPEEDLIMIT,SEGLENGTH,ONEWAY,STATUS,OWNER,SURFACEWIDTH,FLOW",
    ),
    "spd_beats": Layer(f"{ARCGIS}/Current_Beats/FeatureServer/0", "beat,first_precinct,sector"),
    "neighborhoods": Layer(f"{ARCGIS}/nma_nhoods_sub/FeatureServer/0", "L_HOOD,S_HOOD,S_HOOD_ALT_NAMES"),
}

LICENSE = {
    "seattle_gis": "City of Seattle open data (data-seattlecitygis.opendata.arcgis.com), Public Domain / no restrictions",
    "census_tigerweb": "U.S. Census Bureau TIGER/Line & 2020 Census, public domain (U.S. Government work)",
}


# --------------------------------------------------------------------------- download


def fetch_layer(snap: RawSnapshot, name: str, layer: Layer) -> gpd.GeoDataFrame:
    """Page through an ArcGIS REST query as GeoJSON (EPSG:4326), storing each page raw."""
    sess = session()
    url = f"{layer.url}/query"
    offset, page, frames = 0, 0, []
    no_geom = layer.extra.get("returnGeometry") == "false"
    while True:
        params = {
            "where": layer.where,
            "outFields": layer.out_fields,
            "outSR": 4326,
            "f": "json" if no_geom else "geojson",
            "returnGeometry": "true",
            "geometryPrecision": 6,
            "resultOffset": offset,
            "resultRecordCount": layer.page_size,
            **layer.extra,
        }
        r = http_get(sess, url, params, timeout=300)
        payload = r.json()
        if "error" in payload:
            raise RuntimeError(f"{name}: ArcGIS error {payload['error']}")
        feats = payload.get("features", [])
        snap.save(f"{name}/page-{page:03d}.json", r.content, url, params, rows=len(feats))
        if feats:
            if no_geom:
                frames.append(gpd.GeoDataFrame(pd.DataFrame([f["attributes"] for f in feats])))
            else:
                frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
        exceeded = payload.get("exceededTransferLimit") or (payload.get("properties") or {}).get("exceededTransferLimit")
        offset += len(feats)
        page += 1
        if not feats or (not exceeded and len(feats) < layer.page_size):
            break
    gdf = pd.concat(frames, ignore_index=True) if frames else gpd.GeoDataFrame()
    log.info("%s: %d features in %d page(s)", name, len(gdf), page)
    return gdf


# --------------------------------------------------------------------------- transforms (pure)


def make_valid_polygons(geoms: gpd.GeoSeries) -> gpd.GeoSeries:
    """Repair invalid (self-intersecting) polygons without collapsing them to lines."""
    fixed = shapely.make_valid(geoms.values, method="structure", keep_collapsed=False)
    return gpd.GeoSeries(fixed, index=geoms.index, crs=geoms.crs)


def build_city_limits(city: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    geom = make_valid_polygons(city.geometry).union_all()
    out = gpd.GeoDataFrame({"name": ["City of Seattle"], "source": ["seattle_gis:Seattle_Area_Polygon"]}, geometry=[geom], crs="EPSG:4326")
    out["area_km2"] = out.to_crs(METRIC_CRS).area / 1e6
    return out


def build_tracts(seattle: gpd.GeoDataFrame, tiger: pd.DataFrame, city: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Seattle (shoreline-clipped) tract polygons joined to 2020 census population."""
    attrs = tiger.rename(columns=str.upper)[["GEOID", "POP100", "HU100", "AREALAND", "AREAWATER"]]
    t = seattle.rename(columns={"GEOID20": "GEOID"}).merge(attrs, on="GEOID", how="left")
    out = gpd.GeoDataFrame(
        {
            "GEOID": t["GEOID"].astype(str),
            "name": t["NAME"],
            "population": pd.to_numeric(t["POP100"], errors="coerce").astype("Int64"),
            "housing_units": pd.to_numeric(t["HU100"], errors="coerce").astype("Int64"),
            "aland_m2": pd.to_numeric(t["AREALAND"], errors="coerce"),
            "awater_m2": pd.to_numeric(t["AREAWATER"], errors="coerce"),
            "cra_no": t.get("CRA_NO"),
            "cra_group": t.get("CRA_GRP"),
            "neighborhood_alias": t.get("GEN_ALIAS"),
            "council_district": t.get("C_DISTRICT"),
            "population_source": "2020 Decennial Census P.L. 94-171 (POP100 via TIGERweb)",
        },
        geometry=make_valid_polygons(t.geometry).values,
        crs="EPSG:4326",
    )
    m = out.to_crs(METRIC_CRS)
    city_m = city.to_crs(METRIC_CRS).union_all()
    out["area_km2"] = m.area / 1e6
    out["share_in_city"] = (m.intersection(city_m).area / m.area).round(4)
    pop_km2 = out["population"].astype("float64") / out["area_km2"]
    out["pop_density_km2"] = pop_km2.round(1)
    return out


def build_zoning(z: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = gpd.GeoDataFrame(
        {
            "zone_id": z["ZONEID"],
            "zoning": z["ZONING"],
            "base_zone": z["BASE_ZONE"],
            "zone_category": z["CATEGORY_DESC"],  # simplified category (10 values)
            "zone_class": z["CLASS_DESC"],
            "zonelut": z["ZONELUT"],
            "zonelut_desc": z["ZONELUT_DESC"],
            "overlay": z["OVERLAY"],
            "light_rail_overlay": z["LIGHTRAIL"],
            "major_institution": z["MIO_NAME"],
            "effective": pd.to_datetime(pd.to_numeric(z["EFFECTIVE"], errors="coerce"), unit="ms", utc=True),
        },
        geometry=make_valid_polygons(z.geometry).values,
        crs="EPSG:4326",
    )
    return out[~out.geometry.is_empty & out.geometry.notna()]


ROAD_CLASS = {  # SDOT ARTCLASS codes -> labels (ARTDESCRIPT carries the same text)
    0: "local",
    1: "principal_arterial",
    2: "minor_arterial",
    3: "collector_arterial",
    4: "state_route_freeway",
    5: "interstate_freeway",
    9: "county_arterial",
}


def build_streets(s: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    code = pd.to_numeric(s["ARTCLASS"], errors="coerce").astype("Int64")
    out = gpd.GeoDataFrame(
        {
            "compkey": s["COMPKEY"],
            "street_name": s["STNAME_ORD"],
            "road_class": code.map(ROAD_CLASS).fillna("unknown"),
            "artclass": code,
            "artdescript": s["ARTDESCRIPT"],
            "speed_limit_mph": pd.to_numeric(s["SPEEDLIMIT"], errors="coerce"),
            "oneway": s["ONEWAY"],
            "status": s["STATUS"],
            "owner": s["OWNER"],
            "surface_width_ft": pd.to_numeric(s["SURFACEWIDTH"], errors="coerce"),
        },
        geometry=s.geometry.values,
        crs="EPSG:4326",
    )
    out["length_m"] = out.to_crs(METRIC_CRS).length.round(1)
    return out[~out.geometry.is_empty & out.geometry.notna()]


def build_beats(b: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = gpd.GeoDataFrame(
        {"beat": b["beat"], "precinct": b["first_precinct"], "sector": b["sector"]},
        geometry=make_valid_polygons(b.geometry).values,
        crs="EPSG:4326",
    )
    return out.dissolve(by="beat", as_index=False, aggfunc="first") if out["beat"].duplicated().any() else out


def build_neighborhoods(n: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = gpd.GeoDataFrame(
        {"l_hood": n["L_HOOD"], "s_hood": n["S_HOOD"], "s_hood_alt_names": n["S_HOOD_ALT_NAMES"]},
        geometry=make_valid_polygons(n.geometry).values,
        crs="EPSG:4326",
    )
    rp = out.to_crs(METRIC_CRS).representative_point().to_crs("EPSG:4326")
    out["centroid_lat"], out["centroid_lon"] = rp.y.round(6), rp.x.round(6)
    return out


# --------------------------------------------------------------------------- entry point


def fetch_boundaries(cfg: Config) -> dict[str, Path]:
    snap = RawSnapshot(cfg.raw_dir, "boundaries")
    raw = {name: fetch_layer(snap, name, layer) for name, layer in LAYERS.items()}
    proc = cfg.processed_dir
    city = build_city_limits(raw["city_limits"])
    built: dict[str, gpd.GeoDataFrame] = {
        "city_limits": city,
        "census_tracts": build_tracts(raw["seattle_tracts"], raw["tiger_tracts"], city),
        "zoning": build_zoning(raw["zoning"]),
        "streets": build_streets(raw["streets"]),
        "spd_beats": build_beats(raw["spd_beats"]),
        "neighborhoods": build_neighborhoods(raw["neighborhoods"]),
    }
    outputs = {k: write_parquet(v, proc / f"{k}.parquet") for k, v in built.items()}

    # cross-check against the Census place polygon (includes water to the legal boundary)
    place = raw["tiger_place"].to_crs(METRIC_CRS)
    city_m = city.to_crs(METRIC_CRS).union_all()
    check = {
        "seattle_gis_city_area_km2": round(city_m.area / 1e6, 2),
        "census_place_area_km2": round(place.area.sum() / 1e6, 2),
        "census_place_aland_km2": round(float(place["AREALAND"].sum()) / 1e6, 2),
        "share_of_city_polygon_inside_census_place": round(city_m.intersection(place.union_all()).area / city_m.area, 4),
        "census_place_pop100": int(place["POP100"].sum()),
        "sum_tract_population": int(built["census_tracts"]["population"].sum()),
    }
    snap.finish(
        license=LICENSE,
        outputs={k: str(v) for k, v in outputs.items()},
        row_counts={k: len(v) for k, v in built.items()},
        cross_check=check,
    )
    log.info("boundaries written: %s\ncross-check: %s", {k: len(v) for k, v in built.items()}, json.dumps(check))
    return outputs
