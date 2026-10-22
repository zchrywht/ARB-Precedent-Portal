#!/usr/bin/env python3
"""
viz_dates.py  –  Histogram of building construction dates in 5-year bins.

Reads source-data/parcels_hoh.geojson and plots year-built frequency.
Bars are colored to match the era palette used on the web map.

Usage:
    python viz_dates.py
    python viz_dates.py --footprints   # use footprints (one bar per building)
"""

import argparse
import json
from collections import Counter

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── Era palette (matches map.js ERA_MAP) ──────────────────────────────────────
ERAS = [
    (1,    1880, '#5c1e08', 'Pre-1880'),
    (1880, 1910, '#8b3b16', '1880 – 1909'),
    (1910, 1950, '#b65e27', '1910 – 1949'),
    (1950, 1980, '#ce834a', '1950 – 1979'),
    (1980, 2000, '#d4aa7f', '1980 – 1999'),
    (2000, 9999, '#dfcaaf', '2000 and later'),
]

def era_color(year):
    for start, end, color, _ in ERAS:
        if start <= year < end:
            return color
    return '#d1d5db'

def bin5(year):
    return (year // 10) * 10

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--footprints', action='store_true',
                        help='Use footprints GeoJSON instead of parcels')
    args = parser.parse_args()

    path = 'source-data/footprints_hoh.geojson' if args.footprints \
           else 'source-data/parcels_hoh.geojson'
    label = 'buildings' if args.footprints else 'parcels'

    with open(path) as f:
        features = json.load(f)['features']

    years = [
        int(p['YR_BLT'])
        for feat in features
        for p in [feat['properties']]
        if p.get('YR_BLT') and p['YR_BLT'] > 0
    ]

    if not years:
        print('No year-built data found.')
        return

    bins = Counter(bin5(y) for y in years)
    xs   = sorted(bins)
    ys   = [bins[x] for x in xs]
    clrs = [era_color(x) for x in xs]

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor('#f8f7f4')
    ax.set_facecolor('#f8f7f4')

    bars = ax.bar(xs, ys, width=8.5, color=clrs, edgecolor='white', linewidth=0.6)

    # Count labels on bars
    for bar, count in zip(bars, ys):
        if count > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.15,
                    str(count), ha='center', va='bottom', fontsize=7.5, color='#334155')

    # Era legend
    legend_patches = [mpatches.Patch(color=c, label=l) for _, _, c, l in ERAS]
    ax.legend(handles=legend_patches, loc='upper left', fontsize=8,
              framealpha=0.8, edgecolor='#e2e8f0')

    ax.set_xlabel('Year built (decade)', labelpad=8, color='#334155')
    ax.set_ylabel(f'Number of {label}', labelpad=8, color='#334155')
    ax.set_title('Village of Head of the Harbor — Construction dates',
                 fontsize=13, fontweight='bold', color='#1e293b', pad=12)

    ax.set_xticks(xs)
    ax.set_xticklabels([f'{x}s' for x in xs], rotation=45, ha='right')
    ax.tick_params(colors='#64748b', labelsize=8)
    ax.spines[['top', 'right']].set_visible(False)
    ax.spines[['left', 'bottom']].set_color('#e2e8f0')
    ax.yaxis.grid(True, color='#e2e8f0', linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)

    total   = len(years)
    missing = sum(1 for feat in features
                  if not feat['properties'].get('YR_BLT')
                  or feat['properties']['YR_BLT'] == 0)
    ax.text(0.99, 0.97,
            f'{total} {label} with year data  ·  {missing} without',
            transform=ax.transAxes, ha='right', va='top',
            fontsize=8, color='#94a3b8')

    plt.tight_layout()
    plt.show()

if __name__ == '__main__':
    main()
