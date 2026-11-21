"""Offline multilingual gazetteer (GeoNames cities15000 + countries + admin1), CC-BY 4.0.

Source files are downloaded by ``scripts/fetch_gazetteer.sh`` into ``data/gazetteer/``.
The first load parses them and caches a compact SQLite index (``index.sqlite``) next to
them; later loads read the cache (rebuilt automatically when the source files change).

``match(text, language)`` looks up 1-3 token n-grams of Unicode-normalised, case-folded
text (character windows for CJK), then disambiguates candidates by population, country
co-mentions and specificity.
"""

import logging
import math
import re
import sqlite3
import statistics
import threading
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from osint.core.geo import haversine_km
from osint.core.models import PlaceHit

log = logging.getLogger(__name__)

INDEX_VERSION = "1"
SOURCE_FILES = ("cities15000.zip", "countryInfo.txt", "admin1CodesASCII.txt")
MIN_CONFIDENCE = 0.5
_SPECIFICITY = {"city": 0.08, "admin1": 0.04, "country": 0.0}

# Common words that are also place names (after case-folding); never matched alone.
STOPWORDS = frozenset(str.split("""
a an and are as at be but by for from has have he her his in is it its of on or our she
that the their them they this to was we were will with you said says say says new old
police mobile nice split reading bath most male bar bay may march sale same deal union
university hope opportunity independence liberty victory progress unity peace freedom
central north south east west northern southern eastern western city state capital port
san santa saint st la le el al de del da di do du des los las der die das und van von
she man mi ma be no si se te ya va un una une il est les en et au aux par pour sur
over under after before during against about between into through across among
best last first second third next live news world today report update video watch
"""))

# Given names / surnames / generic words that are also city names: never matched as a
# single token (multi-token names such as "Port Said" still match).
AMBIGUOUS_SINGLE = frozenset(str.split("""
vladimir volodymyr davis summit victoria florence jackson lincoln eugene troy marion hamilton
charlotte austin orlando lafayette mohammed muhammad ahmed ali hassan hussein said salem
asia europe africa oceania america americas trump biden carter kennedy wilson clinton
regina laval mercedes president minister general admiral colonel
"""))

# Seas, straits and other maritime areas (not in cities15000). Precision "admin1"-like.
MARITIME_AREAS: list[tuple[str, float, float, list[str]]] = [
    ("Red Sea", 20.0, 38.5, ["red sea", "البحر الأحمر", "красное море", "دریای سرخ", "mer rouge", "mar rojo", "rotes meer", "kızıldeniz",
                             "红海", "червоне море"]),
    ("Bab el-Mandeb", 12.6, 43.4, ["bab el mandeb", "bab al mandab", "bab el-mandeb",
                                   "باب المندب", "баб эль мандеб", "باب‌المندب"]),
    ("Gulf of Aden", 12.5, 47.5, ["gulf of aden", "خليج عدن", "аденский залив",
                                  "golfe d'aden", "golfo de adén", "亚丁湾"]),
    ("Strait of Hormuz", 26.6, 56.4, ["strait of hormuz", "hormuz strait", "مضيق هرمز",
                                      "ормузский пролив", "تنگه هرمز", "détroit d'ormuz",
                                      "estrecho de ormuz", "straße von hormus",
                                      "hürmüz boğazı", "霍尔木兹海峡"]),
    ("Persian Gulf", 27.0, 51.5, ["persian gulf", "arabian gulf", "الخليج العربي",
                                  "خلیج فارس", "персидский залив", "golfe persique",
                                  "golfo pérsico", "persischer golf", "basra körfezi", "波斯湾"]),
    ("Gulf of Oman", 24.5, 58.5, ["gulf of oman", "خليج عمان", "دریای عمان",
                                  "оманский залив"]),
    ("Arabian Sea", 15.0, 63.0, ["arabian sea", "بحر العرب", "аравийское море"]),
    ("Suez Canal", 30.6, 32.35, ["suez canal", "قناة السويس", "суэцкий канал",
                                 "canal de suez", "suezkanal", "süveyş kanalı", "苏伊士运河"]),
    ("Black Sea", 43.0, 34.0, ["black sea", "чёрное море", "черное море", "чорне море",
                               "البحر الأسود", "mer noire", "mar negro", "schwarzes meer",
                               "karadeniz", "黑海", "دریای سیاه"]),
    ("Sea of Azov", 46.0, 36.5, ["sea of azov", "азовское море", "азовське море"]),
    ("Kerch Strait", 45.3, 36.5, ["kerch strait", "керченский пролив", "керченська протока"]),
    ("Baltic Sea", 58.0, 20.0, ["baltic sea", "балтийское море", "mer baltique",
                                "mar báltico", "ostsee", "baltık denizi", "波罗的海"]),
    ("South China Sea", 12.0, 114.0, ["south china sea", "南海", "южно-китайское море",
                                      "mer de chine méridionale", "mar de china meridional",
                                      "südchinesisches meer", "west philippine sea"]),
    ("East China Sea", 29.0, 125.0, ["east china sea", "东海", "восточно-китайское море"]),
    ("Taiwan Strait", 24.0, 119.5, ["taiwan strait", "台湾海峡", "тайваньский пролив",
                                    "détroit de taïwan", "estrecho de taiwán"]),
    ("Strait of Malacca", 3.0, 100.5, ["strait of malacca", "malacca strait",
                                       "马六甲海峡", "малаккский пролив"]),
    ("Eastern Mediterranean", 34.0, 30.0, ["eastern mediterranean", "شرق المتوسط"]),
    ("English Channel", 50.2, -1.0, ["english channel", "la manche"]),
    ("Panama Canal", 9.1, -79.7, ["panama canal", "canal de panamá", "canal de panama"]),
    ("Gulf of Guinea", 2.0, 3.0, ["gulf of guinea", "golfe de guinée"]),
]

# Multilingual names, short forms and demonyms for countries (ISO2). countryInfo.txt is
# English only; GeoNames' country alternate names live in a 600 MB file we don't need.
COUNTRY_ALIASES: dict[str, list[str]] = {
    "US": ["us", "u.s", "usa", "u.s.a", "united states of america", "america", "american",
           "americans", "соединенные штаты", "сша", "الولايات المتحدة",
           "أمريكا", "آمریکا", "ایالات متحده", "美国", "états-unis", "estados unidos",
           "vereinigte staaten", "abd", "сша"],
    "GB": ["uk", "u.k", "britain", "great britain", "british", "england", "великобритания",
           "بريطانيا", "بریتانیا", "英国", "royaume-uni", "reino unido", "großbritannien",
           "birleşik krallık", "велика британія"],
    "RU": ["russia", "russian", "russians", "kremlin", "россия", "рф", "росія",
           "روسيا", "روسیه", "俄罗斯", "russie", "rusia", "russland", "rusya"],
    "UA": ["ukraine", "ukrainian", "ukrainians", "украина", "україна", "أوكرانيا",
           "اوکراین", "乌克兰", "ucrania", "ukraina", "ukrayna"],
    "IL": ["israel", "israeli", "israelis", "idf", "израиль", "ізраїль", "إسرائيل",
           "اسرائيل", "اسرائیل", "以色列", "israël", "israil"],
    "PS": ["palestine", "palestinian", "palestinians", "gaza strip", "west bank",
           "فلسطين", "قطاع غزة", "الضفة الغربية", "палестина", "сектор газа", "巴勒斯坦",
           "加沙", "cisjordanie", "cisjordania", "westjordanland", "filistin"],
    "IR": ["iran", "iranian", "iranians", "иран", "إيران", "ایران", "伊朗",
           "irán", "i̇ran"],
    "IQ": ["iraq", "iraqi", "ирак", "العراق", "عراق", "伊拉克", "irak"],
    "SY": ["syria", "syrian", "сирия", "سوريا", "سورية", "سوریه", "叙利亚", "syrie", "siria",
           "syrien", "suriye"],
    "LB": ["lebanon", "lebanese", "ливан", "لبنان", "黎巴嫩", "liban", "líbano", "libanon",
           "lübnan"],
    "YE": ["yemen", "yemeni", "houthi", "houthis", "йемен", "اليمن", "یمن", "也门", "yémen",
           "jemen"],
    "SA": ["saudi arabia", "saudi", "саудовская аравия", "السعودية", "عربستان", "沙特",
           "arabie saoudite", "arabia saudita", "saudi-arabien", "suudi arabistan"],
    "AE": ["uae", "u.a.e", "emirates", "emirati", "الإمارات", "امارات", "оаэ", "阿联酋"],
    "QA": ["qatar", "qatari", "قطر", "катар", "卡塔尔", "katar"],
    "EG": ["egypt", "egyptian", "египет", "مصر", "埃及", "égypte", "egipto", "ägypten",
           "mısır"],
    "JO": ["jordan", "jordanian", "иордания", "الأردن", "اردن", "约旦", "jordanie"],
    "TR": ["turkey", "türkiye", "turkish", "турция", "تركيا", "ترکیه", "土耳其", "turquie",
           "turquía", "türkei", "туреччина"],
    "CN": ["china", "chinese", "китай", "الصين", "چین", "中国", "chine",
           "çin", "китаю"],
    "TW": ["taiwan", "taiwanese", "тайвань", "تايوان", "تایوان", "台湾", "taïwan", "taiwán",
           "tayvan"],
    "KP": ["north korea", "north korean", "dprk", "кндр", "северная корея",
           "كوريا الشمالية", "کره شمالی", "朝鲜", "corée du nord", "corea del norte",
           "nordkorea", "kuzey kore"],
    "KR": ["south korea", "south korean", "южная корея", "كوريا الجنوبية", "کره جنوبی",
           "韩国", "corée du sud", "corea del sur", "südkorea", "güney kore"],
    "JP": ["japan", "japanese", "япония", "اليابان", "ژاپن", "日本", "japon", "japón",
           "japonya"],
    "IN": ["india", "indian", "индия", "الهند", "هند", "印度", "inde", "indien", "hindistan"],
    "PK": ["pakistan", "pakistani", "пакистан", "باكستان", "پاکستان", "巴基斯坦"],
    "AF": ["afghanistan", "afghan", "taliban", "афганистан", "أفغانستان", "افغانستان",
           "阿富汗", "afganistán", "afganistan"],
    "SD": ["sudan", "sudanese", "rsf", "судан", "السودان", "سودان", "苏丹", "soudan",
           "sudán"],
    "SS": ["south sudan", "южный судан", "جنوب السودان"],
    "LY": ["libya", "libyan", "ливия", "ليبيا", "لیبی", "利比亚", "libye", "libia",
           "libyen", "libya"],
    "SO": ["somalia", "somali", "al-shabaab", "сомали", "الصومال", "索马里", "somalie"],
    "ET": ["ethiopia", "ethiopian", "эфиопия", "إثيوبيا", "埃塞俄比亚", "éthiopie", "etiopía"],
    "ML": ["mali", "malian", "мали", "مالي"],
    "BF": ["burkina faso", "буркина-фасо", "بوركينا فاسو"],
    "NE": ["niger", "nigerien", "нигер", "النيجر"],
    "NG": ["nigeria", "nigerian", "нигерия", "نيجيريا", "尼日利亚", "nigéria"],
    "CD": ["drc", "dr congo", "democratic republic of the congo", "congo-kinshasa",
           "демократическая республика конго", "الكونغو الديمقراطية", "rdc"],
    "MM": ["myanmar", "burma", "burmese", "мьянма", "ميانمار", "缅甸", "birmanie"],
    "VE": ["venezuela", "venezuelan", "венесуэла", "فنزويلا", "委内瑞拉"],
    "BY": ["belarus", "belarusian", "беларусь", "белоруссия", "білорусь", "بيلاروسيا",
           "白俄罗斯", "biélorussie", "bielorrusia", "weißrussland"],
    "PL": ["poland", "polish", "польша", "польща", "بولندا", "波兰", "pologne", "polonia",
           "polen", "polonya"],
    "DE": ["germany", "german", "германия", "німеччина", "ألمانيا", "آلمان", "德国",
           "allemagne", "alemania", "deutschland", "almanya"],
    "FR": ["france", "french", "франция", "франція", "فرنسا", "فرانسه", "法国", "francia",
           "frankreich", "fransa"],
    "AM": ["armenia", "armenian", "армения", "أرمينيا", "ارمنستان"],
    "AZ": ["azerbaijan", "azerbaijani", "азербайджан", "أذربيجان", "آذربایجان"],
    "GE": ["georgia", "georgian", "грузия", "جورجيا", "گرجستان"],
    "MD": ["moldova", "moldovan", "молдавия", "молдова"],
    "RS": ["serbia", "serbian", "сербия"],
    "PH": ["philippines", "philippine", "filipino", "филиппины", "الفلبين", "菲律宾"],
    "VN": ["vietnam", "vietnamese", "вьетнам", "فيتنام", "越南"],
    "MX": ["mexico", "mexican", "méxico", "мексика", "المكسيك", "墨西哥", "mexique"],
    "CO": ["colombia", "colombian", "колумбия", "كولومبيا", "哥伦比亚", "colombie"],
    "HT": ["haiti", "haitian", "гаити", "هايتي", "haïti", "haití"],
    "ER": ["eritrea", "eritrean", "эритрея", "إريتريا"],
    "DJ": ["djibouti", "джибути", "جيبوتي"],
    "OM": ["oman", "omani", "оман", "عمان", "阿曼"],
    "BH": ["bahrain", "bahraini", "бахрейн", "البحرين"],
    "KW": ["kuwait", "kuwaiti", "кувейт", "الكويت"],
    "CY": ["cyprus", "cypriot", "кипр", "قبرص"],
    "GR": ["greece", "greek", "греция", "اليونان"],
}

_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_TOKEN_RE = re.compile(r"[^\W_]+(?:[.'’][^\W_]+)*", re.UNICODE)
_KANA_RE = re.compile(r"[぀-ヿ]")
_SPLIT_RE = re.compile(r"[.'’]")
_ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ة": "ه", "ک": "ك",
    "ـ": None, "’": "'",
})
_AR_CLITICS = ("وال", "بال", "فال", "كال", "لل", "و", "ف", "ب", "ل", "ك")
_CYR_SUFFIXES = ("ами", "ями", "ого", "ому", "ой", "ом", "ем", "ах", "ях", "ам", "ям",
                 "а", "у", "е", "и", "і", "ы", "ю", "я")


def _is_acronym(token: str) -> bool:
    return token.isascii() and token.isupper() and len(token.replace(".", "")) <= 4


def fold(text: str) -> str:
    """NFKC, case-fold, drop combining marks, fold Arabic/Persian letter variants."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFKC", text).translate(_ARABIC_FOLD)


def tokens(text: str) -> list[tuple[str, str]]:
    """(folded token, original token) pairs."""
    out = []
    for m in _TOKEN_RE.finditer(text):
        orig = m.group(0)
        for part in _SPLIT_RE.split(fold(orig)):
            if part:
                out.append((part, orig))
    return out


def name_key(name: str) -> str:
    return " ".join(t for t, _ in tokens(name))


@dataclass(frozen=True, slots=True)
class Place:
    pid: int
    name: str
    country: str
    admin1: str
    lat: float
    lon: float
    population: int
    feature: str  # GeoNames feature code, "COUNTRY", "ADM1", "sea"
    precision: str  # city | admin1 | country


def _keep_alt_name(raw: str) -> bool:
    raw = raw.strip()
    if not raw or any(ch.isdigit() for ch in raw) or "/" in raw or "@" in raw:
        return False
    if raw.isascii() and raw.isupper() and len(raw) <= 4:  # IATA/ICAO-style codes
        return False
    key = name_key(raw)
    if not key:
        return False
    if _CJK_RE.search(key):
        return len(key.replace(" ", "")) >= 2
    return len(key) >= 3


class Gazetteer:
    def __init__(self, places: list[Place], names: dict[str, list[tuple[int, int]]]) -> None:
        """``names``: key -> [(place index, is_primary)]."""
        self.places = places
        self.names = names
        self.cjk_keys = {k for k in names if _CJK_RE.search(k)}
        self.max_cjk = max((len(k) for k in self.cjk_keys), default=0)
        self.country_place: dict[str, int] = {
            p.country: i for i, p in enumerate(places) if p.precision == "country"}

    # ------------------------------------------------------------------ building

    @classmethod
    def from_records(cls, records: list[tuple[Place, list[str], list[str]]]) -> "Gazetteer":
        """records: (place, primary names, alternate names)."""
        places: list[Place] = []
        names: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for place, primary, alternates in records:
            idx = len(places)
            places.append(place)
            seen: set[str] = set()
            for is_primary, group in ((1, primary), (0, alternates)):
                for raw in group:
                    key = name_key(raw)
                    if not key or key in seen or key in STOPWORDS:
                        continue
                    seen.add(key)
                    names[key].append((idx, is_primary))
        return cls(places, dict(names))

    @classmethod
    def build(cls, src_dir: Path) -> "Gazetteer":
        countries: dict[str, str] = {}
        with (src_dir / "countryInfo.txt").open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) > 4:
                    countries[cols[0]] = cols[4]
        admin1: dict[str, str] = {}
        with (src_dir / "admin1CodesASCII.txt").open(encoding="utf-8") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 3:
                    admin1[cols[0]] = cols[1]
        records: list[tuple[Place, list[str], list[str]]] = []
        by_country: dict[str, list[tuple[float, float]]] = defaultdict(list)
        by_admin1: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
        with zipfile.ZipFile(src_dir / "cities15000.zip") as z:
            text = z.read("cities15000.txt").decode("utf-8")
        for line in text.splitlines():
            c = line.split("\t")
            if len(c) < 15:
                continue
            try:
                gid, lat, lon, pop = int(c[0]), float(c[4]), float(c[5]), int(c[14] or 0)
            except ValueError:
                continue
            cc, a1 = c[8], c[10]
            alts = [a for a in c[3].split(",") if _keep_alt_name(a)]
            records.append((Place(gid, c[1], cc, admin1.get(f"{cc}.{a1}", ""), lat, lon, pop,
                                  c[7], "city"), [c[1], c[2]], alts))
            by_country[cc].append((lat, lon))
            by_admin1[f"{cc}.{a1}"].append((lat, lon, pop))
        # admin1 areas and countries get the median of their cities' coordinates
        for code, name in admin1.items():
            pts = by_admin1.get(code)
            if not pts:
                continue
            cc = code.split(".")[0]
            lat = statistics.median(p[0] for p in pts)
            lon = statistics.median(p[1] for p in pts)
            pop = sum(p[2] for p in pts)
            records.append((Place(-1, name, cc, name, lat, lon, pop, "ADM1", "admin1"),
                            [name], []))
        for cc, name in countries.items():
            pts = by_country.get(cc)
            if not pts:
                continue
            lat = statistics.median(p[0] for p in pts)
            lon = statistics.median(p[1] for p in pts)
            records.append((Place(-1, name, cc, "", lat, lon, 10**8, "COUNTRY", "country"),
                            [name], COUNTRY_ALIASES.get(cc, [])))
        for name, lat, lon, aliases in MARITIME_AREAS:
            records.append((Place(-1, name, "", "", lat, lon, 10**7, "sea", "admin1"),
                            [name], aliases))
        return cls.from_records(records)

    # ------------------------------------------------------------------ caching

    @staticmethod
    def _fingerprint(src_dir: Path) -> str:
        parts = [INDEX_VERSION]
        for f in SOURCE_FILES:
            st = (src_dir / f).stat()
            parts.append(f"{f}:{st.st_size}:{int(st.st_mtime)}")
        return "|".join(parts)

    def save(self, path: Path, fingerprint: str) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        try:
            con.executescript(
                "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);"
                "CREATE TABLE places(i INTEGER PRIMARY KEY, pid INTEGER, name TEXT, cc TEXT,"
                " a1 TEXT, lat REAL, lon REAL, pop INTEGER, feature TEXT, precision TEXT);"
                "CREATE TABLE names(k TEXT, i INTEGER, prim INTEGER);"
            )
            con.execute("INSERT INTO meta VALUES ('fingerprint', ?)", (fingerprint,))
            con.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?)", (
                (i, p.pid, p.name, p.country, p.admin1, p.lat, p.lon, p.population,
                 p.feature, p.precision) for i, p in enumerate(self.places)))
            con.executemany("INSERT INTO names VALUES (?,?,?)", (
                (k, i, prim) for k, lst in self.names.items() for i, prim in lst))
            con.commit()
        finally:
            con.close()
        tmp.replace(path)

    @classmethod
    def load_cache(cls, path: Path, fingerprint: str) -> "Gazetteer | None":
        if not path.exists():
            return None
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
            if not row or row[0] != fingerprint:
                return None
            places = [Place(*r) for r in con.execute(
                "SELECT pid, name, cc, a1, lat, lon, pop, feature, precision FROM places "
                "ORDER BY i")]
            names: dict[str, list[tuple[int, int]]] = defaultdict(list)
            for k, i, prim in con.execute("SELECT k, i, prim FROM names"):
                names[k].append((i, prim))
        except sqlite3.DatabaseError:
            return None
        finally:
            con.close()
        return cls(places, dict(names))

    @classmethod
    def load(cls, src_dir: Path) -> "Gazetteer":
        fp = cls._fingerprint(src_dir)
        cache = src_dir / "index.sqlite"
        gz = cls.load_cache(cache, fp)
        if gz is not None:
            return gz
        log.info("building gazetteer index from %s", src_dir)
        gz = cls.build(src_dir)
        try:
            gz.save(cache, fp)
        except OSError:
            log.warning("could not cache gazetteer index at %s", cache, exc_info=True)
        return gz

    # ------------------------------------------------------------------ matching

    def _lookup(self, key: str) -> list[tuple[int, int]]:
        return self.names.get(key, [])

    def _variants(self, tok: str) -> list[str]:
        """Inflection/clitic fallbacks for single Arabic-script and Cyrillic tokens."""
        out: list[str] = []
        first = tok[0]
        if "؀" <= first <= "ۿ":
            for p in _AR_CLITICS:
                if tok.startswith(p) and len(tok) - len(p) >= 3:
                    rest = tok[len(p):]
                    out += [rest, "ال" + rest]
        elif "Ѐ" <= first <= "ӿ" and len(tok) >= 5:
            for suf in _CYR_SUFFIXES:
                if tok.endswith(suf) and len(tok) - len(suf) >= 4:
                    stem = tok[: -len(suf)]
                    out += [stem, stem + "а", stem + "ь", stem + "я"]
        return out

    def match(self, text: str, language: str = "", *, country_hint: str = "",
              limit: int = 8) -> list[PlaceHit]:
        """Places mentioned in ``text``, best first (deduplicated by place)."""
        toks = tokens(text)
        found: list[tuple[str, list[tuple[int, int]], int, bool]] = []  # key, cands, n, variant
        used = [False] * len(toks)
        for n in (3, 2, 1):
            for i in range(len(toks) - n + 1):
                if any(used[i:i + n]):
                    continue
                span = toks[i:i + n]
                if span[0][1][:1].islower():  # cased scripts: place names are capitalised
                    continue
                key = " ".join(t for t, _ in span)
                if n == 1 and (key in STOPWORDS or key in AMBIGUOUS_SINGLE):
                    continue
                cands = self._lookup(key)
                variant = False
                if not cands and n == 1:
                    for v in self._variants(key):
                        cands = self._lookup(v)
                        if cands:
                            variant = True
                            break
                if cands and n == 1 and _is_acronym(span[0][1]):
                    # "IPO", "NATO": only countries may match an all-caps short token
                    cands = [c for c in cands if self.places[c[0]].precision == "country"]
                if cands:
                    found.append((" ".join(o for _, o in span), cands, n, variant))
                    for j in range(i, i + n):
                        used[j] = True
        # CJK has no spaces: scan character windows, longest first
        for tok, _orig in toks:
            if not _CJK_RE.search(tok):
                continue
            i = 0
            while i < len(tok):
                for w in range(min(self.max_cjk, len(tok) - i), 1, -1):
                    sub = tok[i:i + w]
                    if w < 3 and _KANA_RE.search(sub):
                        continue  # 2-character kana strings are mostly not place names
                    if sub in self.cjk_keys:
                        found.append((sub, self._lookup(sub), 1, False))
                        i += w
                        break
                else:
                    i += 1
        return self._disambiguate(found, country_hint, limit)

    def _disambiguate(self, found, country_hint: str, limit: int) -> list[PlaceHit]:
        mentioned = {country_hint} if country_hint else set()
        for _, cands, _, _ in found:
            for i, _ in cands:
                if self.places[i].precision == "country":
                    mentioned.add(self.places[i].country)
        best: dict[int, PlaceHit] = {}
        for matched, cands, n, variant in found:
            latin = matched[:1].isascii()
            scored = []
            for i, primary in cands:
                p = self.places[i]
                base = {"country": 0.72, "admin1": 0.45, "city": 0.5}[p.precision]
                s = base + min(0.25, math.log10(max(p.population, 1)) / 28)
                if p.precision != "country" and p.country in mentioned:
                    s += 0.2
                if primary:
                    s += 0.05
                elif latin:  # Latin alternate names collide with words/surnames
                    s -= 0.1
                elif n == 1 and p.population < 1_000_000 and p.precision == "city":
                    # non-Latin alternate names of smaller towns are often given names
                    s -= 0.2
                if n > 1:
                    s += 0.08
                if variant:
                    s -= 0.1
                scored.append((s, i))
            scored.sort(reverse=True)
            s, i = scored[0]
            top = self.places[i]
            for s2, j in scored[1:]:
                if s2 <= s - 0.05:
                    break
                other = self.places[j]
                # near-tie with a different location (not e.g. a city and its own province)
                if other.country != top.country or haversine_km(
                        top.lat, top.lon, other.lat, other.lon) > 150:
                    s -= 0.1
                    break
            conf = round(max(0.0, min(1.0, s)), 3)
            if conf < MIN_CONFIDENCE:
                continue
            p = self.places[i]
            hit = PlaceHit(name=p.name, country=p.country, admin1=p.admin1, lat=p.lat,
                           lon=p.lon, precision=p.precision, confidence=conf, matched=matched,
                           geonameid=p.pid if p.pid > 0 else None, feature=p.feature)
            if i not in best or best[i].confidence < conf:
                best[i] = hit
        hits = sorted(best.values(),
                      key=lambda h: h.confidence + _SPECIFICITY[h.precision], reverse=True)
        return hits[:limit]


_RANK = {"city": 2, "admin1": 1, "country": 0}


def best_place(hits: list[PlaceHit], confident: float = 0.65) -> PlaceHit | None:
    """The most specific place among confident hits, else the most confident hit."""
    good = [h for h in hits if h.confidence >= confident]
    if good:
        return max(good, key=lambda h: (_RANK.get(h.precision, 0), h.confidence))
    return max(hits, key=lambda h: h.confidence) if hits else None


class LazyGazetteer:
    """Loads the gazetteer once on first use (thread-safe); empty if files are missing."""

    def __init__(self, src_dir: Path) -> None:
        self.src_dir = src_dir
        self._gz: Gazetteer | None = None
        self._lock = threading.Lock()
        self.available = True

    def get(self) -> Gazetteer | None:
        if self._gz is None and self.available:
            with self._lock:
                if self._gz is None and self.available:
                    if not all((self.src_dir / f).exists() for f in SOURCE_FILES):
                        log.warning("gazetteer files missing in %s (run "
                                    "scripts/fetch_gazetteer.sh); place matching disabled",
                                    self.src_dir)
                        self.available = False
                        return None
                    try:
                        self._gz = Gazetteer.load(self.src_dir)
                    except Exception:
                        log.exception("gazetteer load failed; place matching disabled")
                        self.available = False
        return self._gz

    def match(self, text: str, language: str = "", **kw) -> list[PlaceHit]:
        gz = self.get()
        return gz.match(text, language, **kw) if gz is not None else []
