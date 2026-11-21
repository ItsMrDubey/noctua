"""Add high-priority sound alert to dashboard."""
with open('dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Add alert banner HTML before the flights panel
alert_html = """
<!-- ── ALERT BANNER ── -->
<div id="alert-banner" style="display:none; position:fixed; top:60px; left:50%; transform:translateX(-50%);
  z-index:9998; background:rgba(255,0,0,0.15); border:1px solid #ff1a1a; border-radius:6px;
  padding:10px 24px; font-family:'Chakra Petch',sans-serif; font-size:13px; letter-spacing:2px;
  color:#ff4444; backdrop-filter:blur(8px); animation:alert-pulse 1s ease-in-out infinite;
  cursor:pointer; max-width:90vw;" onclick="dismissAlert()">
  <span id="alert-text"></span>
  <span style="margin-left:12px;color:#666;font-size:10px;">CLICK TO DISMISS</span>
</div>
<style>
  @keyframes alert-pulse {
    0%,100% { box-shadow: 0 0 10px rgba(255,0,0,0.3); }
    50% { box-shadow: 0 0 25px rgba(255,0,0,0.6); }
  }
</style>

"""

content = content.replace('<!-- ── FIRES PANEL ── -->', alert_html + '<!-- ── FIRES PANEL ── -->')

# Add alert sound + tracking JS before the closing </script>
alert_js = """
// ── HIGH PRIORITY ALERT SYSTEM ──
let knownHighPriority = new Set();
let alertActive = false;
let audioCtx = null;

function playAlertSound() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    // Three-tone military alert
    const times = [0, 0.15, 0.3];
    const freqs = [880, 1100, 880];
    times.forEach((t, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.type = 'square';
      osc.frequency.value = freqs[i];
      gain.gain.setValueAtTime(0.15, audioCtx.currentTime + t);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + t + 0.12);
      osc.start(audioCtx.currentTime + t);
      osc.stop(audioCtx.currentTime + t + 0.12);
    });
    // Second burst after pause
    setTimeout(() => {
      times.forEach((t, i) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.type = 'square';
        osc.frequency.value = freqs[i];
        gain.gain.setValueAtTime(0.15, audioCtx.currentTime + t);
        gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + t + 0.12);
        osc.start(audioCtx.currentTime + t);
        osc.stop(audioCtx.currentTime + t + 0.12);
      });
    }, 600);
  } catch(e) {}
}

function checkHighPriorityAlerts(flights) {
  const newAlerts = [];
  flights.forEach(f => {
    if (f.high_priority && !knownHighPriority.has(f.hex)) {
      knownHighPriority.add(f.hex);
      newAlerts.push(f);
    }
  });

  // Clean up aircraft that left
  const currentHexes = new Set(flights.filter(f => f.high_priority).map(f => f.hex));
  for (const hex of knownHighPriority) {
    if (!currentHexes.has(hex)) knownHighPriority.delete(hex);
  }

  if (newAlerts.length > 0) {
    playAlertSound();
    const names = newAlerts.map(f => {
      const name = f.desc || f.type || 'UNKNOWN';
      return `${f.callsign} (${name})`;
    }).join(' | ');
    const banner = document.getElementById('alert-banner');
    document.getElementById('alert-text').textContent =
      'HIGH PRIORITY CONTACT: ' + names;
    banner.style.display = 'block';
    alertActive = true;

    // Auto-switch to flights tab
    switchTab('flights');
  }
}

function dismissAlert() {
  document.getElementById('alert-banner').style.display = 'none';
  alertActive = false;
}

"""

content = content.replace('// Initial load\n', alert_js + '// Initial load\n')

# Hook checkHighPriorityAlerts into loadFlights
content = content.replace(
    "document.getElementById('flights-status').style.display = d.flights.length ? 'none' : 'block';\n  } catch(e) {",
    "document.getElementById('flights-status').style.display = d.flights.length ? 'none' : 'block';\n    checkHighPriorityAlerts(d.flights);\n  } catch(e) {"
)

with open('dashboard.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('Added high-priority sound alert system')
