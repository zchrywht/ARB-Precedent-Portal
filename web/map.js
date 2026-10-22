// ═══════════════════════════════════════════════════════════════════════════
// CONFIG — edit this block to change map appearance
// ═══════════════════════════════════════════════════════════════════════════

const CONFIG = {
  // Base map style (Stadia Maps — swap URL for any MapLibre-compatible style)
  styleUrl: 'https://tiles.stadiamaps.com/styles/alidade_smooth.json',

  // GeoJSON data paths (relative to web/)
  data: {
    village:    '../source-data/village_hoh.geojson',
    parcels:    '../source-data/parcels_hoh.geojson',
    footprints: '../source-data/footprints_hoh.geojson',
    historic:   '../source-data/historic_hoh.geojson',
  },

  // Layer opacities
  opacity: {
    parcelFill:    0.38,   // lot background tint
    footprintFill: 1.00,   // building footprint fill
    villageMask:   0.50,   // white overlay outside village boundary
  },

  // Parcel lot-line color — default matches the lightest grey on alidade_smooth
  parcelOutlineColor: '#ffffff50',

  // Historic register overlay
  historic: {
    fillColor:    'rgba(0,0,0,0.05)',
    outlineColor: 'rgba(0,0,0,0.5)',
    outlineWidth: 1,
  },

  // Exemplary / style-reference buildings
  exemplary: {
    fillColor: 'rgba(0,0,0,0.85)',
  },
};

// ── Era palette ──────────────────────────────────────────────────────────────
// Edit colors here to change how year-built eras are shaded.
// Each entry: label shown in legend, hex fill color, first year of the era.
// The first entry (stop: null) is the fallback for buildings with no year data.
// ─────────────────────────────────────────────────────────────────────────────
const ERA_MAP = [
  { label: 'Not recorded',   color: '#d1d5db', stop: null },
  { label: 'Pre-1880',       color: '#5c1e08', stop: 1    },
  { label: '1880 – 1909',    color: '#8b3b16', stop: 1880 },
  { label: '1910 – 1949',    color: '#b65e27', stop: 1910 },
  { label: '1950 – 1979',    color: '#ce834a', stop: 1950 },
  { label: '1980 – 1999',    color: '#d4aa7f', stop: 1980 },
  { label: '2000 and later', color: '#dfcaaf', stop: 2000 },
];

// ═══════════════════════════════════════════════════════════════════════════
// Derived expressions — no need to edit below here for palette changes
// ═══════════════════════════════════════════════════════════════════════════

// MapLibre step expression: default → era[0].color, then one step per era
const ERA_COLOR_EXPR = [
  'step',
  ['coalesce', ['get', 'YR_BLT'], 0],
  ERA_MAP[0].color,
  ...ERA_MAP.slice(1).flatMap(e => [e.stop, e.color]),
];

// ── Helpers ───────────────────────────────────────────────────────────────────

// Build a "world minus village" polygon so a fill layer dims everything outside.
function makeMask(villageGJ) {
  const world = [[-180, -85.051129], [180, -85.051129], [180, 85.051129], [-180, 85.051129], [-180, -85.051129]];
  const geom  = villageGJ.features[0].geometry;
  const holes = geom.type === 'Polygon' ? geom.coordinates : geom.coordinates.flat(1);
  return { type: 'FeatureCollection', features: [
    { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [world, ...holes] } },
  ]};
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function load(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} loading ${path}`);
  return r.json();
}

Promise.all(Object.values(CONFIG.data).map(load))
  .then(([village, parcels, footprints, historic]) => {
    initMap(village, parcels, footprints, historic);
  })
  .catch(err => {
    document.getElementById('map').innerHTML =
      `<div style="padding:2rem;font:14px monospace;color:#ef4444;background:#fff">
        <strong>Error loading data</strong><br>${err.message}<br><br>
        Run <code>python prep_hoh.py</code> then serve from the project root.
      </div>`;
  });

// ── Map initialisation ────────────────────────────────────────────────────────

function initMap(village, parcels, footprints, historic) {
  // Fit map to village bounding box
  const coords = village.features[0].geometry.coordinates.flat(Infinity);
  const lons = [], lats = [];
  for (let i = 0; i < coords.length; i += 2) { lons.push(coords[i]); lats.push(coords[i + 1]); }
  const bounds = [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];

  const map = new maplibregl.Map({
    container: 'map',
    style: CONFIG.styleUrl,
    bounds,
    fitBoundsOptions: { padding: 56 },
    hash: true,
  });

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'imperial' }), 'bottom-right');

  map.on('load', () => {
    // Suppress the basemap's own building polygons to avoid bleed-through
    if (map.getLayer('building')) map.setLayoutProperty('building', 'visibility', 'none');

    // ── Sources ──────────────────────────────────────────────────────────────
    map.addSource('mask',       { type: 'geojson', data: makeMask(village) });
    map.addSource('parcels',    { type: 'geojson', data: parcels });
    map.addSource('footprints', { type: 'geojson', data: footprints, generateId: true });
    map.addSource('historic',   { type: 'geojson', data: historic });

    map.addLayer({
      id: 'village-mask', type: 'fill', source: 'mask',
      paint: { 'fill-color': '#ffffff', 'fill-opacity': CONFIG.opacity.villageMask },
    });

    map.addLayer({
      id: 'parcels-fill', type: 'fill', source: 'parcels',
      paint: { 'fill-color': ERA_COLOR_EXPR, 'fill-opacity': CONFIG.opacity.parcelFill },
    });
    map.addLayer({
      id: 'parcels-outline', type: 'line', source: 'parcels',
      paint: { 'line-color': CONFIG.parcelOutlineColor, 'line-width': 0.8 },
    });

    map.addLayer({
      id: 'footprints-fill', type: 'fill', source: 'footprints',
      paint: {
        'fill-color': ['case',
          ['!=', ['coalesce', ['get', 'EXEMPLARY'], ''], ''], CONFIG.exemplary.fillColor,
          ERA_COLOR_EXPR,
        ],
        'fill-opacity': CONFIG.opacity.footprintFill,
      },
    });
    map.addLayer({
      id: 'footprints-stroke', type: 'line', source: 'footprints',
      paint: { 'line-color': 'rgba(0,0,0,0.2)', 'line-width': 0.6 },
    });

    map.addLayer({
      id: 'footprints-hover', type: 'fill', source: 'footprints',
      paint: {
        'fill-color':   '#fff',
        'fill-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 0.35, 0],
      },
    });

    map.addLayer({
      id: 'historic-fill', type: 'fill', source: 'historic',
      paint: { 'fill-color': CONFIG.historic.fillColor },
    });
    map.addLayer({
      id: 'historic-outline', type: 'line', source: 'historic',
      paint: {
        'line-color': CONFIG.historic.outlineColor,
        'line-width': CONFIG.historic.outlineWidth,
      },
    });

    // ── Legend ────────────────────────────────────────────────────────────
    (function buildLegend() {
      let html = '<h3>Year built</h3>';
      ERA_MAP.slice(1).forEach(({ label, color }) => {
        html += `<div class="legend-row">
          <div class="swatch" style="background:${color}"></div>${label}
        </div>`;
      });
      html += `<hr class="legend-sep">
        <div class="legend-row">
          <div class="swatch" style="background:${ERA_MAP[0].color}"></div>Not recorded
        </div>
        <hr class="legend-sep">
        <div class="legend-row">
          <div class="swatch" style="background:${CONFIG.historic.fillColor};
               border:1px solid ${CONFIG.historic.outlineColor}"></div>Historic register
        </div>
        <div class="legend-row">
          <div class="swatch" style="background:${CONFIG.exemplary.fillColor}"></div>Style reference
        </div>`;
      document.getElementById('legend').innerHTML = html;
    })();

    // ── Style filter ─────────────────────────────────────────────────────
    const FILTER_LAYERS = [
      'parcels-fill', 'parcels-outline',
      'footprints-fill', 'footprints-stroke', 'footprints-hover',
    ];
    // Groups that fold multiple BLDG_STYLE_DESC values under one display label.
    // Add entries here to merge more styles without touching the source data.
    const STYLE_GROUPS = {
      'Ranch': ['Ranch', 'Raised ranch', 'Split level'],
    };

    // Reverse lookup: source value → group label
    const styleGroupOf = {};
    Object.entries(STYLE_GROUPS).forEach(([label, values]) =>
      values.forEach(v => { styleGroupOf[v] = label; })
    );

    // Count styles, rolling sub-types into their group label
    const styleCounts = {};
    footprints.features.forEach(f => {
      const s = f.properties.BLDG_STYLE_DESC;
      if (!s || s === 'Unknown') return;
      const label = styleGroupOf[s] ?? s;
      styleCounts[label] = (styleCounts[label] || 0) + 1;
    });

    // Build radio list sorted by count descending
    const styleEntries = Object.entries(styleCounts).sort((a, b) => b[1] - a[1]);

    let filterHtml = `<label class="toggle-row">
      <input type="radio" name="style-filter" value="" checked> All
    </label>`;
    styleEntries.forEach(([style, count]) => {
      filterHtml += `<label class="toggle-row">
        <input type="radio" name="style-filter" value="${style}">
        ${style}<span class="filter-count">${count}</span>
      </label>`;
    });
    document.getElementById('style-filters').innerHTML = filterHtml;

    document.getElementById('style-filters').addEventListener('change', e => {
      const val = e.target.value;
      let filter = null;
      if (val) {
        const values = STYLE_GROUPS[val] ?? [val];
        filter = ['in', ['coalesce', ['get', 'BLDG_STYLE_DESC'], ''], ['literal', values]];
      }
      FILTER_LAYERS.forEach(id => map.setFilter(id, filter));
    });

    // ── Layer toggles ─────────────────────────────────────────────────────
    const EXEMPLARY_FILTER = ['!=', ['coalesce', ['get', 'EXEMPLARY'], ''], ''];
    document.getElementById('tog-exemplary-only').addEventListener('change', function () {
      const f = this.checked ? EXEMPLARY_FILTER : null;
      ['parcels-fill', 'parcels-outline', 'footprints-fill',
       'footprints-stroke', 'footprints-hover'].forEach(id => map.setFilter(id, f));
    });

    // ── Hover highlight ───────────────────────────────────────────────────
    let hoveredId = null;

    map.on('mousemove', 'footprints-fill', e => {
      map.getCanvas().style.cursor = 'pointer';
      const id = e.features[0].id;
      if (hoveredId !== null && hoveredId !== id)
        map.setFeatureState({ source: 'footprints', id: hoveredId }, { hover: false });
      hoveredId = id;
      map.setFeatureState({ source: 'footprints', id }, { hover: true });
    });

    map.on('mouseleave', 'footprints-fill', () => {
      map.getCanvas().style.cursor = '';
      if (hoveredId !== null) {
        map.setFeatureState({ source: 'footprints', id: hoveredId }, { hover: false });
        hoveredId = null;
      }
    });

    // ── Click popup ───────────────────────────────────────────────────────
    const popup = new maplibregl.Popup({ closeButton: true, maxWidth: '280px' });

    map.on('click', 'footprints-fill', e => {
      const p = e.features[0].properties;
      const addr      = p.PARCEL_ADDR || 'N/A';
      const houseName = p.HOUSE_NAME  || '';
      const style = (p.BLDG_STYLE_DESC && p.BLDG_STYLE_DESC !== 'Unknown')
                      ? p.BLDG_STYLE_DESC : 'Not recorded';
      const yr    = p.YR_BLT && p.YR_BLT > 0 ? p.YR_BLT : 'Not recorded';
      const acres = p.LOT_ACRES != null ? `${p.LOT_ACRES} ac` : '—';

      const histFeats = map.queryRenderedFeatures(e.point, { layers: ['historic-fill'] });

      let html = `<div class="pop-title">${addr}</div>`;
      if (houseName) html += `<div class="pop-subtitle">${houseName}</div>`;
      html += popRow('Style',      style);
      html += popRow('Year built', yr);
      html += popRow('Lot size',   acres);
      if (p.PROP_CLASS) html += popRow('Class',  p.PROP_CLASS);
      if (p.PRINT_KEY)  html += popRow('Parcel', p.PRINT_KEY);

      if (histFeats.length > 0) {
        const h = histFeats[0].properties;
        html += `<div class="hist-badge">${h.register_type || 'Historic register'}</div>`;
        if (h.HistoricName)
          html += `<div style="margin-top:4px;font-size:11px;color:${CONFIG.historic.outlineColor}">
                     ${h.HistoricName}</div>`;
      }

      const addrQ  = encodeURIComponent(`${addr} Head of the Harbor NY`);
      const svUrl  = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${e.lngLat.lat},${e.lngLat.lng}`;
      html += `<div class="pop-links">
        <a href="https://www.google.com/search?q=${addrQ}" target="_blank" rel="noopener">Google Search</a>
        <a href="${svUrl}" target="_blank" rel="noopener">Street View</a>
      </div>`;

      popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
    });

    map.on('click', 'historic-fill', e => {
      if (map.queryRenderedFeatures(e.point, { layers: ['footprints-fill'] }).length > 0) return;
      const h = e.features[0].properties;
      let html = `<div class="pop-title">${h.HistoricName || 'Historic property'}</div>`;
      if (h.register_type) html += `<div class="hist-badge">${h.register_type}</div>`;
      if (h.DateNR)  html += popRow("Nat'l Register", h.DateNR);
      if (h.DateSR)  html += popRow('State Register',  h.DateSR);
      if (h.DateNHL) html += popRow('NHL date',        h.DateNHL);
      if (h.NRNum)   html += popRow('NR number',       h.NRNum);
      popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
    });
  });
}

function popRow(label, value) {
  return `<div class="pop-row">
    <span class="pop-label">${label}</span>
    <span class="pop-val">${value}</span>
  </div>`;
}
