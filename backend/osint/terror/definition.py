"""The working definition of terrorism and the incident taxonomy Terror Watch uses.

Aligned with the Global Terrorism Database (GTD, START, University of Maryland) inclusion
criteria (GTD Codebook, "Definition of terrorism"), with operational rules for the
ambiguous cases a news-derived system meets (warfare, insurgency, crime, communal
violence, hoaxes). Exposed verbatim at ``GET /api/terror/definition`` and in the UI.
"""

DEFINITION_VERSION = "2026-11-23.1"

CRITERIA = {
    "required": [
        {"id": "intentional", "text": "The incident is intentional (the result of a "
         "conscious calculation by a perpetrator)."},
        {"id": "violence", "text": "It involves violence or the immediate threat of "
         "violence, including violence against property."},
        {"id": "sub_national", "text": "The perpetrator is a sub-national (non-state) actor: "
         "an organisation, a network or an individual. Acts by states, their armed forces "
         "or security services are excluded (state violence is covered by the conflict "
         "layers)."},
    ],
    "at_least_two_of": [
        {"id": "goal", "text": "C1: aimed at a political, economic, religious or social "
         "goal."},
        {"id": "audience", "text": "C2: intended to coerce, intimidate or convey a message to "
         "a larger audience than the immediate victims."},
        {"id": "outside_warfare", "text": "C3: outside the context of legitimate warfare "
         "(deliberately targets civilians / non-combatants, or violates international "
         "humanitarian law)."},
    ],
}

OPERATIONAL_RULES = [
    ("Interstate or state-on-state warfare (missile, drone, artillery or air strikes by a "
    "state's forces) is excluded even when civilians are hit."),
    ("Attacks by insurgent or designated groups on military / security-force targets in an "
    "active armed conflict are kept as 'possible' (GTD: 'doubt terrorism proper -- "
    "insurgency / guerrilla action') and never counted as confirmed terrorism."),
    ("Ordinary crime (no stated or reported political, religious or social motive) and "
    "communal / inter-communal violence without such a goal (e.g. cattle raids) are "
    "excluded."),
    ("Counter-terrorism operations (security forces killing or capturing militants) are not "
    "attacks; they are kept as context ('ct_operation')."),
    ("Plots, foiled plots, arrests and prosecutions are a separate class -- never counted "
    "as attacks."),
    ("Threats and hoaxes (e.g. bomb-threat e-mails) are class 'threat'; a report that an "
    "attack did NOT happen ('false alarm', 'contrary to reports, no ...', 'hoax') is "
    "never an incident."),
    ("A denial of involvement or responsibility ('X denies involvement') does not erase an "
    "incident; only a denial that the incident happened does."),
    ("Retrospective coverage (follow-up reporting on an attack that happened earlier) is "
    "dated by the attack, not by the article, and is never shown as new."),
]

# incident classes
CLASSES = {
    "attack": "Attack (including attempted / failed attacks)",
    "plot": "Plot or foiled plot (no attack carried out)",
    "arrest": "Arrest / detention of terror suspects",
    "prosecution": "Charges, trial, conviction or sentencing",
    "threat": "Threat or hoax (no violence carried out)",
    "ct_operation": "Counter-terrorism operation (context only)",
}
# incident status (claim vs confirmation are tracked separately)
STATUSES = {
    "confirmed": "Meets the definition and is reported by >= 2 independent outlets (distinct "
                 "organisations, reposts collapsed, all Telegram = one outlet) or by an "
                 "official source, and the article-level verification supports it.",
    "reported": "Meets the definition but rests on a single outlet / origin so far "
                "(reported, unconfirmed).",
    "possible": "Ambiguous: some criteria are unknown, the perpetrator type is unclear, or "
                "an insurgent attack on a military target (doubt terrorism proper).",
    "excluded": "Does not meet the definition (warfare, crime, denial, no event, ...). "
                "Kept for audit with the reason; never shown as an incident.",
}
ATTRIBUTION_LEVELS = {
    "claimed": "Responsibility claimed by the group (who, and through which channel, as "
               "reported by the cited outlet).",
    "attributed_by_authorities": "Attributed by named authorities (police, prosecutors, "
                                 "ministries).",
    "suspected": "Suspected by media / analysts; no claim and no official attribution.",
    "unknown": "No perpetrator stated.",
}
ATTACK_TYPES = {
    "bombing_ied": "Bombing / IED", "vbied": "Vehicle-borne IED", "suicide": "Suicide attack",
    "armed_assault": "Armed assault", "mass_shooting": "Mass shooting",
    "stabbing": "Stabbing / edged weapon", "vehicle_ramming": "Vehicle ramming",
    "arson": "Arson / incendiary", "kidnapping": "Kidnapping / hostage taking",
    "assassination": "Assassination", "drone_rocket": "Drone / rocket attack on civilians",
    "cyber_physical": "Cyber-physical sabotage", "sabotage": "Sabotage of infrastructure",
    "unknown": "Unknown / other",
}
TARGET_TYPES = {
    "civilians": "Private citizens & property", "religious": "Religious sites / figures",
    "transport": "Transport", "government": "Government (general)",
    "security_forces": "Police / military / security forces",
    "diplomatic": "Diplomatic", "western_interests": "US / Western interests",
    "energy_infrastructure": "Energy / utilities / infrastructure",
    "education": "Educational institutions", "journalists": "Journalists & media",
    "business": "Business", "unknown": "Unknown / other",
}

SOURCES_NOTE = (
    "Incidents are derived from the platform's news events (RSS, Telegram, GDELT, news "
    "API) after LLM extraction with verbatim quotes and deterministic rules. Counts are "
    "unique incidents (cross-language duplicates merged), not articles. Historical base "
    "rates use UCDP one-sided violence by non-state actors (a proxy: it excludes "
    "non-lethal attacks, attacks on security forces and lone actors)."
)


def as_dict() -> dict:
    return {"version": DEFINITION_VERSION,
            "basis": "Aligned with the Global Terrorism Database (GTD) inclusion criteria "
                     "(START, University of Maryland, GTD Codebook).",
            "criteria": CRITERIA, "operational_rules": OPERATIONAL_RULES,
            "classes": CLASSES, "statuses": STATUSES,
            "attribution_levels": ATTRIBUTION_LEVELS, "attack_types": ATTACK_TYPES,
            "target_types": TARGET_TYPES, "sources_note": SOURCES_NOTE,
            "disclaimer": "Indicators & warnings, not predictions. Every number shows its "
                          "source, time window and basis."}
