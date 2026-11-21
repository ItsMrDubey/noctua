"""Per-article extraction schema (spec §3 EventField, §5 stage 4) and instruction."""

EVENT_TYPES = ["armed_conflict", "air_activity", "maritime_security", "civil_unrest",
               "disaster", "infrastructure", "political", "economic", "cyber_outage", "other"]
PRECISIONS = ["point", "city", "admin1", "country", "unknown"]
ROLES = ["perpetrator", "target", "victim", "official", "other"]
CLAIM_TYPES = ["official", "eyewitness", "media_report", "unverified"]
QUOTE_MAX = 200
SUMMARY_MAX_WORDS = 40


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_real_event", "event_type", "subtype", "summary", "when", "where",
                 "actors", "casualties", "weapons_platforms", "cause_stated", "consequences",
                 "claim_type", "quotes"],
    "properties": {
        "is_real_event": {"type": "boolean"},
        "event_type": {"type": "string", "enum": EVENT_TYPES},
        "subtype": _nullable("string"),
        "summary": {"type": "string"},
        "when": _nullable("string"),
        "where": {
            "type": "object", "additionalProperties": False,
            "required": ["place_name", "admin1", "country", "precision"],
            "properties": {
                "place_name": _nullable("string"),
                "admin1": _nullable("string"),
                "country": _nullable("string"),
                "precision": {"type": "string", "enum": PRECISIONS},
            },
        },
        "actors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "role", "type"],
            "properties": {"name": {"type": "string"},
                           "role": {"type": "string", "enum": ROLES},
                           "type": _nullable("string")}}},
        "casualties": {
            "type": "object", "additionalProperties": False, "required": ["killed", "injured"],
            "properties": {"killed": _nullable("integer"), "injured": _nullable("integer")}},
        "weapons_platforms": {"type": "array", "items": {"type": "string"}},
        "cause_stated": _nullable("string"),
        "consequences": {"type": "array", "items": {"type": "string"}},
        "claim_type": {"type": "string", "enum": CLAIM_TYPES},
        "quotes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["field", "quote"],
            "properties": {"field": {"type": "string"}, "quote": {"type": "string"}}}},
    },
}

INSTRUCTION = (
    "You are an OSINT event extractor. The text on stdin is one news article (it may be "
    "truncated, and may be only a headline and snippet). Fill the JSON schema using ONLY "
    "facts stated in the text; never add outside knowledge or guesses (use null / [] when "
    "the text does not say). Write all output values in English, even for non-English "
    "articles; quotes stay verbatim in the original language.\n"
    "- is_real_event: true only if the article reports a concrete thing that happened or "
    "was officially announced. false for opinion, analysis, explainers, previews, "
    "live-blog index pages, sports/entertainment idioms, or pages with no event.\n"
    "- event_type: armed_conflict, air_activity (military flights, airspace), "
    "maritime_security, civil_unrest, disaster, infrastructure, political (incl. "
    "diplomatic), economic, cyber_outage, or other.\n"
    f"- summary: one English sentence, at most {SUMMARY_MAX_WORDS} words.\n"
    "- when: ISO 8601 date or date-time of the incident if the text states it, else null.\n"
    "- where: where the incident itself happened (not where it was reported or who "
    "commented). country = ISO 3166-1 alpha-2 code. precision: point (exact site), city, "
    "admin1 (province/state/sea region), country, unknown.\n"
    "- cause_stated: the stated cause/motive as a near-verbatim English phrase "
    "(e.g. 'in retaliation for ...'), else null.\n"
    "- claim_type: official (government/military statement), eyewitness, media_report, "
    "unverified (claims, rumours, social media).\n"
    f"- quotes: for each key field you filled (event_type, where, when, actors, casualties, "
    f"cause_stated) give {{field, quote}} with a verbatim excerpt of at most {QUOTE_MAX} "
    "characters copied exactly from the text that supports it."
)
