"""LLM extraction schema + instruction for terrorism classification (one call per candidate
event, on its best article; same discipline as the event extraction: facts from the text
only, verbatim quotes that are checked, null when the text does not say)."""

SCHEMA_VERSION = 1
QUOTE_MAX = 220

KINDS = ["attack", "attempted_attack", "plot_foiled", "arrest", "prosecution", "threat_or_hoax",
         "ct_operation", "warfare", "crime", "communal_violence", "protest_or_unrest",
         "commentary", "none"]
OCCURRED = ["yes", "no", "unclear"]
ATTACK_TYPES = ["bombing_ied", "vbied", "suicide", "armed_assault", "mass_shooting", "stabbing",
                "vehicle_ramming", "arson", "kidnapping", "assassination", "drone_rocket",
                "cyber_physical", "sabotage", "unknown"]
TARGET_TYPES = ["civilians", "religious", "transport", "government", "security_forces",
                "diplomatic", "western_interests", "energy_infrastructure", "education",
                "journalists", "business", "unknown"]
PERP_TYPES = ["non_state_group", "lone_actor", "state_military", "state_security",
              "criminal_group", "unknown"]
ATTRIBUTION = ["claimed", "attributed_by_authorities", "suspected", "unknown"]
PRECISIONS = ["point", "city", "admin1", "country", "unknown"]


def _n(t: str) -> dict:
    return {"type": [t, "null"]}


def _quote() -> dict:
    return _n("string")


TERROR_SCHEMA: dict = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "occurred", "negation_quote", "summary", "attack_type", "target_type",
                 "perpetrator", "attribution", "motive", "criteria", "where", "when",
                 "when_quote", "casualties", "is_follow_up", "quotes"],
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "occurred": {"type": "string", "enum": OCCURRED},
        "negation_quote": _quote(),
        "summary": {"type": "string"},
        "attack_type": {"type": "string", "enum": ATTACK_TYPES},
        "target_type": {"type": "string", "enum": TARGET_TYPES},
        "perpetrator": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "type", "quote"],
            "properties": {"name": _n("string"), "type": {"type": "string", "enum": PERP_TYPES},
                           "quote": _quote()}},
        "attribution": {
            "type": "object", "additionalProperties": False,
            "required": ["level", "by", "channel", "quote"],
            "properties": {"level": {"type": "string", "enum": ATTRIBUTION},
                           "by": _n("string"), "channel": _n("string"), "quote": _quote()}},
        "motive": {
            "type": "object", "additionalProperties": False, "required": ["stated", "quote"],
            "properties": {"stated": _n("string"), "quote": _quote()}},
        "criteria": {
            "type": "object", "additionalProperties": False,
            "required": ["political_religious_social_goal", "intimidate_wider_audience",
                         "civilians_or_noncombatants_targeted", "in_armed_conflict_zone"],
            "properties": {"political_religious_social_goal": _n("boolean"),
                           "intimidate_wider_audience": _n("boolean"),
                           "civilians_or_noncombatants_targeted": _n("boolean"),
                           "in_armed_conflict_zone": _n("boolean")}},
        "where": {
            "type": "object", "additionalProperties": False,
            "required": ["place_name", "admin1", "country", "precision", "quote"],
            "properties": {"place_name": _n("string"), "admin1": _n("string"),
                           "country": _n("string"),
                           "precision": {"type": "string", "enum": PRECISIONS},
                           "quote": _quote()}},
        "when": _n("string"),
        "when_quote": _quote(),
        "casualties": {
            "type": "object", "additionalProperties": False,
            "required": ["killed", "killed_max", "injured", "injured_max", "includes_perpetrators",
                         "quote"],
            "properties": {"killed": _n("integer"), "killed_max": _n("integer"),
                           "injured": _n("integer"), "injured_max": _n("integer"),
                           "includes_perpetrators": _n("boolean"), "quote": _quote()}},
        "is_follow_up": {"type": "boolean"},
        "quotes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["field", "quote"],
            "properties": {"field": {"type": "string"}, "quote": {"type": "string"}}}},
    },
}

INSTRUCTION = (
    "You classify ONE news item (headline, maybe a snippet or article text, any language) for "
    "a terrorism monitor. Use ONLY what the text states; never add outside knowledge (no "
    "knowledge of groups, places or past events). Unknown -> null / 'unknown'. Output values "
    "in English; every *quote* field is copied VERBATIM from the text in its original "
    f"language (at most {QUOTE_MAX} characters) or null.\n"
    "- kind: attack (a violent attack happened, incl. failed/partial), attempted_attack, "
    "plot_foiled (plot disrupted before any attack), arrest (suspects detained), prosecution "
    "(charges, trial, conviction, sentence), threat_or_hoax (threats, bomb-threat e-mails, "
    "hoaxes; no violence), ct_operation (security forces kill/capture militants), warfare "
    "(strikes/fighting by state armed forces, or battles between armed forces), crime "
    "(violence with no political/religious/social motive stated), communal_violence "
    "(inter-communal / cattle raids / clan clashes), protest_or_unrest, commentary "
    "(analysis, opinion, policy, anniversaries, no new incident), none.\n"
    "- occurred: did the incident the item is about actually happen per the text? 'no' when "
    "the text says it did not happen (false alarm, hoax, 'contrary to reports, no ...', "
    "debunked); then negation_quote = that sentence. A denial of INVOLVEMENT or "
    "RESPONSIBILITY by someone ('X denies involvement') is NOT a 'no'.\n"
    "- perpetrator: who carried it out per the text. type: non_state_group, lone_actor, "
    "state_military (a state's armed forces), state_security (police/intelligence acting "
    "for a state), criminal_group, unknown. quote = the words naming them.\n"
    "- attribution.level: claimed (the group itself claimed responsibility; by = claimant, "
    "channel = how, e.g. 'statement on Telegram', 'Amaq'), attributed_by_authorities "
    "(police/ministry/prosecutor named the perpetrator; by = which authority), suspected "
    "(media/analysts/witnesses suspect, 'suspected', 'believed to be'), unknown. quote = the "
    "words stating the claim/attribution.\n"
    "- motive.stated: motive as stated in the text (political, religious, ideological, "
    "personal, robbery...), else null.\n"
    "- criteria: answer only from the text, null if it does not say: "
    "political_religious_social_goal; intimidate_wider_audience (aimed at a wider audience "
    "than the victims); civilians_or_noncombatants_targeted; in_armed_conflict_zone (the "
    "text places it within an ongoing war/insurgency).\n"
    "- where: where the incident itself happened -- NOT where an official spoke, NOT a "
    "capital used for its government ('Kyiv said', 'Washington accuses'). country = ISO "
    "3166-1 alpha-2. quote = the words naming the place of the incident.\n"
    "- when: ISO 8601 date of the incident if the text states or clearly implies it "
    "(e.g. 'on Monday' with a dated article -> null unless the date is in the text), else "
    "null; when_quote = the words. is_follow_up: true when the item is later coverage of an "
    "incident that happened earlier (trial, investigation, anniversary, aftermath report).\n"
    "- casualties: killed / injured as stated (killed_max / injured_max when a range or "
    "'at least N'/conflicting figures are given, else null); includes_perpetrators if the "
    "text says attackers are among the dead. quote = the words with the figures.\n"
    "- attack_type, target_type: closest value, 'unknown' if not stated.\n"
    "- quotes: any other supporting excerpts {field, quote}, verbatim."
)
