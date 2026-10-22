#!/usr/bin/env python3
"""
make_img_dirs.py  –  Create web/img/<address>/ folders for each exemplary building.

Reads source-data/manual_overrides.csv and makes one subdirectory per row
where EXEMPLARY is set.  Run this whenever you add or rename exemplary buildings.

Usage:
    python make_img_dirs.py
"""

import csv
import os
import re

OVERRIDES_CSV = "source-data/manual_overrides.csv"
IMG_ROOT      = "web/img"


def slugify(addr: str) -> str:
    """Convert a street address to a safe directory name, e.g. '57 Fifty Acre Rd'→'57_fifty_acre_rd'."""
    slug = addr.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug


def main():
    os.makedirs(IMG_ROOT, exist_ok=True)

    with open(OVERRIDES_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    exemplary = [r for r in rows if r.get("EXEMPLARY", "").strip()]
    print(f"{len(exemplary)} exemplary buildings found")

    created = 0
    for row in exemplary:
        addr = (row.get("PARCEL_ADDR_OVERRIDE") or row.get("PARCEL_ADDR") or "").strip()
        if not addr:
            print(f"  skipping row with no address (SBL {row.get('SWIS_SBL_ID','')})")
            continue
        slug = slugify(addr)
        path = os.path.join(IMG_ROOT, slug)
        if not os.path.exists(path):
            os.makedirs(path)
            print(f"  created  {path}")
            created += 1
        else:
            print(f"  exists   {path}")

    print(f"\nDone. {created} new folder(s) created under {IMG_ROOT}/")


if __name__ == "__main__":
    main()
