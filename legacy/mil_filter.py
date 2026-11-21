"""
Military and government aircraft identification.
Uses ICAO24 hex ranges and callsign patterns to flag mil/gov aircraft.

NOTE: Many country ICAO blocks are shared between military and civilian.
We only include ranges that are specifically military-assigned or where
the country has very limited commercial aviation.
"""

# ICAO24 hex ranges that are specifically military
# Being conservative to avoid tagging civilian airlines as military
MIL_ICAO_RANGES = {
    "US Military":       [(0xADF7C0, 0xAFFFFF)],
    "Israeli Military":  [(0x738000, 0x7383FF)],  # Tight: IAF only, not El Al
    "UK RAF":            [(0x43C000, 0x43CFFF)],
    "French Military":   [(0x3B0000, 0x3B0FFF)],
    "German Military":   [(0x3F4000, 0x3F4FFF)],
    "NATO":              [(0x480000, 0x480FFF)],
    "Iranian Military":  [(0x730000, 0x7303FF)],  # Tight: IRIAF block
    "Saudi RSAF":        [(0x710800, 0x710FFF)],  # Saudi mil-specific range
    "Russian Military":  [(0x155000, 0x1557FF)],  # Tight: VKS range
    "Turkish Military":  [(0x4B0000, 0x4B00FF)],  # Very tight: TAF
    "Italian Military":  [(0x300000, 0x3003FF)],
    "Greek Military":    [(0x468000, 0x468FFF)],
    "Jordanian Military":[(0x740000, 0x7400FF)],
    "Qatar Emiri AF":    [(0x896C00, 0x896CFF)],
}

# Known military callsign prefixes
MIL_CALLSIGN_PREFIXES = [
    # US Military
    "RCH", "REACH", "DUKE", "EVAC", "JAKE", "NAVY", "DOOM", "BOLT",
    "VIPER", "HAVOC", "TOPCAT", "KING", "JOLLY", "PEDRO", "MAGMA",
    "KNIFE", "FURY", "RAID", "TORCH", "BOXER", "DEMON", "GIANT",
    "HOMER", "IRON", "LANCE", "MAMBA", "PANTH", "RONIN", "SKULL",
    "TANGO", "VALOR", "WOLF", "DRACO", "CYLON", "STEEL", "COBRA",
    "ARROW", "TALON", "ATLAS", "PACK", "ROCKY", "MOOSE", "SPAR",
    # UK RAF
    "RFR", "ASCOT", "RRR",
    # French
    "FAF", "CTM", "COTAM",
    # German
    "GAF",
    # Israeli
    "IAF", "ISR",
    # NATO
    "NATO", "MMF", "NAEW",
    # Russian
    "RFF", "RF",
    # Government/VIP
    "SAM", "EXEC",
]

# Callsign prefixes that look military but are actually civilian airlines
FALSE_POSITIVE_PREFIXES = [
    "ISR9",  # Israir Airlines
]

# Countries of high interest for ME conflict monitoring
INTEREST_COUNTRIES = {
    "Israel", "United States", "Iran", "Russian Federation",
    "United Kingdom", "France", "Turkey", "Saudi Arabia",
    "United Arab Emirates", "Egypt", "Jordan", "Qatar",
    "Iraq", "Syria", "Yemen", "Lebanon",
}


def classify_aircraft(icao24_hex, callsign, country):
    """
    Returns (is_military, tag, priority) for an aircraft.
    priority: 0=civilian, 1=interest country, 2=military/govt
    """
    icao_int = int(icao24_hex, 16)
    cs = (callsign or "").strip().upper()

    # Check for known false positives first
    for fp in FALSE_POSITIVE_PREFIXES:
        if cs.startswith(fp):
            if country in INTEREST_COUNTRIES:
                return False, country, 1
            return False, "", 0

    # Check ICAO24 military ranges
    for tag, ranges in MIL_ICAO_RANGES.items():
        for lo, hi in ranges:
            if lo <= icao_int <= hi:
                return True, tag, 2

    # Check military callsign prefixes
    for prefix in MIL_CALLSIGN_PREFIXES:
        if cs.startswith(prefix) and len(cs) > len(prefix):
            return True, f"MIL ({prefix})", 2

    # Interest country civilian
    if country in INTEREST_COUNTRIES:
        return False, country, 1

    return False, "", 0
