"""Turn an extracted "when" phrase into a UTC time band without inventing precision.

The extractor stores the source's own wording ("early on Nov. 23", "в ніч на 20 листопада",
"samedi 21 novembre", "at 0406"). ``parse_when`` resolves it against the publishing time
of the document it came from and returns a band [lo, hi) in UTC plus a precision label:

  minute       an explicit clock time with a stated zone (UTC/GMT/CET/MSK...)
  time_local   an explicit clock time in an unstated zone: assumed local (longitude-based
               offset), widened by +-1 h for the zone uncertainty
  part_of_day  a date plus "overnight" / "morning" / "early" ... (a stated window)
  date         a calendar date only (the whole local day)
  range        two dates ("between Nov 20 and Nov 21")

Nothing is guessed beyond the phrase: a weekday or "yesterday" is resolved to the most
recent such local day on or before publication; a phrase with no usable anchor returns
None (shown as stated, unplaced). A band that would end after publication is clamped to
the publishing time (an event cannot be reported before it happens); a band that starts
after publication is flagged ``future`` (scheduled / announced, not occurred).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MONTHS: dict[str, int] = {}
for _i, _names in enumerate([
    "january jan janvier enero января січня januar gennaio",
    "february feb février fevrier febrero февраля лютого februar febbraio",
    "march mar mars marzo марта березня märz marz",
    "april apr avril abril апреля квітня aprile",
    "may mai mayo мая травня maggio",
    "june jun juin junio июня червня juni giugno",
    "july jul juillet julio июля липня juli luglio",
    "august aug août aout agosto августа серпня",
    "september sep sept septembre septiembre сентября вересня settembre",
    "october oct octobre octubre октября жовтня oktober ottobre",
    "november nov novembre noviembre ноября листопада",
    "december dec décembre decembre diciembre декабря грудня dezember dicembre",
], start=1):
    for _n in _names.split():
        MONTHS[_n] = _i

# Monday = 0. Oblique cases (Russian / Ukrainian accusative) included.
WEEKDAYS: dict[str, int] = {}
for _i, _names in enumerate([
    "monday lundi lunes понедельник понеділок الاثنين montag",
    "tuesday mardi martes вторник вівторок الثلاثاء dienstag",
    "wednesday mercredi miércoles miercoles среду среда середу середа الأربعاء الاربعاء mittwoch",
    "thursday jeudi jueves четверг четвер الخميس donnerstag",
    "friday vendredi viernes пятницу пятница п'ятницю п'ятниця пʼятницю الجمعة freitag",
    "saturday samedi sábado sabado субботу суббота суботу субота السبت samstag",
    "sunday dimanche domingo воскресенье неділю неділя الأحد الاحد sonntag",
]):
    for _n in _names.split():
        WEEKDAYS[_n] = _i

# Stated zones -> UTC offset hours (November: northern-hemisphere winter time).
ZONES = {"utc": 0.0, "gmt": 0.0, "z": 0.0, "cet": 1.0, "cest": 2.0, "eet": 2.0, "eest": 3.0,
         "msk": 3.0, "moscow time": 3.0, "по московскому времени": 3.0, "kyiv time": 2.0,
         "bst": 1.0, "est": -5.0, "edt": -4.0, "pst": -8.0, "pdt": -7.0, "ist": 5.5,
         "irst": 3.5, "ast": 3.0}

# Part-of-day windows in local hours relative to the stated day (start, end); "night of D"
# starts the evening before.
PARTS: list[tuple[re.Pattern, tuple[float, float], str]] = [
    (re.compile(r"shortly after midnight|сразу после полуночи|одразу після опівночі"),
     (0, 3), "shortly after midnight"),
    (re.compile(r"overnight|night of|night on|night to|в ніч|у ніч|в ночь|ночью|вночі|уночі|"
                r"la nuit|dans la nuit|madrugada|de la noche a|فجر|الليلة|شب"),
     (-4, 6), "overnight"),
    (re.compile(r"early hours|early on|early morning|рано утром|рано вранці|tôt"),
     (0, 9), "early"),
    (re.compile(r"morning|утром|утра|вранці|зранку|matin|mañana|manana|صباح|بامداد|صبح"),
     (5, 12), "morning"),
    (re.compile(r"afternoon|днём|днем|вдень|après-midi|apres-midi|tarde|ظهر|بعدازظهر"),
     (12, 18), "afternoon"),
    (re.compile(r"late on|late at night|late night|поздно вечером|пізно ввечері"),
     (18, 24), "late"),
    (re.compile(r"evening|вечером|вечора|ввечері|soir|soirée|noche|مساء|عصر|شامگاه"),
     (17, 24), "evening"),
]
RANGE_WORDS = re.compile(r"\bbetween\b|\bfrom\b.*\b(?:to|until|till)\b|\bс\b.*\bпо\b|\bз\b.*\bпо\b|"
                         r"\bdu\b.*\bau\b|\bdel\b.*\bal\b|بين")
BOUND_WORDS = re.compile(r"\bthrough\b|\buntil\b|\bsince\b|\bup to\b|\bas of\b|^с |^з |depuis|desde")
REL_DAY = [
    (re.compile(r"\byesterday\b|вчера|вчора|\bhier\b|\bayer\b|أمس|امس|دیروز|昨天|전날"), -1),
    (re.compile(r"\btoday\b|\bthis morning\b|\btonight\b|сегодня|сьогодні|aujourd'hui|\bhoy\b|"
                r"اليوم|امروز|今天|이날"), 0),
    (re.compile(r"\blast night\b|минулої ночі|прошлой ночью|cette nuit|anoche"), 0),
]
WORD = r"[a-zа-яіїєґ'ʼéèêûôáíóúñäöüß؀-ۿ]+"
D_MONTH = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th|er|-?го|-?е)?\.?\s+(?:de\s+)?(" + WORD + r")\.?"
                     r"(?:,?\s+(\d{4}))?")
MONTH_D = re.compile(r"(" + WORD + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?")
ISO_D = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})")
NUM_D = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
CJK_D = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
CLOCK = re.compile(r"(?<![\d.:])(\d{1,2})[:h.](\d{2})(?:\s*([ap])\.?\s?m\.?)?(?![\d])|"
                   r"(?<![\d:])(\d{1,2})\s*([ap])\.?\s?m\.?(?![a-z])|"
                   r"\bat\s+(\d{2})(\d{2})\b")
ZONE_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(z) for z in ZONES), key=len,
                                              reverse=True)) + r")\b|по местному времени|"
                     r"за місцевим часом|local time|heure locale|hora local")


@dataclass
class Band:
    lo: datetime
    hi: datetime
    precision: str
    qualifier: str | None = None
    future: bool = False
    clamped: bool = False
    tz_basis: str = "stated"  # stated | longitude

    def as_dict(self) -> dict:
        return {"lo": iso(self.lo), "hi": iso(self.hi), "precision": self.precision,
                "qualifier": self.qualifier, "future": self.future, "clamped": self.clamped,
                "tz_basis": self.tz_basis}


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="minutes")


def local_offset_h(lon: float | None) -> float:
    """Approximate local UTC offset from longitude (solar time zones, whole hours)."""
    return 0.0 if lon is None else float(round(max(-180.0, min(180.0, lon)) / 15.0))


def _dates(text: str, anchor_local: datetime) -> list[tuple[int, int, int | None]]:
    """(month, day, year or None) found in ``text`` in order of appearance."""
    found: list[tuple[int, int, int, int | None]] = []
    for m in ISO_D.finditer(text):
        found.append((m.start(), int(m[2]), int(m[3]), int(m[1])))
    for m in NUM_D.finditer(text):
        found.append((m.start(), int(m[2]), int(m[1]), int(m[3])))  # day/month/year
    for m in CJK_D.finditer(text):
        found.append((m.start(), int(m[2]), int(m[3]), int(m[1]) if m[1] else None))
    for m in D_MONTH.finditer(text):
        mo = MONTHS.get(m[2].rstrip("."))
        if mo:
            found.append((m.start(), mo, int(m[1]), int(m[3]) if m[3] else None))
    for m in MONTH_D.finditer(text):
        mo = MONTHS.get(m[1].rstrip("."))
        if mo:
            found.append((m.start(), mo, int(m[2]), int(m[3]) if m[3] else None))
    found.sort()
    out, seen_pos = [], set()
    for pos, mo, d, y in found:
        if not (1 <= mo <= 12 and 1 <= d <= 31) or any(abs(pos - p) < 3 for p in seen_pos):
            continue
        seen_pos.add(pos)
        out.append((mo, d, y))
    return out


def _resolve_year(mo: int, d: int, y: int | None, anchor_local: datetime) -> datetime | None:
    """The local midnight of that date; without a year, the year that puts it closest to
    publication ("Dec 1" in a late-November report is next week, "Dec 28" in January was
    last month)."""
    years = [y] if y else [anchor_local.year - 1, anchor_local.year, anchor_local.year + 1]
    best = None
    for yy in years:
        try:
            day = datetime(yy, mo, d, tzinfo=UTC)
        except ValueError:
            continue
        if best is None or abs(day - anchor_local) < abs(best - anchor_local):
            best = day
    return best


def _weekday_day(text: str, anchor_local: datetime) -> datetime | None:
    words = re.findall(WORD, text)
    for w in words:
        if w in WEEKDAYS:
            back = (anchor_local.weekday() - WEEKDAYS[w]) % 7
            return datetime(anchor_local.year, anchor_local.month, anchor_local.day,
                            tzinfo=UTC) - timedelta(days=back)
    return None


def _clock(text: str) -> tuple[int, int] | None:
    m = CLOCK.search(text)
    if not m:
        return None
    if m[1]:
        h, mi, ap = int(m[1]), int(m[2]), m[3]
    elif m[4]:
        h, mi, ap = int(m[4]), 0, m[5]
    else:
        h, mi, ap = int(m[6]), int(m[7]), None
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def parse_when(phrase: str | None, published: datetime, lon: float | None = None) -> Band | None:
    """UTC band for ``phrase`` stated in a document published at ``published``."""
    if not phrase:
        return None
    text = " ".join(str(phrase).casefold().replace("’", "'").split())
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    off_local = local_offset_h(lon)
    anchor_local = published.astimezone(UTC) + timedelta(hours=off_local)
    zone = ZONE_RE.search(text)
    stated_off: float | None = None
    if zone and zone.group(1):
        stated_off = ZONES.get(zone.group(1))
    off = off_local if stated_off is None else stated_off
    tz_basis = "longitude" if stated_off is None else "stated"

    dates = [d for d in (_resolve_year(mo, d, y, anchor_local)
                         for mo, d, y in _dates(text, anchor_local)) if d is not None]
    day: datetime | None = dates[0] if dates else None
    if day is None:
        for rx, delta in REL_DAY:
            if rx.search(text):
                day = datetime(anchor_local.year, anchor_local.month, anchor_local.day,
                               tzinfo=UTC) + timedelta(days=delta)
                break
    if day is None:
        day = _weekday_day(text, anchor_local)
    if day is None:
        return None
    if BOUND_WORDS.search(text) and len({d for d in dates}) < 2:
        return None  # "through Nov 21" / "since Nov 19": a bound, not an occurrence time

    precision, qualifier = "date", None
    lo_l, hi_l = day, day + timedelta(days=1)
    distinct = sorted(set(dates))
    if len(distinct) >= 2 and RANGE_WORDS.search(text):
        lo_l, hi_l, precision = distinct[0], distinct[-1] + timedelta(days=1), "range"
    else:
        clock = _clock(text)
        if clock:
            t = day + timedelta(hours=clock[0], minutes=clock[1])
            if stated_off is None:
                lo_l, hi_l, precision = t - timedelta(hours=1), t + timedelta(hours=1), \
                    "time_local"
            else:
                lo_l, hi_l, precision = t, t + timedelta(minutes=1), "minute"
        else:
            if re.search(r"last night|минулої ночі|прошлой ночью|cette nuit|anoche", text):
                qualifier, (a, b) = "overnight", (-4, 6)
                lo_l, hi_l = day + timedelta(hours=a), day + timedelta(hours=b)
                precision = "part_of_day"
            else:
                for rx, (a, b), label in PARTS:
                    if rx.search(text):
                        lo_l, hi_l = day + timedelta(hours=a), day + timedelta(hours=b)
                        precision, qualifier = "part_of_day", label
                        break
    lo = lo_l - timedelta(hours=off)
    hi = hi_l - timedelta(hours=off)
    band = Band(lo, hi, precision, qualifier, tz_basis=tz_basis)
    pub = published.astimezone(UTC)
    if lo >= pub + timedelta(hours=1):
        band.future = True
    elif hi > pub:
        band.hi, band.clamped = pub, True
    return band
