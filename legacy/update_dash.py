"""One-time script to update dashboard with military classification."""
with open('dashboard.html', 'r') as f:
    content = f.read()

# 1. Replace flights stats bar
content = content.replace(
    '<div class="stat-label">Aircraft Tracked</div><div class="stat-value amber" id="sfl-total">--</div>',
    '<div class="stat-label">Military / Govt</div><div class="stat-value red" id="sfl-mil">--</div>'
)
content = content.replace(
    '<div class="stat-label">Airborne</div><div class="stat-value amber" id="sfl-air">--</div>',
    '<div class="stat-label">Interest Countries</div><div class="stat-value amber" id="sfl-interest">--</div>'
)
content = content.replace(
    '<div class="stat-label">Countries</div><div class="stat-value green" id="sfl-countries">--</div>',
    '<div class="stat-label">Total Aircraft</div><div class="stat-value green" id="sfl-total">--</div>'
)

# 2. Replace flights table header
content = content.replace(
    '<th>Callsign</th><th>ICAO24</th><th>Country</th>',
    '<th>Class</th><th>Callsign</th><th>Country</th>'
)

# 3. Replace flight stripe styles
content = content.replace(
    '.flight-air td:first-child { border-left-color: var(--amber); }\n  .flight-ground td:first-child { border-left-color: var(--text-dim); }',
    '.flight-mil td:first-child { border-left-color: var(--red); }\n  .flight-mil { background: rgba(255,26,26,0.04) !important; }\n  .flight-interest td:first-child { border-left-color: var(--amber); }\n  .flight-civil td:first-child { border-left-color: transparent; }\n  .flight-ground td:first-child { border-left-color: var(--text-dim); }'
)

# 4. Replace loadFlights JS function
old_body = """    document.getElementById('tab-flights-count').textContent = d.total;
    document.getElementById('sfl-total').textContent = d.total;
    document.getElementById('sfl-updated').textContent = d.updated;

    let airborne = 0;
    const countries = new Set();
    d.flights.forEach(f => { if (!f.on_ground) airborne++; countries.add(f.country); });
    document.getElementById('sfl-air').textContent = airborne;
    document.getElementById('sfl-countries').textContent = countries.size;

    let html = '';
    d.flights.forEach((f, i) => {
      const rc = f.on_ground ? 'flight-ground' : 'flight-air';
      const alt = f.alt_ft !== null ? f.alt_ft.toLocaleString() + ' ft' : 'GND';
      const spd = f.speed_kts !== null ? f.speed_kts + ' kts' : '--';
      const sq = f.squawk === '7700' ? `<span class="badge badge-red">7700 EMER</span>` :
                 f.squawk === '7600' ? `<span class="badge badge-amber">7600 COMMS</span>` :
                 f.squawk === '7500' ? `<span class="badge badge-red">7500 HIJACK</span>` :
                 `<span class="dim">${esc(f.squawk)}</span>`;
      const vert = f.vertical.includes('Climbing') ? `<span style="color:var(--green)">${esc(f.vertical)}</span>` :
                   f.vertical.includes('Descending') ? `<span style="color:var(--amber)">${esc(f.vertical)}</span>` :
                   `<span class="dim">${esc(f.vertical)}</span>`;
      html += `<tr class="${rc}" style="animation-delay:${i*0.015}s">
        <td style="color:var(--amber);font-weight:bold">${esc(f.callsign)}</td>
        <td class="dim">${esc(f.icao24)}</td><td>${esc(f.country)}</td>
        <td>${alt}</td><td>${spd}</td><td class="label-text">${esc(f.heading)}</td>
        <td>${vert}</td><td>${sq}</td>
      </tr>`;
    });"""

new_body = """    document.getElementById('tab-flights-count').textContent = d.military || 0;
    document.getElementById('sfl-mil').textContent = d.military || 0;
    document.getElementById('sfl-interest').textContent = d.interest || 0;
    document.getElementById('sfl-total').textContent = d.total;
    document.getElementById('sfl-updated').textContent = d.updated;

    let html = '';
    d.flights.forEach((f, i) => {
      const rc = f.is_mil ? 'flight-mil' : f.priority === 1 ? 'flight-interest' : 'flight-civil';
      const alt = f.alt_ft !== null ? f.alt_ft.toLocaleString() + ' ft' : 'GND';
      const spd = f.speed_kts !== null ? f.speed_kts + ' kts' : '--';
      const sq = f.squawk === '7700' ? `<span class="badge badge-red">7700 EMER</span>` :
                 f.squawk === '7600' ? `<span class="badge badge-amber">7600 COMMS</span>` :
                 f.squawk === '7500' ? `<span class="badge badge-red">7500 HIJACK</span>` :
                 `<span class="dim">${esc(f.squawk)}</span>`;
      const vert = f.vertical.includes('Climbing') ? `<span style="color:var(--green)">${esc(f.vertical)}</span>` :
                   f.vertical.includes('Descending') ? `<span style="color:var(--amber)">${esc(f.vertical)}</span>` :
                   `<span class="dim">${esc(f.vertical)}</span>`;
      const classBadge = f.is_mil ? `<span class="badge badge-red">${esc(f.mil_tag)}</span>` :
                         f.priority === 1 ? `<span class="badge badge-amber">${esc(f.country)}</span>` :
                         `<span class="dim">CIV</span>`;
      const csColor = f.is_mil ? 'var(--red)' : f.priority === 1 ? 'var(--amber)' : 'var(--text-dim)';
      html += `<tr class="${rc}" style="animation-delay:${i*0.015}s">
        <td>${classBadge}</td>
        <td style="color:${csColor};font-weight:bold">${esc(f.callsign)}</td>
        <td>${esc(f.country)}</td>
        <td>${alt}</td><td>${spd}</td><td class="label-text">${esc(f.heading)}</td>
        <td>${vert}</td><td>${sq}</td>
      </tr>`;
    });"""

content = content.replace(old_body, new_body)

with open('dashboard.html', 'w') as f:
    f.write(content)

print('Dashboard updated with military classification')
