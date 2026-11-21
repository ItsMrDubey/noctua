"""
ICAO type codes that are exclusively military aircraft.
If an aircraft has one of these types, it is ALWAYS military — no civilian variants exist.
"""

# Fighter / Attack
FIGHTERS = {
    "F15", "F16", "F18H", "F18S", "F35", "F5", "F86",
    "EUFI",  # Eurofighter Typhoon
    "HAWK",  # BAE Hawk
    "A10",   # A-10 Thunderbolt
    "B1",    # B-1 Lancer
    "B52",   # B-52 Stratofortress
    "SB39",  # Gripen
    "SA37",  # Viggen
    "TOR",   # Tornado
    "MI24",  # Hind attack helo
    "MI28",  # Havoc attack helo
    "KA52",  # Ka-52 Alligator
    "H64",   # AH-64 Apache
    "AJET",  # Hurjet
    "M339",  # M-346
    "SU",    # Sukhoi fighters (various)
}

# Military Transport / Tanker / Special Mission
TRANSPORT_TANKER = {
    "C17",   # C-17 Globemaster
    "C5M",   # C-5 Super Galaxy
    "C130",  # C-130 Hercules
    "C30J",  # C-130J Super Hercules
    "C2",    # Kawasaki C-2
    "C27J",  # C-27J Spartan
    "C295",  # CASA C-295
    "C160",  # Transall C-160
    "A400",  # A400M Atlas
    "K35R",  # KC-135 Stratotanker
    "KC2",   # KC-2
    "KC39",  # KC-390
    "KC46",  # KC-46 Pegasus
    "CN35",  # CN-235 (mostly military)
    "Y20",   # Y-20 (Chinese military transport)
    "IL76",  # Il-76 (overwhelmingly military)
    "AN124", # An-124 (military transport)
    "AN12",  # An-12
    "AN22",  # An-22
    "AN26",  # An-26
    "AN72",  # An-72
}

# ISR / Surveillance / EW / AWACS
ISR_SURVEILLANCE = {
    "E3CF",  # E-3 Sentry AWACS
    "E3TF",  # E-3 AWACS (TF33)
    "E6",    # E-6 Mercury (TACAMO)
    "E737",  # E-7 Wedgetail
    "R135",  # RC-135 Rivet Joint / Cobra Ball
    "P8",    # P-8 Poseidon (maritime patrol)
    "P3",    # P-3 Orion
    "E2",    # E-2 Hawkeye
    "U2",    # U-2 Dragon Lady
    "ATLA",  # Atlantique (maritime patrol)
}

# UAV / Drones
DRONES = {
    "Q9",    # MQ-9 Reaper / Protector
    "Q4",    # RQ-4 Global Hawk
    "JPRO",  # Jet Provost / UAV
    "UAV",   # Generic UAV
}

# Military Helicopters
MIL_HELOS = {
    "H60",   # UH-60 / MH-60 / SH-60 Black Hawk / Seahawk
    "H47",   # CH-47 Chinook
    "H53",   # CH-53 Sea Stallion
    "H53S",  # CH-53E Super Stallion
    "UH1",   # UH-1 Huey
    "UH1Y",  # UH-1Y Venom
    "V22",   # V-22 Osprey
    "NH90",  # NH90
    "EH10",  # EH101 Merlin
    "LYNX",  # Westland Lynx
    "PUMA",  # SA 330 Puma
    "TIGR",  # Eurocopter Tiger
}

# Military Trainers (exclusively military)
TRAINERS = {
    "T38",   # T-38 Talon
    "T6",    # T-6 Texan II
    "TEX2",  # Texan II variant
    "PC21",  # Pilatus PC-21
    "PC7",   # Pilatus PC-7
    "PC9",   # Pilatus PC-9
    "KT1",   # KAI KT-1
    "CT4",   # CT-4 Airtrainer
    "G120",  # Grob G120
    "TB30",  # Epsilon trainer
}

# HIGH PRIORITY: Carrier-based, ISR, strike, and other high-value assets
# These trigger a sound alert on the dashboard
HIGH_PRIORITY_TYPES = {
    # Carrier-based
    "H60",   # MH-60 Seahawk
    "F18H",  # F/A-18 Hornet
    "F18S",  # F/A-18 Super Hornet
    "E2",    # E-2 Hawkeye (carrier AWACS)
    "V22",   # V-22 Osprey
    "H53",   # CH-53 Sea Stallion
    "H53S",  # CH-53E Super Stallion
    "LYNX",  # Westland Lynx (naval)
    "EH10",  # AW101 Merlin (naval)
    # Strike / Fighters
    "F15", "F16", "F35", "EUFI", "TOR", "A10",
    "B1", "B52", "H64",
    # ISR / High-value
    "E3CF", "E3TF", "E6", "E737", "R135",
    "P8", "P3", "U2",
    # Drones
    "Q9", "Q4",
    # Key transport/tanker (significant in conflict zone)
    "C17", "C5M", "K35R", "KC46",
}

# All military-exclusive type codes combined
ALL_MIL_TYPES = (
    FIGHTERS | TRANSPORT_TANKER | ISR_SURVEILLANCE |
    DRONES | MIL_HELOS | TRAINERS
)


def get_mil_category_from_type(icao_type):
    """
    Returns (is_military, category) based on ICAO type code.
    Only returns True for types that are EXCLUSIVELY military.
    """
    if not icao_type:
        return False, ""

    t = icao_type.upper().strip()

    if t in FIGHTERS:
        return True, "Fighter/Attack"
    if t in TRANSPORT_TANKER:
        return True, "Transport/Tanker"
    if t in ISR_SURVEILLANCE:
        return True, "ISR/Surveillance"
    if t in DRONES:
        return True, "UAV/Drone"
    if t in MIL_HELOS:
        return True, "Military Helo"
    if t in TRAINERS:
        return True, "Trainer"

    return False, ""


def is_high_priority(icao_type):
    """Check if aircraft type is a high-priority alert (carrier-based, strike, ISR, drone)."""
    if not icao_type:
        return False
    return icao_type.upper().strip() in HIGH_PRIORITY_TYPES
