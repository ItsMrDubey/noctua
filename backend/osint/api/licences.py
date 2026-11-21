"""Static data-source licence register (commercial-readiness checklist) and attribution strings."""

from fastapi import FastAPI

from osint.webcams.sources import REGISTRY

UCDP_CITE = ("Davies, S., Pettersson, T. & Öberg, M. (2026). Organized violence 1989-2025, and violent "
             "political protests. Journal of Peace Research. https://doi.org/10.1093/jopres/xjag046; "
             "Sundberg, R. & Melander, E. (2013). Introducing the UCDP Georeferenced Event Dataset. "
             "Journal of Peace Research 50(4). UCDP is part of and funded by DEMSCORE, national research "
             "infrastructure grant 2021-00162 from the Swedish Research Council.")

# commercial: "ok" | "review" (terms need legal review before commercial launch) | "no" (non-commercial only)
SOURCES: list[dict] = [
    {"id": "esri", "name": "Esri World Imagery (base map)", "licence": "Esri terms of use", "url": "https://www.esri.com/en-us/legal/terms/full-master-agreement",
     "attribution": "Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community", "commercial": "review",
     "notes": "Free tile use is for non-commercial/evaluation; a commercial product needs an Esri licence or a different basemap."},
    {"id": "gibs", "name": "NASA GIBS", "licence": "NASA open data (public domain)", "url": "https://earthdata.nasa.gov/gibs",
     "attribution": "Imagery: NASA EOSDIS GIBS", "commercial": "ok", "notes": "Acknowledge NASA EOSDIS GIBS."},
    {"id": "cesium_ion", "name": "Cesium ion / OSM Buildings", "licence": "Cesium ion terms; OSM data ODbL 1.0", "url": "https://cesium.com/legal/terms-of-service/",
     "attribution": "Cesium ion; (c) OpenStreetMap contributors (ODbL)", "commercial": "review", "share_alike": True,
     "notes": "Community tier is non-commercial; commercial use needs a paid ion plan. OSM Buildings: ODbL attribution + share-alike on derived databases."},
    {"id": "ourairports", "name": "OurAirports", "licence": "Public domain", "url": "https://ourairports.com/data/",
     "attribution": "OurAirports (public domain)", "commercial": "ok", "notes": ""},
    {"id": "natural_earth", "name": "Natural Earth", "licence": "Public domain", "url": "https://www.naturalearthdata.com/about/terms-of-use/",
     "attribution": "Made with Natural Earth", "commercial": "ok", "notes": ""},
    {"id": "nga_wpi", "name": "NGA World Port Index", "licence": "US Government work (public domain)", "url": "https://msi.nga.mil/Publications/WPI",
     "attribution": "NGA World Port Index", "commercial": "ok", "notes": ""},
    {"id": "wikidata", "name": "Wikidata", "licence": "CC0 1.0", "url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
     "attribution": "Wikidata (CC0)", "commercial": "ok", "notes": ""},
    {"id": "adsb_lol", "name": "adsb.lol", "licence": "ODbL 1.0", "url": "https://adsb.lol/",
     "attribution": "Aircraft positions: adsb.lol (ODbL)", "commercial": "review", "share_alike": True,
     "notes": "ODbL share-alike: a derived positions database must be offered under ODbL."},
    {"id": "airplanes_live", "name": "airplanes.live", "licence": "Non-commercial use only", "url": "https://airplanes.live/api-guide/",
     "attribution": "Aircraft positions: airplanes.live", "commercial": "no", "notes": "Commercial use requires an agreement."},
    {"id": "adsb_fi", "name": "adsb.fi", "licence": "Non-commercial use only", "url": "https://adsb.fi/",
     "attribution": "Aircraft positions: adsb.fi", "commercial": "no", "notes": "Commercial use requires an agreement."},
    {"id": "aisstream", "name": "AISStream", "licence": "AISStream terms of service", "url": "https://aisstream.io/",
     "attribution": "Vessel positions: AISStream", "commercial": "review", "notes": "Beta service; review terms and SLA for commercial use."},
    {"id": "firms", "name": "NASA FIRMS", "licence": "NASA open data", "url": "https://firms.modaps.eosdis.nasa.gov/",
     "attribution": "Fire data: NASA FIRMS (LANCE/EOSDIS)", "commercial": "ok", "notes": "Acknowledge NASA FIRMS."},
    {"id": "usgs", "name": "USGS Earthquake Hazards", "licence": "US Government work (public domain)", "url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
     "attribution": "Earthquakes: USGS", "commercial": "ok", "notes": "Credit USGS as the source."},
    {"id": "gdelt", "name": "GDELT", "licence": "Free with attribution", "url": "https://www.gdeltproject.org/about.html",
     "attribution": "Data: The GDELT Project", "commercial": "ok", "notes": "Attribution and a link to gdeltproject.org required."},
    {"id": "ucdp_ged", "name": "UCDP GED", "licence": "CC BY 4.0", "url": "https://ucdp.uu.se/downloads/",
     "attribution": UCDP_CITE, "commercial": "ok", "notes": "Cite as shown (current as of UCDP GED v26.1)."},
    {"id": "alterlab", "name": "AlterLab News", "licence": "AlterLab terms of service", "url": "https://alterlab.io/",
     "attribution": "News via AlterLab", "commercial": "review", "notes": "Article text rights remain with publishers; review redistribution terms."},
    {"id": "copernicus", "name": "Copernicus Sentinel", "licence": "Copernicus open licence (free, full and open)", "url": "https://sentinels.copernicus.eu/web/sentinel/terms-and-conditions",
     "attribution": "Contains modified Copernicus Sentinel data [year]", "commercial": "ok", "notes": "Use the acquisition year; state if modified."},
]


def licences() -> dict:
    web = [{"id": f"webcam:{sid}", "name": src.name, "licence": src.licence, "url": src.url, "attribution": src.attribution,
            "commercial": "ok" if src.commercial_ok else "review", "notes": "Webcam snapshots (proxied, never stored)."}
           for sid, (src, _, _) in REGISTRY.items()]
    return {"sources": SOURCES + web, "ucdp_citation": UCDP_CITE}


def mount_licences(app: FastAPI) -> None:
    body = licences()

    @app.get("/api/sources/licences")
    def sources_licences() -> dict:
        return body
