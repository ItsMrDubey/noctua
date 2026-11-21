"""Update dashboard for new unfiltered ADS-B data format."""
with open('dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Update flights stats bar
content = content.replace(
    '<div class="stat-label">Interest Countries</div><div class="stat-value amber" id="sfl-interest">--</div>',
    '<div class="stat-label">Total in ME</div><div class="stat-value amber" id="sfl-total-me">--</div>'
)

# Update flights table header
content = content.replace(
    '<th>Class</th><th>Callsign</th><th>Country</th>',
    '<th>Class</th><th>Callsign</th><th>Aircraft</th>'
)

# Replace loadFlights function
old_fn_start = "// ── FLIGHTS ──\nasync function loadFlights() {"
old_fn_end = "document.getElementById('flights-status').style.display = 'block';\n  }\n}"

# Find and replace the entire loadFlights function
idx_start = content.index("// ── FLIGHTS ──")
idx_end = content.index("// ── SHIPS ──")
old_flights_fn = content[idx_start:idx_end]

new_flights_fn = """// ── FLIGHTS ──
async function loadFlights() {
  try {
    const r = await fetch('flights.json?t=' + Date.now());
    if (!r.ok) throw new Error('No data');
    const d = await r.json();

    document.getElementById('tab-flights-count').textContent = d.military || 0;
    document.getElementById('sfl-mil').textContent = d.military || 0;
    document.getElementById('sfl-total-me').textContent = d.total;
    document.getElementById('sfl-total').textContent = d.total;
    document.getElementById('sfl-updated').textContent = d.updated;

    let html = '';
    d.flights.forEach((f, i) => {
      const rc = f.is_mil ? 'flight-mil' : 'flight-civil';
      const alt = f.alt_ft !== null ? f.alt_ft.toLocaleString() + ' ft' : 'GND';
      const spd = f.speed_kts !== null ? f.speed_kts + ' kts' : '--';
      const sq = f.squawk === '7700' ? `<span class="badge badge-red">7700 EMER</span>` :
                 f.squawk === '7600' ? `<span class="badge badge-amber">7600 COMMS</span>` :
                 f.squawk === '7500' ? `<span class="badge badge-red">7500 HIJACK</span>` :
                 `<span class="dim">${esc(f.squawk)}</span>`;
      const vert = f.vertical.includes('Climbing') ? `<span style="color:var(--green)">${esc(f.vertical)}</span>` :
                   f.vertical.includes('Descending') ? `<span style="color:var(--amber)">${esc(f.vertical)}</span>` :
                   `<span class="dim">${esc(f.vertical)}</span>`;

      let classBadge, csColor, acInfo;
      if (f.is_mil) {
        const role = f.mil_role || f.operator || 'MILITARY';
        classBadge = `<span class="badge badge-red">${esc(role)}</span>`;
        csColor = 'var(--red)';
        acInfo = f.desc || f.type || 'Unknown';
      } else {
        classBadge = `<span class="dim">CIV</span>`;
        csColor = 'var(--text-dim)';
        acInfo = f.desc || f.type || '--';
      }

      html += `<tr class="${rc}" style="animation-delay:${i*0.015}s">
        <td>${classBadge}</td>
        <td style="color:${csColor};font-weight:bold">${esc(f.callsign)}</td>
        <td class="label-text" title="${esc(acInfo)}">${esc(acInfo).substring(0,25)}</td>
        <td>${alt}</td><td>${spd}</td><td class="label-text">${esc(f.heading)}</td>
        <td>${vert}</td><td>${sq}</td>
      </tr>`;
    });
    document.getElementById('flights-tbody').innerHTML = html;
    document.getElementById('flights-status').style.display = d.flights.length ? 'none' : 'block';
  } catch(e) {
    document.getElementById('flights-status').className = 'status-message error';
    document.getElementById('flights-status').innerHTML = 'FLIGHT FEED UNAVAILABLE';
    document.getElementById('flights-status').style.display = 'block';
  }
}

"""

content = content[:idx_start] + new_flights_fn + content[idx_end:]

with open('dashboard.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('Dashboard updated for unfiltered ADS-B data')
