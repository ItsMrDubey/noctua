"""frontend/js/timefmt.js formats in UTC, a fixed IANA zone and across DST. Runs the module in
node (skipped without node); the browser-only parts (localStorage) are guarded in the module."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MODULE = (ROOT / "frontend" / "js" / "timefmt.js").as_uri()

SCRIPT = """
const t = await import(%s);
const out = {};
const summer = "2026-07-01T12:30:15Z", winter = "2026-01-15T12:30:15Z";
for (const z of ["UTC", "Asia/Kolkata", "Europe/London"]) {
  out[z] = {
    summer: t.fmtDateTime(summer, { tz: z, seconds: true }), winter: t.fmtDateTime(winter, { tz: z }),
    time: t.fmtTime(summer, { tz: z }), date: t.fmtDate("2026-07-01T23:00:00Z", { tz: z }),
    labelSummer: t.tzLabel(summer, z), labelWinter: t.tzLabel(winter, z),
    offSummer: t.tzOffset(summer, z), offWinter: t.tzOffset(winter, z),
  };
}
out.naive = t.fmtDateTime("2026-07-01T12:30:15", { tz: "UTC", seconds: true });
out.seconds = t.fmtTime(1782909015, { tz: "UTC", seconds: true });
out.ms = t.fmtTime(1782909015000, { tz: "UTC", seconds: true });
out.date = t.fmtTime(new Date(1782909015000), { tz: "UTC", seconds: true });
out.utc = t.utcTitle("2026-07-01T18:00:15+05:30");
out.bad = [t.fmtDateTime(null), t.fmtTime("nope"), t.utcTitle(undefined)];
out.short = t.fmtDateTime(summer, { tz: "Asia/Kolkata", short: true, zone: true });
out.html = t.timeHtml(summer, { tz: "Asia/Kolkata" });
out.defaultTz = t.getTz();
let seen = null;
const off = t.onTzChange((z) => { seen = z; });
t.setTz("Asia/Kolkata"); out.afterSet = [t.getTz(), seen, t.fmtTime(summer)];
t.setTz("Not/AZone"); out.invalid = t.getTz();
off(); t.setTz("UTC"); out.unsubscribed = seen;
out.zones = t.listZones().includes("Europe/London");
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def res():
    if NODE is None:
        pytest.skip("node not installed")
    r = subprocess.run(
        [NODE, "--input-type=module", "-e", SCRIPT % json.dumps(MODULE)],
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode("utf-8"))


def test_utc(res):
    u = res["UTC"]
    assert u["summer"] == "2026-07-01 12:30:15" and u["winter"] == "2026-01-15 12:30"
    assert u["labelSummer"] == u["labelWinter"] == "UTC" and u["offSummer"] == 0


def test_fixed_zone_kolkata(res):
    k = res["Asia/Kolkata"]
    assert k["summer"] == "2026-07-01 18:00:15" and k["winter"] == "2026-01-15 18:00"
    assert k["time"] == "18:00" and k["date"] == "2026-07-02"  # 23:00Z is the next day in IST
    assert k["offSummer"] == k["offWinter"] == 330 and k["labelSummer"].endswith("+05:30")


def test_dst_london(res):
    lo = res["Europe/London"]
    assert (
        lo["summer"] == "2026-07-01 13:30:15"
        and lo["offSummer"] == 60
        and lo["labelSummer"].endswith("+01:00")
    )
    assert (
        lo["winter"] == "2026-01-15 12:30"
        and lo["offWinter"] == 0
        and lo["labelWinter"].endswith("+00:00")
    )


def test_inputs_and_fallbacks(res):
    assert res["naive"] == "2026-07-01 12:30:15"  # zone-less ISO = UTC, not browser-local
    assert res["seconds"] == res["ms"] == res["date"] == "12:30:15"
    assert res["utc"] == "2026-07-01 12:30:15 UTC"
    assert res["bad"] == ["–", "–", ""]
    assert res["short"].startswith("07-01 18:00 ")
    assert (
        'datetime="2026-07-01T12:30:15.000Z"' in res["html"]
        and 'title="2026-07-01 12:30:15 UTC"' in res["html"]
    )
    assert ">2026-07-01 18:00<" in res["html"]


def test_preference(res):
    assert res["defaultTz"] == "local"
    assert res["afterSet"] == ["Asia/Kolkata", "Asia/Kolkata", "18:00"]
    assert res["invalid"] == "local"
    assert res["unsubscribed"] == "local"  # listener removed before the UTC change
    assert res["zones"]
