"""Add seismic, bases, and correlations tabs to dashboard."""
with open('dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add new tabs
old_tabs = """  <div class="tab" data-tab="ships" onclick="switchTab('ships')">
    SHIPS <span class="tab-count" id="tab-ships-count">--</span>
  </div>
</div>"""

new_tabs = """  <div class="tab" data-tab="ships" onclick="switchTab('ships')">
    SHIPS <span class="tab-count" id="tab-ships-count">--</span>
  </div>
  <div class="tab" data-tab="seismic" onclick="switchTab('seismic')">
    SEISMIC <span class="tab-count" id="tab-seismic-count">--</span>
  </div>
  <div class="tab" data-tab="intel" onclick="switchTab('intel')">
    INTEL <span class="tab-count" id="tab-intel-count">--</span>
  </div>
  <div class="tab" data-tab="bases" onclick="switchTab('bases')">
    BASES <span class="tab-count" id="tab-bases-count">--</span>
  </div>
</div>"""

content = content.replace(old_tabs, new_tabs)

# 2. Add tab styling for new tabs
content = content.replace(
    ".tab[data-tab=\"ships\"].active { border-bottom-color: var(--blue); }",
    """.tab[data-tab="ships"].active { border-bottom-color: var(--blue); }
  .tab[data-tab="seismic"].active { border-bottom-color: var(--amber); }
  .tab[data-tab="intel"].active { border-bottom-color: var(--cyan); }
  .tab[data-tab="bases"].active { border-bottom-color: var(--green); }
  .tab[data-tab="seismic"].active .tab-count { background: var(--amber-dim); color: var(--amber); }
  .tab[data-tab="intel"].active .tab-count { background: rgba(0,221,255,0.15); color: var(--cyan); }
  .tab[data-tab="bases"].active .tab-count { background: var(--green-dim); color: var(--green); }"""
)

# 3. Add new panels HTML after ships panel
ships_panel_end = """  <div class="status-message" id="ships-status">INITIALIZING<div class="loading-bar"></div></div>
</div>"""

new_panels = """  <div class="status-message" id="ships-status">INITIALIZING<div class="loading-bar"></div></div>
</div>

<!-- ── SEISMIC PANEL ── -->
<div class="panel" id="panel-seismic">
  <div class="stats-bar">
    <div class="stat-box"><div class="stat-label">Events (24h)</div><div class="stat-value amber" id="ss-events">--</div></div>
    <div class="stat-box"><div class="stat-label">Explosions</div><div class="stat-value red" id="ss-explosions">--</div></div>
    <div class="stat-box"><div class="stat-label">Max Magnitude</div><div class="stat-value amber" id="ss-maxmag">--</div></div>
    <div class="stat-box"><div class="stat-label">Last Updated</div><div class="stat-value green small" id="ss2-updated">--</div></div>
  </div>
  <table class="data-table">
    <thead><tr>
      <th>Type</th><th>Time (IST)</th><th>Location</th><th>Mag</th><th>Depth</th><th>Felt</th>
    </tr></thead>
    <tbody id="seismic-tbody"></tbody>
  </table>
  <div class="status-message" id="seismic-status">NO SEISMIC EVENTS IN REGION</div>
</div>

<!-- ── INTEL (CORRELATIONS) PANEL ── -->
<div class="panel" id="panel-intel">
  <div class="stats-bar">
    <div class="stat-box"><div class="stat-label">Fire-Flight Links</div><div class="stat-value red" id="si-ff">--</div></div>
    <div class="stat-box"><div class="stat-label">Seismic-Fire Links</div><div class="stat-value amber" id="si-sf">--</div></div>
    <div class="stat-box"><div class="stat-label">Flight Patterns</div><div class="stat-value cyan" id="si-fp">--</div></div>
    <div class="stat-box"><div class="stat-label">Last Analysis</div><div class="stat-value green small" id="si-updated">--</div></div>
  </div>
  <div id="intel-content" style="padding:16px;position:relative;z-index:10;"></div>
  <div class="status-message" id="intel-status">ANALYZING DATA<div class="loading-bar"></div></div>
</div>

<!-- ── BASES PANEL ── -->
<div class="panel" id="panel-bases">
  <div class="stats-bar">
    <div class="stat-box"><div class="stat-label">Total Facilities</div><div class="stat-value green" id="sb-total">--</div></div>
    <div class="stat-box"><div class="stat-label">US Bases</div><div class="stat-value blue" id="sb-us">--</div></div>
    <div class="stat-box"><div class="stat-label">Russian</div><div class="stat-value red" id="sb-ru">--</div></div>
    <div class="stat-box"><div class="stat-label">Iranian</div><div class="stat-value green" id="sb-ir">--</div></div>
  </div>
  <table class="data-table">
    <thead><tr>
      <th>Name</th><th>Type</th><th>Country</th><th>Operator</th><th>Notes</th>
    </tr></thead>
    <tbody id="bases-tbody"></tbody>
  </table>
  <div class="status-message" id="bases-status">LOADING<div class="loading-bar"></div></div>
</div>"""

content = content.replace(ships_panel_end, new_panels)

# 4. Update refresh config and add new load functions
old_refresh = "const REFRESH = { fires: 60, flights: 15, ships: 30 };"
new_refresh = "const REFRESH = { fires: 60, flights: 15, ships: 30, seismic: 120, intel: 60, bases: 3600 };"
content = content.replace(old_refresh, new_refresh)

old_countdowns = "let countdowns = { fires: REFRESH.fires, flights: REFRESH.flights, ships: REFRESH.ships };"
new_countdowns = "let countdowns = { fires: REFRESH.fires, flights: REFRESH.flights, ships: REFRESH.ships, seismic: REFRESH.seismic, intel: REFRESH.intel, bases: REFRESH.bases };"
content = content.replace(old_countdowns, new_countdowns)

# 5. Add load functions before "// Initial load"
new_load_fns = """
// ── SEISMIC ──
async function loadSeismic() {
  try {
    const r = await fetch('seismic.json?t=' + Date.now());
    if (!r.ok) throw new Error('No data');
    const d = await r.json();
    document.getElementById('tab-seismic-count').textContent = d.total;
    document.getElementById('ss-events').textContent = d.total;
    document.getElementById('ss2-updated').textContent = d.updated;
    let explosions = 0, maxMag = 0;
    (d.events||[]).forEach(e => {
      if (e.type && e.type.toLowerCase().includes('explosion')) explosions++;
      if (e.magnitude > maxMag) maxMag = e.magnitude;
    });
    document.getElementById('ss-explosions').textContent = explosions;
    document.getElementById('ss-maxmag').textContent = maxMag > 0 ? maxMag.toFixed(1) : '--';
    let html = '';
    (d.events||[]).forEach((e, i) => {
      const isExplosion = e.type && e.type.toLowerCase().includes('explosion');
      const typeBadge = isExplosion ? `<span class="badge badge-red">EXPLOSION</span>` :
                        `<span class="badge badge-amber">${esc(e.type||'earthquake')}</span>`;
      html += `<tr style="animation-delay:${i*0.02}s">
        <td>${typeBadge}</td><td>${esc(e.time_ist)}</td><td class="label-text">${esc(e.place)}</td>
        <td style="color:var(--amber);font-weight:bold">${e.magnitude}</td>
        <td class="dim">${e.depth_km} km</td><td class="dim">${e.felt_reports||0}</td></tr>`;
    });
    document.getElementById('seismic-tbody').innerHTML = html;
    document.getElementById('seismic-status').style.display = (d.events||[]).length ? 'none' : 'block';
  } catch(e) {
    document.getElementById('seismic-status').textContent = 'SEISMIC FEED UNAVAILABLE';
    document.getElementById('seismic-status').style.display = 'block';
  }
}

// ── INTEL (CORRELATIONS) ──
async function loadIntel() {
  try {
    const r = await fetch('correlations.json?t=' + Date.now());
    if (!r.ok) throw new Error('No data');
    const d = await r.json();
    const ff = d.fire_flight || [];
    const sf = d.seismic_fire || [];
    const fp = Object.values(d.flight_patterns || {});
    document.getElementById('tab-intel-count').textContent = ff.length + sf.length + fp.length;
    document.getElementById('si-ff').textContent = ff.length;
    document.getElementById('si-sf').textContent = sf.length;
    document.getElementById('si-fp').textContent = fp.length;
    document.getElementById('si-updated').textContent = d.timestamp;
    let html = '';
    // Flight patterns
    fp.forEach(p => {
      const patternColor = p.pattern === 'orbiting' ? 'var(--red)' :
                          p.pattern === 'racetrack' ? 'var(--amber)' :
                          p.pattern === 'descending' ? 'var(--amber)' :
                          p.pattern === 'loitering' ? 'var(--cyan)' : 'var(--text-dim)';
      html += `<div style="background:var(--bg-card);border:1px solid var(--border);border-left:3px solid ${patternColor};border-radius:4px;padding:12px 16px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span style="color:${patternColor};font-family:'Chakra Petch',sans-serif;font-weight:700;font-size:13px;letter-spacing:1px;">${esc(p.pattern.toUpperCase())}</span>
          <span class="badge badge-red">${esc(p.callsign)} (${esc(p.type||'')})</span>
        </div>
        <div style="color:var(--text-label);font-size:11px;margin-top:6px;">${esc(p.desc||'')}</div>
        <div style="color:var(--text-dim);font-size:11px;margin-top:4px;">${esc(p.details?.assessment||'')}</div>
      </div>`;
    });
    // Fire-flight correlations
    ff.forEach(c => {
      const flights = c.nearby_military.map(f =>
        `${esc(f.callsign)} (${esc(f.desc||f.type||'')}) - ${f.min_distance_nm}NM`
      ).join('<br>');
      html += `<div style="background:var(--bg-card);border:1px solid var(--border);border-left:3px solid var(--red);border-radius:4px;padding:12px 16px;margin-bottom:8px;">
        <div style="color:var(--red);font-family:'Chakra Petch',sans-serif;font-weight:700;font-size:13px;letter-spacing:1px;">FIRE + MILITARY AIRCRAFT</div>
        <div style="color:var(--text-label);font-size:11px;margin-top:6px;">Fire at ${c.fire_lat.toFixed(3)}, ${c.fire_lon.toFixed(3)} (FRP: ${c.max_frp} MW, ${c.fire_detections} detections)</div>
        <div style="color:var(--amber);font-size:11px;margin-top:4px;">Nearby military: ${flights}</div>
        <div style="color:var(--text-dim);font-size:10px;margin-top:4px;">Confidence: ${(c.confidence*100).toFixed(0)}%</div>
      </div>`;
    });
    // Seismic-fire
    sf.forEach(c => {
      html += `<div style="background:var(--bg-card);border:1px solid var(--border);border-left:3px solid var(--amber);border-radius:4px;padding:12px 16px;margin-bottom:8px;">
        <div style="color:var(--amber);font-family:'Chakra Petch',sans-serif;font-weight:700;font-size:13px;letter-spacing:1px;">SEISMIC + FIRE</div>
        <div style="color:var(--text-label);font-size:11px;margin-top:6px;">${esc(c.seismic_type||'Event')} M${c.magnitude} at ${esc(c.seismic_place||'')} with ${c.nearby_fires.length} fire(s) nearby</div>
        <div style="color:var(--text-dim);font-size:10px;margin-top:4px;">Confidence: ${(c.confidence*100).toFixed(0)}%</div>
      </div>`;
    });
    if (!html) html = '<div style="text-align:center;padding:40px;color:var(--text-dim);letter-spacing:2px;">COLLECTING DATA — CORRELATIONS APPEAR AS PATTERNS EMERGE</div>';
    document.getElementById('intel-content').innerHTML = html;
    document.getElementById('intel-status').style.display = 'none';
  } catch(e) {
    document.getElementById('intel-status').textContent = 'INTEL FEED INITIALIZING';
    document.getElementById('intel-status').style.display = 'block';
  }
}

// ── BASES ──
async function loadBases() {
  try {
    const r = await fetch('bases.json?t=' + Date.now());
    if (!r.ok) throw new Error('No data');
    const bases = await r.json();
    document.getElementById('tab-bases-count').textContent = bases.length;
    document.getElementById('sb-total').textContent = bases.length;
    document.getElementById('sb-us').textContent = bases.filter(b => b.operator && b.operator.includes('United States')).length;
    document.getElementById('sb-ru').textContent = bases.filter(b => b.operator && b.operator.includes('Russia')).length;
    document.getElementById('sb-ir').textContent = bases.filter(b => b.country === 'Iran').length;
    let html = '';
    bases.forEach((b, i) => {
      const opColor = (b.operator||'').includes('United States') ? 'var(--blue)' :
                      (b.operator||'').includes('Russia') ? 'var(--red)' :
                      b.country === 'Iran' ? 'var(--green)' :
                      b.country === 'Israel' ? '#fff' :
                      (b.operator||'').includes('United Kingdom') ? '#aa66ff' : 'var(--text-label)';
      html += `<tr style="animation-delay:${i*0.01}s">
        <td style="color:${opColor};font-weight:bold">${esc(b.name)}</td>
        <td><span class="badge badge-dim">${esc(b.type||'')}</span></td>
        <td>${esc(b.country||'')}</td><td class="label-text">${esc(b.operator||'')}</td>
        <td class="dim" style="white-space:normal;max-width:250px;font-size:10px;">${esc(b.notes||'')}</td>
      </tr>`;
    });
    document.getElementById('bases-tbody').innerHTML = html;
    document.getElementById('bases-status').style.display = bases.length ? 'none' : 'block';
  } catch(e) {
    document.getElementById('bases-status').textContent = 'BASES DATA UNAVAILABLE';
    document.getElementById('bases-status').style.display = 'block';
  }
}

"""

content = content.replace('\n// Initial load\n', new_load_fns + '\n// Initial load\n')

# 6. Update initial load and tick function
content = content.replace(
    'loadFires(); loadFlights(); loadShips();',
    'loadFires(); loadFlights(); loadShips(); loadSeismic(); loadIntel(); loadBases();'
)

content = content.replace(
    "else loadShips();\n    }\n  }",
    "else if (key === 'seismic') loadSeismic();\n      else if (key === 'intel') loadIntel();\n      else if (key === 'bases') loadBases();\n      else loadShips();\n    }\n  }"
)

with open('dashboard.html', 'w', encoding='utf-8') as f:
    f.write(content)
print('Dashboard updated with seismic, intel, and bases tabs')
