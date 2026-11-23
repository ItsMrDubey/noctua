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
     "attribution": "Imagery: NASA EOSDIS GIBS", "commercial": "ok", "notes": "Acknowledge NASA EOSDIS GIBS. Also serves the GOES-East/West and Himawari band-13 infrared cloud layer (GOES: NOAA; Himawari: JMA)."},
    {"id": "eumetsat_view", "name": "EUMETSAT View Service (Meteosat MTG-I / MSG IODC infrared cloud imagery, WMS)", "licence": "EUMETSAT Data Policy", "url": "https://view.eumetsat.int/",
     "attribution": "Contains EUMETSAT Meteosat data (c) EUMETSAT", "commercial": "review",
     "notes": "Keyless public WMS (Fees/AccessConstraints: none in its capabilities). Imagery must carry a EUMETSAT attribution; check the EUMETSAT Data Policy (doc 45173) for near-real-time commercial redistribution before launch."},
    {"id": "cesium_ion", "name": "Cesium ion / OSM Buildings", "licence": "Cesium ion terms; OSM data ODbL 1.0", "url": "https://cesium.com/legal/terms-of-service/",
     "attribution": "Cesium ion; (c) OpenStreetMap contributors (ODbL)", "commercial": "review", "share_alike": True,
     "notes": "Community tier is non-commercial; commercial use needs a paid ion plan. OSM Buildings: ODbL attribution + share-alike on derived databases."},
    {"id": "open_meteo", "name": "Open-Meteo (3D cloud layer; intel console site weather and pass cloud forecast)", "licence": "CC BY 4.0 data; free API non-commercial", "url": "https://open-meteo.com/en/terms",
     "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)", "commercial": "no",
     "notes": "The free API is non-commercial; commercial use needs an Open-Meteo API subscription (or self-hosting the open-source server)."},
    {"id": "rainviewer", "name": "RainViewer (precipitation radar tiles)", "licence": "RainViewer API terms", "url": "https://www.rainviewer.com/api.html",
     "attribution": "Weather data by RainViewer (rainviewer.com)", "commercial": "no",
     "notes": "Personal / educational use only (commercial use needs custom terms). Since 2026-01: zoom <= 7, Universal Blue colour scheme only, 100 requests/IP/min, no satellite IR or nowcast. Credit with a link to rainviewer.com."},
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
     "attribution": "Aircraft positions: airplanes.live", "commercial": "no",
     "notes": "Commercial use requires an agreement. Since 2026 the API answers 403 unless the project is approved (contact@airplanes.live); not polled by default."},
    {"id": "adsb_fi", "name": "adsb.fi", "licence": "Non-commercial use only", "url": "https://adsb.fi/",
     "attribution": "Aircraft positions: adsb.fi", "commercial": "no", "notes": "Commercial use requires an agreement."},
    {"id": "opensky", "name": "OpenSky Network (anonymous REST API)", "licence": "OpenSky Network terms of use (non-commercial research)", "url": "https://opensky-network.org/about/terms-of-use",
     "attribution": "Aircraft positions: The OpenSky Network, https://opensky-network.org", "commercial": "no",
     "notes": "ADS-B fallback only (when adsb.lol and adsb.fi both fail), <= 1 call per 15 min within the 400 anonymous daily credits. Commercial use needs a licence."},
    {"id": "plane_alert_db", "name": "plane-alert-db (military / government aircraft by ICAO hex)", "licence": "ODbL 1.0; contents DbCL 1.0",
     "url": "https://github.com/sdr-enthusiasts/plane-alert-db", "attribution": "Aircraft data: plane-alert-db, SDR-Enthusiasts and contributors (ODbL)",
     "commercial": "review", "share_alike": True, "notes": "reference/plane-alert-mil.csv: operator, type, role tags, category and link shown in asset profiles."},
    {"id": "aisstream", "name": "AISStream", "licence": "AISStream terms of service", "url": "https://aisstream.io/",
     "attribution": "Vessel positions: AISStream", "commercial": "review", "notes": "Beta service; review terms and SLA for commercial use."},
    {"id": "firms", "name": "NASA FIRMS", "licence": "NASA open data", "url": "https://firms.modaps.eosdis.nasa.gov/",
     "attribution": "Fire data: NASA FIRMS (LANCE/EOSDIS)", "commercial": "ok", "notes": "Acknowledge NASA FIRMS."},
    {"id": "usgs", "name": "USGS Earthquake Hazards", "licence": "US Government work (public domain)", "url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
     "attribution": "Earthquakes: USGS", "commercial": "ok", "notes": "Credit USGS as the source."},
    {"id": "gdacs", "name": "GDACS (Global Disaster Alert and Coordination System)", "licence": "Free use with attribution (EC JRC / UN OCHA)", "url": "https://www.gdacs.org/About/termofuse.aspx",
     "attribution": "Disaster alerts: GDACS, a cooperation framework of the UN and the European Commission", "commercial": "review",
     "notes": "Alerts are automatic model estimates; credit GDACS. Review the terms of use before commercial launch."},
    {"id": "eonet", "name": "NASA EONET v3", "licence": "NASA open data (public domain)", "url": "https://eonet.gsfc.nasa.gov/what-is-eonet",
     "attribution": "Natural events: NASA Earth Observatory Natural Event Tracker (EONET)", "commercial": "ok",
     "notes": "Acknowledge NASA EONET; each event also credits its upstream source (IRWIN, JTWC, SIVolcano, ...)."},
    {"id": "reliefweb", "name": "ReliefWeb API (UN OCHA)", "licence": "ReliefWeb terms; content rights remain with each source", "url": "https://reliefweb.int/terms-conditions",
     "attribution": "Disasters and reports: ReliefWeb (UN OCHA)", "commercial": "review",
     "notes": "API v2 requires an approved appname (RELIEFWEB_APPNAME). Metadata and links only; report content belongs to each publishing organisation."},
    {"id": "gdelt", "name": "GDELT", "licence": "Free with attribution", "url": "https://www.gdeltproject.org/about.html",
     "attribution": "Data: The GDELT Project", "commercial": "ok", "notes": "Attribution and a link to gdeltproject.org required."},
    {"id": "ucdp_ged", "name": "UCDP GED", "licence": "CC BY 4.0", "url": "https://ucdp.uu.se/downloads/",
     "attribution": UCDP_CITE, "commercial": "ok", "notes": "Cite as shown (current as of UCDP GED v26.1)."},
    {"id": "ucdp_candidate", "name": "UCDP Candidate Events (monthly)", "licence": "CC BY 4.0", "url": "https://ucdp.uu.se/downloads/#candidate",
     "attribution": UCDP_CITE, "commercial": "ok", "notes": "Public monthly CSVs (no API token needed); cite as for UCDP GED."},
    {"id": "acled", "name": "ACLED", "licence": "ACLED terms of use (myACLED account)", "url": "https://acleddata.com/terms-of-use/",
     "attribution": "Armed Conflict Location & Event Data (ACLED); www.acleddata.com", "commercial": "no",
     "notes": "Event data depends on the account's access level; redistribution and commercial use need an ACLED licence."},
    {"id": "telegram", "name": "Telegram public channels (web preview)", "licence": "Telegram terms of service; content rights stay with channel owners",
     "url": "https://telegram.org/tos", "attribution": "Posts from public Telegram channels (linked)", "commercial": "review",
     "notes": "Only metadata, a short snippet and the t.me link are stored; review channel content rights before commercial use."},
    {"id": "alterlab", "name": "AlterLab News", "licence": "AlterLab terms of service", "url": "https://alterlab.io/",
     "attribution": "News via AlterLab", "commercial": "review", "notes": "Article text rights remain with publishers; review redistribution terms."},
    {"id": "deepstatemap", "name": "DeepStateMap.live (Ukraine front line)", "licence": "DeepStateMap terms (free use with attribution and a link)", "url": "https://deepstatemap.live/",
     "attribution": "Front line: DeepStateMap.live", "commercial": "review",
     "notes": "Fetched a few times a day by the fire scorer; the front line is derived from the occupied / grey-zone polygons. Check redistribution terms before a commercial launch."},
    {"id": "gvp", "name": "Smithsonian GVP Volcanoes of the World", "licence": "Smithsonian terms of use (free with citation)", "url": "https://volcano.si.edu/",
     "attribution": "Global Volcanism Program, Smithsonian Institution: Volcanoes of the World (Holocene volcano list)", "commercial": "review",
     "notes": "Used to demote volcanic thermal anomalies in the fire score; cite the GVP database version."},
    {"id": "eog_vnf", "name": "EOG VIIRS Nightfire global gas flares", "licence": "CC BY 4.0", "url": "https://eogdata.mines.edu/products/vnf/global_gas_flare.html",
     "attribution": "Gas flare sites: Earth Observation Group, Payne Institute for Public Policy, Colorado School of Mines", "commercial": "ok",
     "notes": "Annual flare site list; used to demote flares in the fire score."},
    {"id": "copernicus", "name": "Copernicus Sentinel", "licence": "Copernicus open licence (free, full and open)", "url": "https://sentinels.copernicus.eu/web/sentinel/terms-and-conditions",
     "attribution": "Contains modified Copernicus Sentinel data [year]", "commercial": "ok", "notes": "Use the acquisition year; state if modified."},
    # signals & space layers (osint.signals)
    {"id": "adsb_fi_signals", "name": "adsb.fi open data (GNSS interference, emergency squawks)", "licence": "Non-commercial use only", "url": "https://adsb.fi/",
     "attribution": "GNSS interference and squawks derived from adsb.fi data", "commercial": "no",
     "notes": "All-traffic point / squawk queries, 1 request/s. Method after gpsjam.org (NIC / NACp share per H3 cell)."},
    {"id": "celestrak", "name": "CelesTrak GP element sets", "licence": "CelesTrak terms (free; credit requested)", "url": "https://celestrak.org/NORAD/documentation/gp-data-formats.php",
     "attribution": "Orbital data: CelesTrak", "commercial": "review",
     "notes": "Derived from US Space Force (18 SDS) data; fetched at most every 2 h per group. Check redistribution terms before a commercial launch."},
    {"id": "satellite_js", "name": "satellite.js 6.0.1 (SGP4)", "licence": "MIT", "url": "https://github.com/shashwatak/satellite-js",
     "attribution": "satellite.js (MIT)", "commercial": "ok", "notes": "Client-side propagation; vendored by scripts/fetch_satellitejs.sh or loaded from jsDelivr."},
    {"id": "sgp4", "name": "sgp4 (Python SGP4 propagator; intel console imaging passes)", "licence": "MIT", "url": "https://pypi.org/project/sgp4/",
     "attribution": "sgp4 by Brandon Rhodes (MIT)", "commercial": "ok", "notes": "Library, not data; pass times are computed locally from CelesTrak elements."},
    {"id": "wikimedia_pageviews", "name": "Wikimedia pageviews API (intel console attention)", "licence": "CC0 1.0 (pageview data)", "url": "https://wikitech.wikimedia.org/wiki/Analytics/AQS/Pageviews",
     "attribution": "Pageviews: Wikimedia Foundation (CC0)", "commercial": "ok",
     "notes": "Wikimedia REST API; requests must carry a User-Agent with contact details (OSINT_CONTACT). Cached 6 h per article."},
    {"id": "geonames", "name": "GeoNames gazetteer (geocoding; intel console settlement population)", "licence": "CC BY 4.0", "url": "https://www.geonames.org/about.html",
     "attribution": "Place data: GeoNames (CC BY 4.0)", "commercial": "ok",
     "notes": "Population figures are GeoNames' per-place values; the console's ring totals are settlement-based estimates, not gridded population."},
    {"id": "ioda", "name": "IODA (Georgia Tech Internet Outage Detection and Analysis)", "licence": "IODA terms of use (research use, attribution)", "url": "https://ioda.inetintel.cc.gatech.edu/",
     "attribution": "Internet outage data: IODA, Georgia Institute of Technology", "commercial": "review",
     "notes": "Free API, no key. Commercial use needs agreement with the IODA team. Cloudflare Radar was not used (its API requires a token)."},
    {"id": "swpc", "name": "NOAA Space Weather Prediction Center", "licence": "US Government work (public domain)", "url": "https://www.swpc.noaa.gov/",
     "attribution": "Space weather: NOAA SWPC (OVATION aurora nowcast)", "commercial": "ok", "notes": "Credit NOAA SWPC."},
    {"id": "bfs_odl", "name": "BfS ODL (German ambient gamma dose rate network)", "licence": "Datenlizenz Deutschland - Namensnennung 2.0 (dl-de/by-2-0)", "url": "https://odlinfo.bfs.de/",
     "attribution": "Dose rate: Bundesamt für Strahlenschutz (BfS), dl-de/by-2-0", "commercial": "ok", "notes": "Attribution required."},
    {"id": "safecast", "name": "Safecast realtime devices", "licence": "CC0 1.0", "url": "https://safecast.org/data/",
     "attribution": "Dose rate: Safecast (CC0)", "commercial": "ok", "notes": "Citizen-science sensors; CPM converted to µSv/h with the tube factor."},
]


def licences() -> dict:
    web = [{"id": f"webcam:{sid}", "name": src.name, "licence": src.licence, "url": src.url, "attribution": src.attribution,
            "commercial": "ok" if src.commercial_ok else "review", "notes": "Webcam snapshots (proxied, never stored)."}
           for sid, (src, _, _) in REGISTRY.items()]
    from osint.infra.datasets import DATASETS  # infrastructure layers + watchlists

    have = {s["id"] for s in SOURCES}
    infra = [{"id": d.id if d.id not in have else f"infra:{d.id}", "name": d.name, "licence": d.licence,
              "url": d.url, "attribution": d.attribution, "commercial": d.commercial,
              "notes": d.notes, **({"share_alike": True} if "ODbL" in d.licence else {})}
             for d in DATASETS.values() if d.id != "wpi"]  # WPI is listed as nga_wpi
    from osint.profiles.lookups import SPECS as PROFILE_LOOKUPS  # asset profile lookups

    prof = [{"id": f"profile:{s.id}", "name": s.name, "licence": s.licence, "url": s.url,
             "attribution": s.attribution, "commercial": s.commercial, "notes": s.notes}
            for s in PROFILE_LOOKUPS.values()]
    return {"sources": SOURCES + infra + web + prof, "ucdp_citation": UCDP_CITE}


def mount_licences(app: FastAPI) -> None:
    body = licences()

    @app.get("/api/sources/licences")
    def sources_licences() -> dict:
        return body
