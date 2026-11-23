"""Deterministic text rules: terror vocabulary (candidate pre-filter), denials / negations,
state-warfare headlines, capital-city metonyms, plot / arrest / prosecution language,
retrospective coverage and crime markers. Multilingual where the platform's feeds are
(en, ru, uk, ar, fa, fr, es, de, tr).

These rules never upgrade anything on their own: they exclude (a denial, state warfare),
downgrade (metonym -> no attack location) or route an item to the LLM. Every rule that
fires is recorded on the incident (``rules``) so the decision can be audited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from osint.terror import countries

_F = re.IGNORECASE | re.UNICODE


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts), _F)


# ------------------------------------------------------------------ terror vocabulary
# recall-oriented: an item matching nothing here is not sent to the LLM at all
TERROR_TERMS = _rx(
    # en
    r"\bterror\w*", r"\bjihad\w*", r"\bmilitants?\b", r"\binsurgen\w*", r"\bextremis\w*",
    r"\bsuicide (?:bomb\w*|attack\w*|vest)", r"\bbomb(?:ing|ings|er|ers|s)?\b", r"\bIEDs?\b",
    r"improvised explosive", r"\bcar bomb", r"\bVBIED", r"\bgunm[ae]n\b", r"\bshooting\b",
    r"\bshooter\b", r"opened fire", r"\bstabb(?:ing|ed)\b", r"knife attack", r"\bramm(?:ing|ed)\b",
    r"\bhostages?\b", r"\bkidnap\w*", r"\babduct\w*", r"\bbehead\w*", r"\bassassinat\w*",
    r"\bmassacre\b", r"\bgrenade\b", r"\bblast\b", r"\bexplosion\b", r"\bexplosives?\b",
    r"\barson\b", r"\bfirebomb\w*", r"molotov", r"\bplot(?:ted|ting|s)?\b", r"\bfoiled\b",
    r"\bexplosi\w+", r"\bambush\w*", r"\barmed men\b", r"\battackers?\b", r"\bassailants?\b",
    r"\battack on (?:a |the )?(?:police|church|mosque|synagogue|temple|school|market|bus"
    r"|checkpoint|hotel|convoy|worshippers|festival|concert|parade)",
    r"\bthwart\w*", r"claimed responsibility", r"\bIslamic State\b", r"\bISIS\b", r"\bISIL\b",
    r"\bDaesh\b", r"\bIS-?K\b", r"\bISKP\b", r"\bISWAP\b", r"al[- ]Qa'?[ie]da", r"al[- ]Shabaab",
    r"\bShabab\b", r"Boko Haram", r"\bJNIM\b", r"\bTTP\b", r"\bHamas\b", r"Hezbollah",
    r"Hizballah", r"\bPKK\b", r"\bBLA\b", r"Houthis?", r"Ansar ?Allah", r"Lashkar", r"Jaish",
    r"\bbomb threat", r"\bhoax\b", r"\bradicali[sz]\w*", r"\bneo-?Nazi", r"far-right",
    r"far-left", r"\bantisemit\w*", r"\bsynagogue\b", r"\bmosque\b", r"\bchurch\b",
    # ru / uk
    r"террор\w*", r"теракт\w*", r"смертни\w*", r"взрыв\w*", r"заложни\w*", r"стрельб\w*",
    r"поджог\w*", r"боевик\w*", r"ИГИЛ", r"минирован\w*", r"терор\w*", r"вибух\w*",
    r"стрілянин\w*", r"заручни\w*", r"підпал\w*", r"бойовик\w*",
    # ar
    r"إرهاب\w*", r"ارهاب\w*", r"انتحاري\w*", r"تفجير\w*", r"عبوة ناسفة", r"مفخخة", r"مسلحين",
    r"مسلحون", r"منفذ (?:ال)?هجوم", r"منفذي (?:ال)?هجوم", r"اختطاف", r"رهائن", r"داعش", r"القاعدة", r"حركة الشباب", r"طعن", r"دهس",
    # fa
    r"تروریست\w*", r"انتحاری", r"بمب\w*", r"گروگان\w*", r"داعش",
    # fr / es / de / tr
    r"\bterroris\w*", r"\battentats?\b", r"\bkamikaze\b", r"engin explosif", r"\bfusillade\b",
    r"attaque au couteau", r"prise d'otages", r"\bdjihadist\w*", r"\bjihadist\w*",
    r"\batentados?\b", r"\bexplosivos?\b", r"coche bomba", r"\btiroteo\b", r"\bsecuestr\w*",
    r"hombres armados", r"ataque (?:con explosivos|con drones?|contra (?:la |el |una? )?"
    r"(?:polic\w+|estaci\w+|subestaci\w+|base|batall\w+|cuartel))",
    r"\byihadist\w*", r"\bTerror\w*", r"\bAnschlag\w*", r"\bAttentat\w*", r"Sprengsatz",
    r"Messerangriff", r"Geisel\w*", r"\bIslamist\w*", r"\bterör\w*", r"canlı bomba",
    r"bombalı", r"\brehine\w*",
)
# markers strong enough to keep an item away from the state-warfare shortcut
STRONG_TERROR = _rx(
    r"\bterror\w*", r"\bjihad\w*", r"suicide (?:bomb\w*|attack\w*)", r"claimed responsibility",
    r"\bIslamic State\b", r"\bISIS\b", r"\bISIL\b", r"\bDaesh\b", r"al[- ]Qa'?[ie]da",
    r"al[- ]Shabaab", r"Boko Haram", r"\bJNIM\b", r"\bISWAP\b", r"\bTTP\b", r"\bhostages?\b",
    r"\bextremis\w*", r"террор\w*", r"теракт\w*", r"терор\w*", r"إرهاب\w*", r"ارهاب\w*",
    r"انتحاري", r"تروریست\w*", r"انتحاری", r"\bterroris\w*", r"\battentat\w*", r"\bdjihad\w*",
    r"\batentado\w*", r"\byihad\w*", r"\bAnschlag\w*", r"\bterör\w*",
)

# ------------------------------------------------------------------ denials / negations
# a report that the incident did NOT happen. Never an incident (lesson 1).
DENIAL = _rx(
    r"\bcontrary to (?:the )?(?:earlier |initial |media |some )?reports?\b",
    r"\bfalse alarms?\b", r"\bhoax(?:es)?\b", r"\bdebunk\w*",
    r"\bno (?:missile |drone |rocket |air ?|terror(?:ist)? |bomb )?(?:strikes?|attacks?|launch(?:es)?"
    r"|explosions?|blasts?|shootings?|bombs?|incidents?)\s+(?:were |was |have been |has been |had "
    r"been )?(?:identified|reported|confirmed|detected|recorded|found|took place|occurred)\b",
    r"\bthere (?:was|were|has been|have been|had been) no (?:\w+ ){0,2}(?:attacks?|explosions?"
    r"|blasts?|shootings?|bombs?|strikes?|launch(?:es)?)\b",
    r"\b(?:den(?:y|ied|ies|ying)|rejected|dismissed|refuted) (?:the )?(?:reports?|claims?|rumou?rs?)"
    r" (?:of|that|about) (?:an? |the )?(?:\w+ ){0,2}(?:attacks?|explosions?|blasts?|bombings?"
    r"|shootings?|strikes?|incidents?)",
    r"\b(?:reports?|claims?|rumou?rs?) (?:of|about) (?:an? |the )?(?:\w+ ){0,2}(?:attacks?"
    r"|explosions?|blasts?|shootings?) (?:were|was|are|is) (?:false|untrue|fake|unfounded"
    r"|baseless)\b",
    r"\bno evidence (?:of|that) (?:an? )?(?:attack|explosion|blast|bomb|shooting)",
    r"\bturned out to be (?:a )?(?:false|fake|hoax|controlled|planned|routine)",
    # ru / uk
    r"\bфейк\w*", r"не соответствует действительности", r"не подтверди\w*",
    r"ложн\w* (?:сообщени|минировани|вызов)", r"не відповідає дійсності", r"не підтверди\w*",
    r"неправдив\w*",
    # ar / fa
    r"لا صحة", r"إنذار كاذب", r"انذار كاذب", r"عار عن الصحة", r"شائعات", r"نفى وقوع",
    r"تنفي وقوع", r"ينفي وقوع", r"شایعه", r"صحت ندارد", r"کذب",
    # fr / es / de / tr
    r"fausses? alertes?", r"\bcanulars?\b", r"\bfalsa alarma\b", r"\bbulos?\b",
    r"\bFehlalarm\w*", r"asılsız",
)
# a denial of involvement / responsibility is NOT a denial that the incident happened
DENIAL_OF_INVOLVEMENT = _rx(
    r"\bden(?:y|ies|ied|ying) (?:any )?(?:involvement|responsibility|role|links?|connection"
    r"|participation|being behind|it was behind|accusations?|allegations?)",
    r"\b(?:rejects?|rejected|dismiss(?:es|ed)?) (?:the )?(?:accusations?|allegations?|claims? "
    r"(?:of|that it) (?:involvement|responsib\w*))",
    r"не причастн\w*", r"непричетн\w*", r"تنفي (?:أي )?(?:علاقة|مسؤولية|تورط)",
    r"ينفي (?:أي )?(?:علاقة|مسؤولية|تورط)", r"نفت (?:أي )?(?:علاقة|مسؤولية|تورط)",
    r"dément toute implication", r"niega (?:cualquier )?(?:participación|implicación)",
)
# the incident happened but authorities say it is not terrorism
NOT_TERROR = _rx(
    r"\bnot (?:being )?(?:treated as |considered )?(?:a )?terror(?:ism|ist)?[- ]related",
    r"\bnot (?:a |an )?(?:act of )?terror(?:ism|ist)?(?: attack| incident| act)?\b",
    r"\bno (?:terror(?:ist)?|ideological|political) (?:motive|link|connection|nexus)",
    r"\bruled out (?:a )?(?:terror\w*|ideological motive)", r"\bterror(?:ism)? (?:was )?ruled out",
    r"\bnot (?:politically|ideologically|religiously) motivated",
    r"pas (?:de |d'un |d'une )?(?:caractère |piste )?terroriste",
    r"no (?:se trata de un )?(?:atentado )?terrorista", r"kein(?:en)? terroristisch\w*",
    r"не (?:является|был) терактом", r"не теракт",
)

# ------------------------------------------------------------------ state warfare
# headline actor is a state's armed forces: interstate / state warfare, not terrorism
STATE_STRIKE = _rx(
    r"\bRussian (?:forces|army|troops|military|drones?|missiles?|strikes?|attacks?|shelling"
    r"|aerospace forces|air ?strikes?|glide bombs?|bombers?|occupiers|invaders)\b",
    r"\bRussia(?:'s)? (?:attacks?|strikes?|launch(?:es|ed)?|shells?|pounds?|hits?|targets?"
    r"|bombards?|fires?)\b",
    r"\bRussians (?:attack|strike|shell|hit)",
    r"\bUkrain(?:ian|e's) (?:forces|army|troops|military|drones?|strikes?|attacks?|navy|missiles?"
    r"|shelling|sea drones?)\b",
    r"\bUkraine (?:strikes|attacks|sinks|hits|launches|targets|claims responsib\w+ for (?:the )?"
    r"(?:drone|strike|attack))",
    r"\bIsrael(?:i)? (?:strikes?|army|military|forces|air ?strikes?|troops|army raid|raid)\b",
    r"\bIsrael (?:strikes|bombs|hits|kills|pounds|targets)\b", r"\bIDF\b",
    r"\bUS (?:strikes?|military|forces|navy|air ?strikes?)\b", r"\bAmerican (?:strikes?|forces)\b",
    r"\b(?:Saudi|coalition|Pakistani|Indian|Turkish|Iranian|Sudanese army|SAF|RSF) (?:air ?strikes?"
    r"|strikes?|shelling|forces|army|military|drones?)\b",
    r"\bIRGC\b", r"\bair defen[cs]es?\b", r"\bglide bombs?\b", r"\bFAB-\d+", r"\bShaheds?\b",
    r"\bair[- ]raid alert", r"\bcruise missiles?\b", r"\bballistic missiles?\b",
    r"\bfrontline\b", r"\bfront line\b",
    # ru / uk / ar / fa
    r"\bВСУ\b", r"\bВС РФ\b", r"\bЗСУ\b", r"\bПВО\b", r"\bППО\b", r"российск\w* (?:войск|арми|удар|ракет|дрон|беспилотник)",
    r"російськ\w* (?:військ|арм|удар|ракет|дрон|окупант|атак)", r"\bворог\w*\b", r"окупант\w*",
    r"Минобороны", r"Міноборони", r"الجيش الإسرائيلي", r"جيش الاحتلال", r"غارة إسرائيلية",
    r"غارات إسرائيلية", r"القوات الروسية", r"القوات الأوكرانية", r"مسيرات? أوكرانية",
    r"مسيرات? روسية", r"ارتش اسرائیل", r"حمله پهپادی",
)

# ------------------------------------------------------------------ plots, arrests, courts
PLOT = _rx(r"\bfoiled\b", r"\bthwart\w*", r"\bplot(?:ted|ting|s)?\b", r"\bplanning (?:an? |to )"
           r"(?:attack|bomb)", r"\bprevented (?:an? )?(?:terror\w* )?attack", r"предотвра\w*",
           r"запобіг\w*", r"إحباط", r"مخطط", r"déjoué", r"\bfrustrad[oa]\b", r"vereitelt")
ARREST = _rx(r"\barrest\w*", r"\bdetain\w*", r"\bin custody\b", r"\bapprehend\w*",
             r"\btaken into custody", r"задержан\w*", r"затриман\w*", r"اعتقال", r"القبض",
             r"اعتقل", r"دستگیر", r"\binterpell\w*", r"\bdetenid[oa]s?\b", r"festgenommen",
             r"gözaltı")
PROSECUTION = _rx(r"\bcharged\b", r"\bindict\w*", r"\bconvicted\b", r"\bsentenced\b",
                  r"pleaded guilty", r"pleads guilty", r"\btrial\b", r"\bon trial\b",
                  r"found guilty", r"\bjailed\b", r"\bprosecut\w*", r"приговор\w*",
                  r"осужден\w*", r"засуджен\w*", r"محاكمة", r"حكم على", r"mis en examen",
                  r"condamn\w*", r"\bcondenad\w*", r"verurteilt", r"Anklage")
THREAT = _rx(r"\bbomb threats?\b", r"\bthreat (?:e-?mail|letter|call)", r"\bthreatened to\b",
             r"\bhoax\b", r"\bfalse bomb", r"ложн\w* минировани\w*", r"минировани\w*",
             r"تهديد", r"alerte à la bombe", r"amenaza de bomba", r"Bombendrohung")
CT_OPERATION = _rx(r"\bcounter-?terror\w* (?:operation|raid|forces)", r"\bCTD\b",
                   r"\bsecurity forces (?:killed|neutrali[sz]ed|eliminated)",
                   r"\b(?:terrorists?|militants?|insurgents?) (?:were |was )?(?:killed|neutrali[sz]ed"
                   r"|eliminated) (?:by|in)", r"\bKTO\b", r"\bКТО\b", r"контртеррористическ\w*")
CRIME = _rx(r"\bgang\b", r"\bcartel\b", r"\brobber\w*", r"\bdomestic (?:dispute|violence)",
            r"\bfamily dispute", r"\bpersonal (?:dispute|grudge|motive)", r"\bdrug[- ]",
            r"\bfeud\b", r"\bmafia\b", r"\bextortion\b", r"\bcattle (?:camp|raid|rustl)",
            r"\bcommunal\b", r"\bintercommunal\b", r"\bclan\b",
            r"\bcriminal organi[sz]ation")
RETROSPECTIVE = _rx(r"\banniversar\w*", r"\b(?:a|one|two|three|\d+) (?:years?|months?) "
                    r"(?:after|since|ago)\b", r"\bremember\w*", r"\bmemorial\b",
                    r"\bcommemorat\w*", r"\blater carried out\b", r"\bwho (?:later )?carried out\b",
                    r"\bгодовщин\w*", r"\bроковин\w*", r"ذكرى")

# ------------------------------------------------------------------ metonyms
SPEECH_VERBS = (
    r"says?|said|named|names|accus\w+|claims?|claimed|warns?|warned|announc\w+|denies|denied|"
    r"rejects?|rejected|condemns?|condemned|calls?|called|urges?|urged|insists?|vows?|vowed|"
    r"responds?|responded|confirms?|confirmed|believes?|blames?|blamed|threatens?|threatened|"
    r"summons|summoned|plans?|approves?|approved|sanctions?|designat\w+|wants?|demands?|"
    r"demanded|seeks?|sought|hopes?|signals?|pledges?|pledged|offers?|offered|reports?|"
    r"reported|announced|recogni[sz]\w+|criticis\w+|criticiz\w+|welcomes?|welcomed|"
    r"заяви\w*|обвини\w*|назва\w*|сообщи\w*|повідоми\w*|звинуват\w*|يتهم|تتهم|اتهم|"
    r"أعلن\w*|تعلن|يعلن|قالت|يقول|تقول|accuse\w*|annonce\w*|affirme\w*|dénonce\w*|"
    r"acusa\w*|anuncia\w*|afirma\w*|wirft|erklärt\w*"
)
_METONYM_CACHE: dict[str, re.Pattern] = {}


def _metonym_rx(place: str) -> re.Pattern:
    key = place.casefold()
    rx = _METONYM_CACHE.get(key)
    if rx is None:
        p = re.escape(place)
        rx = re.compile(
            rf"(?:(?<![\w-]){p}(?:'s)?\s+(?:government\s+|officials?\s+|authorities\s+)?"
            rf"(?:has\s+|have\s+|had\s+|will\s+|would\s+|is\s+|was\s+|now\s+|also\s+|again\s+)?"
            rf"(?:{SPEECH_VERBS})\b)|(?:according to\s+{p}\b)|(?:\b(?:officials?|government|"
            rf"authorities) in\s+{p}\b\s+(?:{SPEECH_VERBS}))", _F)
        _METONYM_CACHE[key] = rx
    return rx


def is_metonym(place: str | None, text: str | None) -> bool:
    """True when ``place`` is a capital / seat of government used for its government
    ("Kyiv named ...", "Washington says ...", "Tehran accuses ...") in ``text`` and is NOT
    also used as a location ("in Kyiv", "Kyiv's ... district"). A capital that only
    appears as a speaker never becomes an attack site (lesson 4)."""
    if not place or not text:
        return False
    key = countries.fold(place)
    if key not in countries.capitals() and countries.iso2(place) is None:
        return False
    if not _metonym_rx(place).search(text):
        return False
    # also used locatively? then the place itself is fine
    loc = re.compile(rf"\b(?:in|at|near|outside|around|across|inside|into|on|of)\s+(?:central\s+|"
                     rf"downtown\s+|the city of\s+)?{re.escape(place)}\b", _F)
    return not loc.search(text)


# ------------------------------------------------------------------ helpers


@dataclass
class TextFlags:
    terror_terms: bool
    strong_terror: bool
    denial: bool
    denial_of_involvement: bool
    not_terror: bool
    state_strike: bool
    plot: bool
    arrest: bool
    prosecution: bool
    threat: bool
    ct_operation: bool
    crime: bool
    retrospective: bool

    def fired(self) -> list[str]:
        return [k for k, v in self.__dict__.items() if v]


def flags(text: str) -> TextFlags:
    t = text or ""
    denial = bool(DENIAL.search(t))
    involvement = bool(DENIAL_OF_INVOLVEMENT.search(t))
    return TextFlags(
        terror_terms=bool(TERROR_TERMS.search(t)), strong_terror=bool(STRONG_TERROR.search(t)),
        denial=denial, denial_of_involvement=involvement, not_terror=bool(NOT_TERROR.search(t)),
        state_strike=bool(STATE_STRIKE.search(t)), plot=bool(PLOT.search(t)),
        arrest=bool(ARREST.search(t)), prosecution=bool(PROSECUTION.search(t)),
        threat=bool(THREAT.search(t)), ct_operation=bool(CT_OPERATION.search(t)),
        crime=bool(CRIME.search(t)), retrospective=bool(RETROSPECTIVE.search(t)))


def is_denial(text: str | None) -> bool:
    """The text reports that an incident did NOT happen (false alarm, hoax, "contrary to
    reports, no ..."). "X denies involvement" is not a denial of the incident."""
    t = text or ""
    if not DENIAL.search(t):
        return False
    # "hoax" inside "bomb hoax" is a threat/hoax incident class, still not an attack; a
    # sentence that only denies involvement is not an event denial
    stripped = DENIAL_OF_INVOLVEMENT.sub(" ", t)
    return bool(DENIAL.search(stripped))


def is_candidate(text: str | None) -> bool:
    """Recall-oriented pre-filter: worth an LLM look for terrorism?"""
    return bool(TERROR_TERMS.search(text or ""))


def state_warfare_only(text: str | None) -> bool:
    """A headline whose actor is a state's armed forces with no terrorism marker at all:
    excluded without an LLM call (interstate / state warfare)."""
    t = text or ""
    return bool(STATE_STRIKE.search(t)) and not STRONG_TERROR.search(t)


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a dozen": 12,
              "dozens": None, "scores": None}
_KILLED = re.compile(r"(?:at least |more than |over |nearly |up to )?(\d{1,4}|"
                     + "|".join(k for k in _NUM_WORDS if _NUM_WORDS[k]) +
                     r")\s+(?:\w+\s+){0,3}?(?:people\s+|civilians\s+|persons\s+)?"
                     r"(?:were\s+|are\s+|have been\s+)?(?:killed|dead|died|deaths|slain)", _F)
_KILLS = re.compile(r"\b(?:kill(?:s|ed|ing)|leaves?|left)\s+(?:at least |more than |over |nearly )?"
                    r"(\d{1,4}|" + "|".join(k for k in _NUM_WORDS if _NUM_WORDS[k]) + r")\b"
                    r"(?:\s+(?:people|civilians|dead|persons|soldiers|worshippers|children))?", _F)
_INJURED = re.compile(r"(?:at least |more than |over |nearly )?(\d{1,4}|"
                      + "|".join(k for k in _NUM_WORDS if _NUM_WORDS[k]) +
                      r")\s+(?:\w+\s+){0,3}?(?:people\s+|civilians\s+)?(?:were\s+|are\s+)?"
                      r"(?:injured|wounded|hurt)", _F)


def _num(tok: str) -> int | None:
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


def headline_casualties(title: str | None) -> dict:
    """Killed / injured figures stated in an English headline ({} when none). Used only
    to surface source disagreement next to the extracted figures, never alone."""
    t = title or ""
    out: dict = {}
    m = _KILLED.search(t) or _KILLS.search(t)
    if m:
        v = _num(m.group(1))
        if v is not None:
            out["killed"] = v
    m = _INJURED.search(t)
    if m:
        v = _num(m.group(1))
        if v is not None:
            out["injured"] = v
    return out
