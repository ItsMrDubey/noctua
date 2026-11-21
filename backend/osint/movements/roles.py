"""Role of a military aircraft from its ICAO type code (plus the free-text role/desc fallback)."""

import re

from osint.enrich.mil_types import DRONES, FIGHTERS, ISR_SURVEILLANCE, MIL_HELOS

ROLES = ("airlift", "tanker", "isr", "fighter", "helo", "other")

AIRLIFT = {
    "C17", "C5", "C5M", "A400", "C130", "C30J", "C27J", "C295", "CN35", "C160", "C2", "KC39",
    "IL76", "Y20", "Y9", "Y8", "AN12", "AN124", "A124", "AN22", "AN26", "AN72", "AN32", "C212",
}
TANKER = {"K35R", "K35E", "KC35", "KC46", "K46", "KC10", "KC30", "MRTT", "IL78", "A310", "KC2"}
# types that are a tanker when flown by a military operator (A330 MRTT, KC-46/KC-767, KC-10)
MIL_TANKER_TYPES = {"A332", "B762", "DC10", "A3ST"}
ISR = ISR_SURVEILLANCE | DRONES | {
    "E3", "E7", "E8", "RC135", "RQ4", "MQ9", "MQ4", "Q4", "Q9", "P1", "E2C", "E2D", "E767", "R135"}
HELO = MIL_HELOS | {
    "H64", "MI24", "MI28", "KA52", "MI8", "MI17", "H1", "AS32", "AS65", "AS55", "EC45", "EC35",
    "EC25", "A139", "A169", "A119", "H135", "H145", "H160", "B412", "S61", "S70", "S92", "LYNX",
    "WILD", "NH90", "EH10", "H47", "H53", "H53S", "PUMA", "TIGR", "UH1", "UH1Y", "V22"}
FIGHTER = (FIGHTERS - HELO) | {
    "F22", "F14", "F18", "RFAL", "M2K", "M2000", "J10", "J20", "SU27", "SU30", "SU34", "SU35",
    "MG29", "MG31", "B2", "B1", "B52", "TU95", "TU22", "T160", "JAS39", "F2"}

_KEYWORDS = [
    ("tanker", re.compile(r"tanker|air2air|refuel|thirsty|kc-?\d", re.IGNORECASE)),
    ("isr", re.compile(r"\bisr\b|surveil|recon|aew|awacs|sigint|elint|patrol|submarine|rivet|sentry|poseidon|hawkeye", re.IGNORECASE)),
    ("helo", re.compile(r"heli|chopp|rotor|whirly|cavalry|seahawk|black ?hawk|chinook", re.IGNORECASE)),
    ("fighter", re.compile(r"fighter|attack|bomber|strike|hornet|raptor|typhoon|tornado", re.IGNORECASE)),
    ("airlift", re.compile(r"airlift|cargo|transport|luftbr|globemaster|hercules|atlas", re.IGNORECASE)),
]


def classify(type_code: str | None, *texts: str | None) -> str:
    """Role for ``type_code``; ``texts`` (extra.role, extra.desc, category) break ties for
    ambiguous or unknown types."""
    t = (type_code or "").upper().strip()
    if t in TANKER:
        return "tanker"
    if t in AIRLIFT:
        return "airlift"
    if t in ISR:
        return "isr"
    if t in HELO:
        return "helo"
    if t in FIGHTER:
        return "fighter"
    blob = " ".join(x for x in texts if x)
    for role, rx in _KEYWORDS:
        if rx.search(blob):
            return role
    if t in MIL_TANKER_TYPES:
        return "tanker"
    return "other"


def track_role(meta: dict | None) -> str:
    meta = meta or {}
    extra = meta.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    return classify(meta.get("type_code"), extra.get("role"), extra.get("desc"), meta.get("category"))
