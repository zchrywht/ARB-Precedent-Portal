#!/usr/bin/env python3
"""
fetch_attom.py  –  Backfill missing YR_BLT values using the ATTOM property API.

Reads source-data/parcels_hoh.geojson, queries ATTOM for each parcel that has
a street number but no recorded year-built, and writes the results to
source-data/attom_yr_blt.csv.

prep_hoh.py reads that CSV as a supplemental backfill step, so this script
only needs to be re-run when fresh ATTOM data is needed.

Usage:
    python fetch_attom.py --api-key YOUR_KEY
    python fetch_attom.py              # reads ATTOM_API_KEY environment variable

The script resumes automatically if interrupted — already-fetched IDs in
the CSV are skipped.
"""

import argparse
import csv
import os
import time

import requests

ATTOM_URL  = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/detail"
PARCELS_IN = "source-data/parcels_hoh.geojson"
OUT_CSV    = "source-data/attom_yr_blt.csv"

# Pause between requests — keep well under ATTOM's rate limits
REQUEST_DELAY = 0.4  # seconds


def lookup(address1: str, address2: str, api_key: str) -> int | None:
    """Return year_built from ATTOM for the given address, or None on no match."""
    try:
        r = requests.get(
            ATTOM_URL,
            headers={
                "accept": "application/json",
                "apikey": api_key,
            },
            params={"address1": address1, "address2": address2},
            timeout=15,
        )
        if r.status_code in (404, 400):
            return None
        if r.status_code == 401:
            # Print once to help diagnose auth issues
            try:
                msg = r.json().get("status", {}).get("msg", r.text[:200])
            except Exception:
                msg = r.text[:200]
            print(f"\n  AUTH ERROR: {msg}")
            print("  Check that your API key is correct and the trial is activated.")
            return None
        r.raise_for_status()
        props = r.json().get("property", [])
        if not props:
            return None
        yr = props[0].get("building", {}).get("yearbuilt")
        return int(yr) if yr else None
    except requests.HTTPError as e:
        print(f"    HTTP error: {e}")
        return None
    except Exception as e:
        print(f"    error: {e}")
        return None


def main():
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ATTOM_API_KEY", ""),
        help="ATTOM API key (or set ATTOM_API_KEY env var)",
    )
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit(
            "ERROR: provide --api-key YOUR_KEY  or set the ATTOM_API_KEY environment variable"
        )

    # ── Load parcels ──────────────────────────────────────────────────────────
    with open(PARCELS_IN) as f:
        features = json.load(f)["features"]

    missing = [
        p
        for feat in features
        for p in [feat["properties"]]
        if not p.get("YR_BLT") and p.get("LOC_ST_NBR")
    ]
    print(f"{len(missing)} parcels with a street number and no YR_BLT")

    # ── Resume from existing CSV ──────────────────────────────────────────────
    cached: dict[str, int | None] = {}
    if os.path.exists(OUT_CSV):
        with open(OUT_CSV, newline="") as f:
            for row in csv.DictReader(f):
                cached[row["SWIS_SBL_ID"]] = int(row["YR_BLT"]) if row["YR_BLT"] else None
        hits = sum(1 for v in cached.values() if v)
        print(f"  {len(cached)} already cached ({hits} with a year), resuming…")

    to_fetch = [p for p in missing if p["SWIS_SBL_ID"] not in cached]
    print(f"  {len(to_fetch)} to fetch from ATTOM")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    results = dict(cached)
    new_hits = 0
    for i, p in enumerate(to_fetch, 1):
        sbl  = p["SWIS_SBL_ID"]
        addr = f"{p['LOC_ST_NBR']} {p['LOC_STREET']}"
        zip_ = p.get("LOC_ZIP") or ""
        a2   = f"NY {zip_}".strip()

        print(f"  [{i}/{len(to_fetch)}] {sbl}  {addr}, {a2} … ", end="", flush=True)
        yr = lookup(addr, a2, args.api_key)
        results[sbl] = yr
        if yr:
            new_hits += 1
            print(yr)
        else:
            print("—")

        time.sleep(REQUEST_DELAY)

    # ── Write CSV ─────────────────────────────────────────────────────────────
    os.makedirs("source-data", exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["SWIS_SBL_ID", "YR_BLT"])
        for sbl, yr in sorted(results.items()):
            writer.writerow([sbl, yr if yr else ""])

    total_hits = sum(1 for v in results.values() if v)
    print(f"\nDone.  {new_hits} new values found this run, {total_hits} total → {OUT_CSV}")
    print("Re-run prep_hoh.py to bake the new values into the map data.")


if __name__ == "__main__":
    main()
