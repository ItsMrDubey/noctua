"""Status vocabulary of sensor-derived events.

An event created by a sensor detector (GNSS interference, internet outage, emergency squawk,
radiation, military orbit / concentration, AIS gap, seismic explosion) is *detected* by that
sensor; the readings that made the detection are its evidence, not independent
corroboration. Such events carry status ``sensor-detected`` and a confidence from the
detection's own statistics. Official hazard alerts keep ``official``. Only independent
sources (news reports, a different sensor type) could corroborate a sensor event.
"""

from __future__ import annotations

SENSOR_DETECTED = "sensor-detected"
# detectors whose events are signals rather than something visible from orbit / on the ground
SIGNAL_DETECTORS = frozenset({"gnss_jam", "outage", "squawk", "radiation"})
# evidence kind each detector writes about its own detection
DETECTOR_KINDS = {"gnss_jam": {"gnss_cell"}, "outage": {"outage"}, "squawk": {"aircraft"},
                  "radiation": {"radiation"}, "orbit": {"aircraft"},
                  "concentration": {"aircraft"}, "ais_gap": {"ais_gap"},
                  "seismic": {"quake"}}
SENSOR_LABEL = {
    "gnss_jam": "ADS-B navigation integrity (NIC/NACp)",
    "outage": "IODA internet measurements",
    "squawk": "ADS-B transponder emergency code",
    "radiation": "gamma dose-rate probe",
    "orbit": "ADS-B tracks (orbit pattern)",
    "concentration": "ADS-B tracks (aircraft concentration)",
    "ais_gap": "AIS reporting gap",
    "seismic": "USGS seismic classification",
}


def detection_kinds(detector: str | None) -> set[str]:
    """Evidence kinds that come from the generating sensor of ``detector`` (hazard_*:
    the hazard alerts themselves)."""
    if not detector:
        return set()
    if detector.startswith("hazard_"):
        return {"hazard", "quake"}
    return set(DETECTOR_KINDS.get(detector, set()))


def sensor_label(detector: str | None) -> str:
    if detector and detector.startswith("hazard_"):
        return "official hazard alert (GDACS / EONET / ReliefWeb)"
    return SENSOR_LABEL.get(detector or "", "sensor detector")
