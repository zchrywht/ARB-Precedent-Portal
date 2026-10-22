#!/usr/bin/env python3
"""
prep_hoh.py  –  Data prep for Village of Head of the Harbor, Suffolk County NY.

Fetches parcels, building footprints, and historically registered properties
from NYS ITS GIS / NYSHPO REST services, spatially joins footprints to parcels
(each footprint gets YR_BLT and parcel ID), and writes three WGS-84 GeoJSON
files ready for web use.

Run once; outputs are committed to source-data/ and consumed by the web app.

Requirements:
    pip install geopandas requests shapely pandas
"""

import argparse
import csv as _csv
import json
import os
import re
import sys
import urllib.parse

import geopandas as gpd
import requests

# ── Endpoints ─────────────────────────────────────────────────────────────────

CIVIL_URL = (
    "https://gisservices.its.ny.gov/arcgis/rest/services"
    "/NYS_Civil_Boundaries/FeatureServer/7/query"
)
PARCELS_URL = (
    "https://gisservices.its.ny.gov/arcgis/rest/services"
    "/NYS_Tax_Parcels_Public/FeatureServer/1/query"
)
FOOTPRINTS_URL = (
    "https://gisservices.its.ny.gov/arcgis/rest/services"
    "/BuildingFootprints/FeatureServer/2/query"
)
# NYS OPRHP – nomination boundaries for State/National Register properties
HISTORIC_URL = (
    "https://services.arcgis.com/1xFZPtKn1wKC6POA/arcgis/rest/services"
    "/National_Register_Building_Listings/FeatureServer/13/query"
)

# OpenStreetMap Overpass API — primary + mirrors tried in order
OSM_OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# ── Output files ──────────────────────────────────────────────────────────────

OUT_VILLAGE    = "source-data/village_hoh.geojson"
OUT_PARCELS    = "source-data/parcels_hoh.geojson"
OUT_FOOTPRINTS = "source-data/footprints_hoh.geojson"
OUT_HISTORIC   = "source-data/historic_hoh.geojson"

# ── Field lists (keep outputs lean) ──────────────────────────────────────────

PARCEL_FIELDS = ",".join([
    "SWIS_SBL_ID",      # unique parcel key (SWIS + SBL)
    "PRINT_KEY",        # human-readable SBL (hyphenated)
    "PROP_CLASS",       # property class code, e.g. "210" = 1-family residential
    "YR_BLT",           # year built
    "BLDG_STYLE",       # architectural style code
    "BLDG_STYLE_DESC",  # architectural style description
    "PARCEL_ADDR",      # full site address
    "LOC_ST_NBR",
    "LOC_STREET",
    "LOC_ZIP",
])

FOOTPRINT_FIELDS = ",".join([
    "OBJECTID",
    "SOURCE",
    "SOURCEDATE",
])

HISTORIC_FIELDS = ",".join([
    "NRNum",          # NYSHPO National Register number
    "HistoricName",   # historic property name
    "CityTown",
    "CountyName",
    "DateSR",         # NYS Register listing date
    "DateNR",         # National Register listing date
    "DateNHL",        # National Historic Landmark designation date (if any)
])

# Fields carried from parcel onto each joined footprint
JOIN_FIELDS = ["SWIS_SBL_ID", "PRINT_KEY", "PROP_CLASS", "YR_BLT", "BLDG_STYLE", "BLDG_STYLE_DESC", "PARCEL_ADDR", "LOT_ACRES", "EXEMPLARY", "HOUSE_NAME"]

# ArcGIS REST services cap responses; page through until done
PAGE_SIZE = 2000


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_img_dirs(csv_path: str) -> None:
    """Create web/img/<address>/ for each exemplary row, then write index.json."""
    img_root = "web/img"
    os.makedirs(img_root, exist_ok=True)
    created = 0
    if not os.path.exists(csv_path):
        return
    with open(csv_path, newline="", encoding="utf-8-sig") as _f:
        for row in _csv.DictReader(_f):
            if not row.get("EXEMPLARY", "").strip():
                continue
            addr = (row.get("PARCEL_ADDR_OVERRIDE") or row.get("PARCEL_ADDR") or "").strip()
            if not addr:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", addr.lower()).strip("_")
            path = os.path.join(img_root, slug)
            if not os.path.exists(path):
                os.makedirs(path)
                created += 1
    # Scan all img dirs and write index.json for the web map
    _img_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    index = {}
    for slug_dir in sorted(os.listdir(img_root)):
        full = os.path.join(img_root, slug_dir)
        if not os.path.isdir(full):
            continue
        imgs = sorted(f for f in os.listdir(full)
                      if os.path.splitext(f)[1].lower() in _img_exts)
        if imgs:
            index[slug_dir] = imgs
    with open(os.path.join(img_root, "index.json"), "w") as _f:
        json.dump(index, _f, indent=2)
    print(f"  image dirs: {created} new folder(s) in {img_root}/, {len(index)} with images indexed")


def _read_overrides(csv_path: str, parcels_gdf) -> tuple[dict, dict, dict, dict, dict]:
    """Read manual_overrides.csv and return (yr, style, exemplary, house_name, addr) dicts keyed by SWIS_SBL_ID.

    Excel corrupts long numeric IDs to scientific notation (e.g. 4.73E+25), so
    we match by PARCEL_ADDR as the primary key and look up the real SBL_ID from
    the loaded parcels GeoDataFrame.
    """
    # Build addr → [sbl, ...] and (addr, lot_acres) → sbl for precise matching
    addr_to_sbls: dict[str, list] = {}
    addr_acres_to_sbl: dict[tuple, str] = {}
    for _, row in parcels_gdf.iterrows():
        addr  = str(row.get("PARCEL_ADDR") or "").strip().upper()
        sbl   = row["SWIS_SBL_ID"]
        acres = str(row.get("LOT_ACRES") or "").strip()
        if addr:
            addr_to_sbls.setdefault(addr, []).append(sbl)
            if acres:
                addr_acres_to_sbl[(addr, acres)] = sbl

    yr_overrides    = {}
    style_overrides = {}
    exemplary       = {}
    house_names     = {}
    addr_overrides  = {}

    if not os.path.exists(csv_path):
        return yr_overrides, style_overrides, exemplary, house_names, addr_overrides

    with open(csv_path, newline="", encoding="utf-8-sig") as _f:
        for row in _csv.DictReader(_f):
            addr      = str(row.get("PARCEL_ADDR") or "").strip().upper()
            lot_acres = str(row.get("LOT_ACRES") or "").strip()

            # When a row has PARCEL_ADDR_OVERRIDE it targets exactly ONE parcel.
            # Identify it by (addr, lot_acres); fall back to the override address
            # for the --overrides-only case where the GeoJSON already has the new
            # address and the original addr no longer maps to this parcel.
            if row.get("PARCEL_ADDR_OVERRIDE"):
                specific = addr_acres_to_sbl.get((addr, lot_acres))
                if specific is None:
                    override_addr = row["PARCEL_ADDR_OVERRIDE"].strip().upper()
                    specific = addr_acres_to_sbl.get((override_addr, lot_acres))
                sbls = [specific] if specific else []
                if specific:
                    addr_overrides[specific] = row["PARCEL_ADDR_OVERRIDE"].strip()
            else:
                # No address override: fan out to all parcels sharing this address.
                sbls = addr_to_sbls.get(addr) or ([row["SWIS_SBL_ID"]] if row.get("SWIS_SBL_ID") else [])

            for sbl in sbls:
                if not sbl:
                    continue
                if row.get("YR_BLT_OVERRIDE"):
                    try:
                        yr_overrides[sbl] = int(row["YR_BLT_OVERRIDE"])
                    except ValueError:
                        pass
                if row.get("STYLE_OVERRIDE"):
                    style_overrides[sbl] = row["STYLE_OVERRIDE"].strip()
                if row.get("EXEMPLARY"):
                    exemplary[sbl] = row["EXEMPLARY"].strip()
                if row.get("HOUSE_NAME"):
                    house_names[sbl] = row["HOUSE_NAME"].strip()

    return yr_overrides, style_overrides, exemplary, house_names, addr_overrides


def fetch_features(url: str, params: dict) -> gpd.GeoDataFrame:
    """Fetch all features matching a spatial/attribute query from an ArcGIS FeatureServer.

    Strategy: two-step to avoid ArcGIS offset-pagination bugs where subsequent
    pages silently drop the spatial filter and return global results.

    Step 1 – returnIdsOnly: applies the full filter (spatial + where) once to
             get the exact set of matching OBJECTIDs.
    Step 2 – fetch geometries in OBJECTID batches with no spatial re-filter,
             so pagination is stable.
    """
    # ── Step 1: collect matching OBJECTIDs ───────────────────────────────────
    id_resp = requests.post(
        url,
        data={**params, "f": "json", "returnIdsOnly": "true"},
        timeout=60,
    )
    if not id_resp.ok:
        raise RuntimeError(
            f"HTTP {id_resp.status_code} from {url}\n{id_resp.text[:500]}"
        )
    all_ids = id_resp.json().get("objectIds") or []
    print(f"  {len(all_ids)} matching IDs…", end="\r", flush=True)

    if not all_ids:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326"))

    # ── Step 2: fetch geometries in OBJECTID batches ──────────────────────────
    all_features = []
    for i in range(0, len(all_ids), PAGE_SIZE):
        batch_ids = all_ids[i : i + PAGE_SIZE]
        feat_resp = requests.post(
            url,
            data={
                "f": "geojson",
                "outSR": "4326",
                "objectIds": ",".join(str(x) for x in batch_ids),
                "outFields": params.get("outFields", "*"),
            },
            timeout=60,
        )
        if not feat_resp.ok:
            raise RuntimeError(
                f"HTTP {feat_resp.status_code} fetching IDs batch\n{feat_resp.text[:500]}"
            )
        all_features.extend(feat_resp.json().get("features", []))
        print(f"  {len(all_features)}/{len(all_ids)} features…", end="\r", flush=True)

    print(f"  {len(all_features)} features total        ")

    return gpd.GeoDataFrame.from_features(
        {"type": "FeatureCollection", "features": all_features},
        crs="EPSG:4326",
    )


def to_esri_polygon_json(gdf: gpd.GeoDataFrame) -> str:
    """Dissolve a GeoDataFrame to one polygon and return Esri JSON string.

    Used as the spatial filter geometry for downstream queries.
    """
    geom = gdf.dissolve().geometry.iloc[0]
    if geom.geom_type == "Polygon":
        rings = [list(geom.exterior.coords)]
    else:
        rings = [list(p.exterior.coords) for p in geom.geoms]
    return json.dumps({"rings": rings})


def fetch_osm_years(village_geom):
    """Query Overpass for OSM buildings with date tags inside the village bounding box.

    Looks for start_date, construction:date, year_built, and year tags.
    Returns a GeoDataFrame (EPSG:4326) with columns: geometry, yr_osm.
    Returns an empty GeoDataFrame on error or if nothing is found.
    """
    from shapely.geometry import Polygon

    minx, miny, maxx, maxy = village_geom.bounds
    query = (
        "[out:json][timeout:60];\n"
        f'way["building"]({miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f});\n'
        "out body;\n>;\nout skel qt;\n"
    )
    headers = {"User-Agent": "prep_hoh/1.0 (Head of the Harbor ARB mapping)"}
    resp = None
    for endpoint in OSM_OVERPASS_URLS:
        try:
            resp = requests.post(endpoint, data={"data": query},
                                 headers=headers, timeout=90)
            resp.raise_for_status()
            break
        except Exception as e:
            print(f"  {endpoint.split('/')[2]} failed ({e}), trying next…")
            resp = None
    if resp is None:
        print(f"  all Overpass endpoints failed")
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326"))

    elements = resp.json().get("elements", [])
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    ways  = [e for e in elements if e["type"] == "way"]

    DATE_TAGS = ["start_date", "construction:date", "year_built", "year"]
    records = []
    for way in ways:
        tags = way.get("tags", {})
        raw = next((tags[t] for t in DATE_TAGS if t in tags), None)
        if raw is None:
            continue
        m = re.search(r"\b(1[5-9]\d{2}|20[0-2]\d)\b", raw)
        if not m:
            continue
        coords = [nodes[n] for n in way.get("nodes", []) if n in nodes]
        if len(coords) < 3:
            continue
        records.append({"yr_osm": int(m.group(1)), "geometry": Polygon(coords)})

    if not records:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326"))

    return gpd.GeoDataFrame(records, crs="EPSG:4326")


# ── CLI args ──────────────────────────────────────────────────────────────────

_ap = argparse.ArgumentParser()
_ap.add_argument("--overrides-only", action="store_true",
                 help="Re-apply manual_overrides.csv to existing GeoJSON files without re-fetching data")
_args = _ap.parse_args()

if _args.overrides_only:
    print("-- overrides-only mode: loading existing GeoJSON files --")
    parcels    = gpd.read_file(OUT_PARCELS)
    footprints = gpd.read_file(OUT_FOOTPRINTS)

    # Apply overrides (same logic as Step 2a)
    OVERRIDES_CSV = "source-data/manual_overrides.csv"
    yr_overrides, style_overrides, exemplary, house_names, addr_overrides = _read_overrides(OVERRIDES_CSV, parcels)

    if yr_overrides:
        parcels["YR_BLT"] = parcels.apply(
            lambda r: yr_overrides.get(r["SWIS_SBL_ID"], r["YR_BLT"]), axis=1
        )
    if style_overrides:
        parcels["BLDG_STYLE_DESC"] = parcels.apply(
            lambda r: style_overrides.get(r["SWIS_SBL_ID"], r["BLDG_STYLE_DESC"]), axis=1
        )
    if addr_overrides:
        parcels["PARCEL_ADDR"] = parcels.apply(
            lambda r: addr_overrides.get(r["SWIS_SBL_ID"], r["PARCEL_ADDR"]), axis=1
        )
    parcels["EXEMPLARY"]  = parcels["SWIS_SBL_ID"].map(exemplary).fillna("")
    parcels["HOUSE_NAME"] = parcels["SWIS_SBL_ID"].map(house_names).fillna("")

    print(f"  overrides: {len(yr_overrides)} year, {len(style_overrides)} style, "
          f"{len(exemplary)} exemplary, {len(house_names)} house names, {len(addr_overrides)} address")

    # Push updated JOIN_FIELDS back onto footprints via SWIS_SBL_ID lookup
    _update_cols = ["YR_BLT", "BLDG_STYLE_DESC", "EXEMPLARY", "HOUSE_NAME", "PARCEL_ADDR"]
    _lookup = parcels.set_index("SWIS_SBL_ID")[_update_cols]
    for _col in _update_cols:
        footprints[_col] = footprints["SWIS_SBL_ID"].map(_lookup[_col])
    footprints["EXEMPLARY"]  = footprints["EXEMPLARY"].fillna("")
    footprints["HOUSE_NAME"] = footprints["HOUSE_NAME"].fillna("")

    for _gdf, _path in [(parcels, OUT_PARCELS), (footprints, OUT_FOOTPRINTS)]:
        try:
            _gdf.to_file(_path, driver="GeoJSON")
            print(f"  wrote {_path}")
        except PermissionError:
            sys.exit(f"ERROR: cannot write {_path} – close it in VS Code and re-run.")

    _make_img_dirs(OVERRIDES_CSV)
    sys.exit(0)

# ── Step 1: Village boundary ──────────────────────────────────────────────────

print("Step 1 – village boundary")
village = fetch_features(
    CIVIL_URL,
    {
        "where": "NAME = 'Head of the Harbor'",
        "outFields": "NAME",
    },
)
if village.empty:
    sys.exit("ERROR: village boundary not found – check the layer index or name.")

village_esri = to_esri_polygon_json(village)

# ── Step 2: Parcels ───────────────────────────────────────────────────────────

print("Step 2 – parcels")
parcels = fetch_features(
    PARCELS_URL,
    {
        # Restrict to Smithtown SWIS codes (4734xx) so the slightly oversized
        # village boundary polygon doesn't pull in parcels from neighboring
        # Brookhaven-town municipalities (e.g. Old Field / Stony Brook).
        "where": "SWIS_SBL_ID LIKE '4734%'",
        "outFields": PARCEL_FIELDS,
        "geometry": village_esri,
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
    },
)
print(f"  {len(parcels)} parcels fetched")

# Compute lot area in acres (project to NY State Plane Long Island for accuracy)
parcels["LOT_ACRES"] = (
    parcels.to_crs("EPSG:2263").geometry.area / 43560
).round(2)

# ── Step 2a: Backfill missing year data from ATTOM + OpenStreetMap ───────────

print("Step 2a – supplemental data (ATTOM + OpenStreetMap)")
village_geom = village.dissolve().geometry.iloc[0]

missing_yr = int((parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)).sum())
print(f"  before backfill: {missing_yr} parcels missing YR_BLT")

# ── ATTOM (CSV written by fetch_attom.py) ────────────────────────────────────
ATTOM_CSV = "source-data/attom_yr_blt.csv"
if os.path.exists(ATTOM_CSV):
    import csv
    attom_map = {}
    with open(ATTOM_CSV, newline="") as _f:
        for row in csv.DictReader(_f):
            if row["YR_BLT"]:
                attom_map[row["SWIS_SBL_ID"]] = int(row["YR_BLT"])
    mask = parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)
    parcels.loc[mask, "YR_BLT"] = parcels.loc[mask, "SWIS_SBL_ID"].map(attom_map)
    filled = int(mask.sum() - (parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)).sum())
    print(f"  ATTOM filled {filled} YR_BLT values ({len(attom_map)} entries in CSV)")
else:
    print(f"  no ATTOM data (run fetch_attom.py to generate {ATTOM_CSV})")

print("  querying OpenStreetMap Overpass…")
osm_gdf = fetch_osm_years(village_geom)
print(f"  {len(osm_gdf)} OSM buildings with date tags")

if not osm_gdf.empty:
    osm_c = osm_gdf.copy()
    osm_c["geometry"] = (
        osm_gdf.geometry.to_crs("EPSG:32618").centroid.to_crs("EPSG:4326")
    )
    osm_joined = gpd.sjoin(
        osm_c[["geometry", "yr_osm"]],
        parcels[["geometry", "SWIS_SBL_ID"]],
        how="left", predicate="within",
    )
    osm_yr_map = (
        osm_joined[osm_joined["SWIS_SBL_ID"].notna()]
        .groupby("SWIS_SBL_ID")["yr_osm"].min()
    )
    mask = parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)
    parcels.loc[mask, "YR_BLT"] = parcels.loc[mask, "SWIS_SBL_ID"].map(osm_yr_map)
    filled = int(mask.sum() - (parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)).sum())
    print(f"  OSM filled {filled} YR_BLT values")

missing_yr = int((parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)).sum())
print(f"  after OSM:       {missing_yr} parcels missing YR_BLT")

# ── Manual overrides (manual_overrides.csv) ───────────────────────────────────
OVERRIDES_CSV = "source-data/manual_overrides.csv"
yr_overrides, style_overrides, exemplary, house_names, addr_overrides = _read_overrides(OVERRIDES_CSV, parcels)

if yr_overrides:
    parcels["YR_BLT"] = parcels.apply(
        lambda r: yr_overrides.get(r["SWIS_SBL_ID"], r["YR_BLT"]), axis=1
    )
if style_overrides:
    parcels["BLDG_STYLE_DESC"] = parcels.apply(
        lambda r: style_overrides.get(r["SWIS_SBL_ID"], r["BLDG_STYLE_DESC"]), axis=1
    )
if addr_overrides:
    parcels["PARCEL_ADDR"] = parcels.apply(
        lambda r: addr_overrides.get(r["SWIS_SBL_ID"], r["PARCEL_ADDR"]), axis=1
    )
parcels["EXEMPLARY"]  = parcels["SWIS_SBL_ID"].map(exemplary).fillna("")
parcels["HOUSE_NAME"] = parcels["SWIS_SBL_ID"].map(house_names).fillna("")
print(f"  manual overrides: {len(yr_overrides)} year, {len(style_overrides)} style, "
      f"{len(exemplary)} exemplary, {len(house_names)} house names, {len(addr_overrides)} address")

missing_yr = int((parcels["YR_BLT"].isna() | (parcels["YR_BLT"] == 0)).sum())
print(f"  after backfill:  {missing_yr} parcels missing YR_BLT")

# ── Step 3: Building footprints ───────────────────────────────────────────────

print("Step 3 – building footprints")
footprints = fetch_features(
    FOOTPRINTS_URL,
    {
        "where": "1=1",
        "outFields": FOOTPRINT_FIELDS,
        "geometry": village_esri,
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
    },
)
print(f"  {len(footprints)} footprints fetched")

# ── Step 4: Clip footprints to village boundary ───────────────────────────────

print("Step 4 – clipping footprints to village boundary")
village_union = village.dissolve().geometry.iloc[0]
footprints = footprints[footprints.geometry.intersects(village_union)].copy()
print(f"  {len(footprints)} footprints after clip")

# ── Step 5: Spatial join footprints → parcels ─────────────────────────────────

print("Step 5 – spatial join")
# Use footprint centroids for the join: more reliable than polygon-on-polygon
# when a building sits on a parcel boundary.
fp_centroids = footprints.copy()
# Project to a meter-based CRS for accurate centroid, then reproject back
fp_centroids["geometry"] = (
    footprints.geometry
    .to_crs("EPSG:32618")   # UTM zone 18N covers Long Island
    .centroid
    .to_crs("EPSG:4326")
)

joined = gpd.sjoin(
    fp_centroids,
    parcels[["geometry"] + JOIN_FIELDS],
    how="left",
    predicate="within",
)

# Restore original footprint polygons (centroids were only for the join)
joined["geometry"] = footprints.loc[joined.index, "geometry"].values
joined.drop(columns=["index_right"], errors="ignore", inplace=True)

# Drop footprints that didn't join to any parcel — these are either on the
# wrong side of the boundary (foreign-municipality parcels filtered out above)
# or genuine edge slivers.  They have no useful data and would render grey.
footprints_out = gpd.GeoDataFrame(
    joined[joined["SWIS_SBL_ID"].notna()].copy(),
    crs="EPSG:4326",
)

print(f"  {len(footprints_out)} footprints retained (matched to a Smithtown parcel)")

# ── Step 6: Historically registered properties ───────────────────────────────

print("Step 6 – historic properties (NYS/National Register)")
historic = fetch_features(
    HISTORIC_URL,
    {
        "where": "CountyName = 'Suffolk'",
        "outFields": HISTORIC_FIELDS,
        "geometry": village_esri,
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
    },
)
print(f"  {len(historic)} historic listings fetched")

# Clip to village boundary so nomination polygons that merely touch the edge
# don't bleed into the map.
if not historic.empty:
    historic = historic[historic.geometry.intersects(village_union)].copy()
    print(f"  {len(historic)} after clip")

# Derive a human-readable register_type field for the web map legend
def _register_type(row):
    if row.get("DateNHL"):
        return "National Historic Landmark"
    if row.get("DateNR"):
        return "National Register"
    if row.get("DateSR"):
        return "State Register"
    return "Listed"

if not historic.empty:
    historic["register_type"] = historic.apply(_register_type, axis=1)

# ── Step 7: Write GeoJSON ─────────────────────────────────────────────────────

print("Step 7 – writing output files")
os.makedirs("source-data", exist_ok=True)

outputs = [
    (village,        OUT_VILLAGE),
    (parcels,        OUT_PARCELS),
    (footprints_out, OUT_FOOTPRINTS),
    (historic,       OUT_HISTORIC),
]
for gdf, path in outputs:
    try:
        gdf.to_file(path, driver="GeoJSON")
        print(f"  wrote {path}")
    except PermissionError:
        sys.exit(
            f"ERROR: cannot write {path} – the file is locked.\n"
            "Close any GeoJSON tabs open in VS Code and re-run."
        )

# ── Step 8: Refresh manual-overrides template ────────────────────────────────

print("Step 8 – refreshing manual overrides template")

# Preserve any values the user has already entered
_user_data = {}
if os.path.exists(OVERRIDES_CSV):
    with open(OVERRIDES_CSV, newline="", encoding="utf-8-sig") as _f:
        for row in _csv.DictReader(_f):
            _user_data[row["SWIS_SBL_ID"]] = {
                "YR_BLT_OVERRIDE":    row.get("YR_BLT_OVERRIDE", ""),
                "STYLE_OVERRIDE":     row.get("STYLE_OVERRIDE", ""),
                "HOUSE_NAME":         row.get("HOUSE_NAME", ""),
                "EXEMPLARY":          row.get("EXEMPLARY", ""),
                "PARCEL_ADDR_OVERRIDE": row.get("PARCEL_ADDR_OVERRIDE", ""),
            }

# Spatial join: find which parcels fall inside a historic nomination polygon
_historic_area = {}
if not historic.empty:
    _hj = gpd.sjoin(
        parcels[["geometry", "SWIS_SBL_ID"]],
        historic[["geometry", "HistoricName"]],
        how="left", predicate="intersects",
    )
    _historic_area = (
        _hj[_hj["HistoricName"].notna()]
        .drop_duplicates("SWIS_SBL_ID")
        .set_index("SWIS_SBL_ID")["HistoricName"]
        .to_dict()
    )

# Build rows and sort by street then house number
_rows = []
for _, p in parcels.iterrows():
    sbl  = p["SWIS_SBL_ID"]
    addr = p.get("PARCEL_ADDR") or ""
    ud   = _user_data.get(sbl, {})
    _rows.append({
        "SWIS_SBL_ID":     sbl,
        "PARCEL_ADDR":     addr,
        "LOC_ZIP":         p.get("LOC_ZIP") or "",
        "PROP_CLASS":      p.get("PROP_CLASS") or "",
        "YR_BLT":          "" if (p.get("YR_BLT") is None or p.get("YR_BLT") != p.get("YR_BLT")) else int(p["YR_BLT"]),
        "BLDG_STYLE_DESC": p.get("BLDG_STYLE_DESC") or "",
        "LOT_ACRES":       p.get("LOT_ACRES") or "",
        "HISTORIC_AREA":   _historic_area.get(sbl, ""),
        "YR_BLT_OVERRIDE":    ud.get("YR_BLT_OVERRIDE", ""),
        "STYLE_OVERRIDE":     ud.get("STYLE_OVERRIDE", ""),
        "HOUSE_NAME":         ud.get("HOUSE_NAME", ""),
        "EXEMPLARY":          ud.get("EXEMPLARY", ""),
        "PARCEL_ADDR_OVERRIDE": ud.get("PARCEL_ADDR_OVERRIDE", ""),
    })

def _sort_key(r):
    parts = str(r["PARCEL_ADDR"] or "").split()
    street = " ".join(parts[1:]).upper() if len(parts) > 1 else (parts[0].upper() if parts else "")
    try:
        num = int(parts[0]) if parts else 0
    except ValueError:
        num = 0
    return (street, num)

_rows.sort(key=_sort_key)

_COLS = ["SWIS_SBL_ID", "PARCEL_ADDR", "LOC_ZIP", "PROP_CLASS", "YR_BLT", "BLDG_STYLE_DESC",
         "LOT_ACRES", "HISTORIC_AREA",
         "YR_BLT_OVERRIDE", "STYLE_OVERRIDE", "HOUSE_NAME", "EXEMPLARY", "PARCEL_ADDR_OVERRIDE"]

with open(OVERRIDES_CSV, "w", newline="", encoding="utf-8") as _f:
    w = _csv.DictWriter(_f, fieldnames=_COLS)
    w.writeheader()
    w.writerows(_rows)

_preserved = sum(1 for d in _user_data.values() if any(d.values()))
print(f"  wrote {OVERRIDES_CSV} ({len(_rows)} rows, {_preserved} with user data preserved)")

_make_img_dirs(OVERRIDES_CSV)

print("\nDone.")
print(f"  village    -> {OUT_VILLAGE}")
print(f"  parcels    -> {OUT_PARCELS}")
print(f"  footprints -> {OUT_FOOTPRINTS}")
print(f"  historic   -> {OUT_HISTORIC}")
