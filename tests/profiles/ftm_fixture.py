"""A small FollowTheMoney graph shaped like the OpenSanctions exports: a sanctioned tanker with a
dated owner / manager history (War & Sanctions style), an owner whose parent is linked by an
"Owned or Controlled By" UnknownLink, a sanctioned aircraft (verbatim OFAC rows for RA-85041)
and noise (a person, an unrelated sanction)."""

import json
from pathlib import Path


def ent(id_, schema, props, datasets=("us_ofac_sdn",), **kw):
    return {"id": id_, "caption": (props.get("name") or [schema])[0], "schema": schema,
            "referents": kw.get("referents", []), "datasets": list(datasets),
            "first_seen": "2025-01-10T15:53:02", "last_seen": "2026-11-23T06:53:07",
            "last_change": kw.get("last_change", "2026-11-16T09:16:08"), "properties": props}


SANCTIONS = [
    ent("NK-tanker", "Vessel", {
        "name": ["VELMAR"], "previousName": ["Venture", "Crudemed"], "pastFlags": ["pa", "km"],
        "flag": ["bb"], "imoNumber": ["IMO9832547"], "mmsi": ["273278060"], "type": ["Crude Oil Tanker"],
        "buildDate": ["2018"], "topics": ["sanction"], "programId": ["US-RUSHAR", "EU-MARE"],
        "sourceUrl": ["https://sanctionssearch.ofac.treas.gov/Details.aspx?id=51596"]},
        referents=["imo-vsl-9832547", "ofac-51596"]),
    ent("ofac-s1", "Sanction", {
        "entity": ["NK-tanker"], "authority": ["Office of Foreign Assets Control"], "country": ["us"],
        "program": ["RUSSIA-EO14024"], "programId": ["US-RUSHAR"], "reason": ["Executive Order 14024 (Russia)"],
        "sourceUrl": ["https://www.treasury.gov/resource-center/sanctions/Pages/default.aspx"]}),
    ent("eu-s1", "Sanction", {
        "entity": ["NK-tanker"], "authority": ["Council of the European Union"], "country": ["eu"],
        "programId": ["EU-MARE"], "startDate": ["2025-05-21"],
        "reason": [("Article 3s(2), point (b): transport crude oil or petroleum products that originate "
                   "in Russia while practicing irregular and high-risk shipping practices")],
        "sourceUrl": ["https://eur-lex.europa.eu/"]}, datasets=("eu_journal_sanctions",)),
    ent("own-1", "Ownership", {"asset": ["NK-tanker"], "owner": ["NK-glory"]}),
    ent("own-2", "Ownership", {"asset": ["NK-tanker"], "owner": ["NK-prominent"],
                               "role": ["Property in the interest of"]}),
    ent("NK-glory", "Company", {"name": ["Glory Shipping HK Limited"], "country": ["hk"],
                                "topics": ["sanction"]}),
    ent("uk-s2", "Sanction", {"entity": ["NK-glory"], "authority": ["UK"], "programId": ["GB-RUS"],
                              "startDate": ["2025-07-21"], "reason": ["Owner of VELMAR"]},
        datasets=("gb_fcdo_sanctions",)),
    ent("NK-prominent", "Company", {"name": ["Prominent Shipmanagement Zrt."], "country": ["hu"],
                                    "topics": ["poi"]}),
    ent("link-parent", "UnknownLink", {"subject": ["NK-prominent"], "object": ["NK-parent"],
                                       "role": ["Owned or Controlled By"]}),
    ent("NK-parent", "Company", {"name": ["Parent Holding Ltd"], "country": ["ae"], "topics": ["sanction"]}),
    ent("ofac-43200", "Airplane", {
        "topics": ["sanction"], "name": ["RA-85041"], "registrationNumber": ["RA-85041"],
        "sourceUrl": ["https://sanctionssearch.ofac.treas.gov/Details.aspx?id=43200"],
        "serialNumber": ["12A997"], "programId": ["US-RUSHAR"], "model": ["TU-154"]},
        datasets=("us_ofac_sdn", "us_trade_csl")),
    ent("ofac-own-air", "Ownership", {"role": ["Property in the interest of"], "asset": ["ofac-43200"],
                                      "owner": ["NK-223"]}),
    ent("NK-223", "Organization", {"name": ["223rd Flight Unit State Airlines"], "country": ["ru"],
                                   "topics": ["sanction", "export.control"]}),
    ent("ofac-s-air", "Sanction", {"entity": ["ofac-43200"], "authority": ["Office of Foreign Assets Control"],
                                   "programId": ["US-RUSHAR"], "reason": ["Executive Order 14024 (Russia)"]}),
    ent("NK-unrelated", "Person", {"name": ["Somebody Else"], "topics": ["sanction"]}),
    ent("x-s9", "Sanction", {"entity": ["NK-unrelated"], "authority": ["UN"], "programId": ["UN-X"]}),
]
UA_WAR = [
    # the same tanker from the second export: properties are merged
    ent("NK-tanker", "Vessel", {"name": ["VELMAR"], "previousName": ["Velmar"],
                                "description": ["The tanker is involved in the export of russian oil."],
                                "topics": ["mare.shadow"], "deadweightTonnage": ["115643"]},
        datasets=("ua_war_sanctions",)),
    ent("ua-own", "Ownership", {"asset": ["NK-tanker"], "owner": ["NK-idas"], "role": ["owner"],
                                "startDate": ["2025-12-17"]}, datasets=("ua_war_sanctions",)),
    ent("ua-mgr", "UnknownLink", {"subject": ["NK-idas"], "object": ["NK-tanker"],
                                  "role": ["commerce manager"], "startDate": ["2025-12-17"]},
        datasets=("ua_war_sanctions",)),
    ent("NK-idas", "Company", {"name": ["Idas LLC"], "country": ["ru"], "topics": ["poi"]},
        datasets=("ua_war_sanctions",)),
]


def write(dirpath: Path) -> list[Path]:
    paths = []
    for name, rows in (("sanctions.ftm.json", SANCTIONS), ("ua_war.ftm.json", UA_WAR)):
        p = dirpath / name
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        paths.append(p)
    return paths
