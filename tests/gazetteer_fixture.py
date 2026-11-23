"""A tiny GeoNames-format gazetteer for tests (no download needed)."""

import zipfile
from pathlib import Path

# geonameid, name, asciiname, alternatenames, lat, lon, fclass, fcode, cc, admin1, pop
CITIES = [
    (706483, "Kharkiv", "Kharkiv", "Charkiw,Kharkov,Харків,Харьков,خاركيف", 49.98, 36.25,
     "PPLA", "UA", "07", 1430885),
    (703448, "Kyiv", "Kyiv", "Kiev,Kiew,Київ,Киев,كييف,基輔", 50.45, 30.52, "PPLC", "UA", "12",
     2797553),
    (276781, "Beirut", "Beirut", "Beyrouth,Бейрут,بيروت,بیروت", 33.89, 35.50, "PPLC", "LB",
     "04", 1916100),
    (418863, "Isfahan", "Isfahan", "Esfahan,Исфахан,اصفهان", 32.65, 51.67, "PPLA", "IR", "28",
     1547164),
    (2988507, "Paris", "Paris", "Parigi,Париж,باريس,巴黎", 48.85, 2.35, "PPLC", "FR", "11",
     2138551),
    (4717560, "Paris", "Paris", "", 33.66, -95.56, "PPLA2", "US", "TX", 24782),
    (79415, "Al Hudaydah", "Al Hudaydah", "Hodeidah,Hudaydah,الحديدة", 14.80, 42.95, "PPLA",
     "YE", "08", 617871),
    (2867714, "Munich", "Munich", "Muenchen,München,Мюнхен", 48.14, 11.58, "PPLA", "DE", "02",
     1260391),
    (3089999, "Police", "Police", "", 53.55, 14.57, "PPL", "PL", "84", 33000),
    (999001, "Nice", "Nice", "", 43.70, 7.27, "PPLA2", "FR", "93", 342522),
    (999002, "Ipoh", "Ipoh", "Ipo", 4.58, 101.08, "PPLA", "MY", "08", 673318),
    # a town that is also a common surname; a current name that is another city's old name
    (1266999, "Kim", "Kim", "", 21.39, 72.85, "PPL", "IN", "09", 20000),
    (704508, "Pokrovsk", "Pokrovsk", "Покровськ", 48.28, 37.18, "PPL", "UA", "05", 60127),
    (563464, "Engels", "Engels", "Pokrovsk,Энгельс", 51.50, 46.12, "PPL", "RU", "67", 196011),
]
COUNTRIES = [("UA", "UKR", "Ukraine"), ("LB", "LBN", "Lebanon"), ("IR", "IRN", "Iran"),
             ("FR", "FRA", "France"), ("US", "USA", "United States"), ("YE", "YEM", "Yemen"),
             ("DE", "DEU", "Germany"), ("PL", "POL", "Poland"), ("MY", "MYS", "Malaysia"),
             ("IN", "IND", "India"), ("RU", "RUS", "Russia")]
ADMIN1 = [("UA.07", "Kharkiv"), ("UA.12", "Kyiv City"), ("US.TX", "Texas"),
          ("FR.11", "Île-de-France")]


def write_gazetteer(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    lines = []
    for gid, name, ascii_, alts, lat, lon, fcode, cc, a1, pop in CITIES:
        cols = [str(gid), name, ascii_, alts, str(lat), str(lon), "P", fcode, cc, "", a1,
                "", "", "", str(pop), "", "100", "Europe/X", "2026-01-01"]
        lines.append("\t".join(cols))
    with zipfile.ZipFile(dest / "cities15000.zip", "w") as z:
        z.writestr("cities15000.txt", "\n".join(lines) + "\n")
    info = ["#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital"]
    info += [f"{cc}\t{iso3}\t000\t{cc}\t{name}\tX" for cc, iso3, name in COUNTRIES]
    (dest / "countryInfo.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
    (dest / "admin1CodesASCII.txt").write_text(
        "\n".join(f"{code}\t{name}\t{name}\t1" for code, name in ADMIN1) + "\n",
        encoding="utf-8")
    return dest
