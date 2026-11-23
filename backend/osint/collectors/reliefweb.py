"""ReliefWeb (UN OCHA) API v2: current disasters and the latest humanitarian reports.

API status checked 2026-11-23: ``/v1`` answers 410 (decommissioned); ``/v2`` needs an
*approved* ``appname`` (403 "not using an approved appname" otherwise; request one at
https://apidoc.reliefweb.int/parameters#appname). The site's RSS feeds sit behind a bot
challenge. So this collector is disabled by default and needs ``RELIEFWEB_APPNAME`` (.env) or
``reliefweb.appname`` in sources.yaml. The API firewall answers 406 ("Blocked due to bot
activity") to User-Agents containing "osint", so requests identify as ``user_agent``
(default: the app name). Response shape (API docs; same as v1):
{"totalCount", "count", "data": [{"id", "fields": {...}}]}.

* disasters (status alert / current) -> ``hazard`` observations at the primary country's
  centroid, ``precision: country`` (see ``osint.core.hazards``); version = date.changed.
* reports (newest first, since the cursor) -> ``Document`` (kind ``article``: metadata and
  link only, no body), triaged and clustered like any news item.
"""

import logging
from datetime import datetime, timedelta

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.hazards import iso, observed_ts, parse_time
from osint.core.models import Batch, Document, Observation, clip_snippet, document_id

log = logging.getLogger(__name__)

API_URL = "https://api.reliefweb.int/v2"
# ReliefWeb disaster type codes (GLIDE) -> osint.core.hazards.HAZARD_TYPES
TYPES = {"EQ": "earthquake", "TC": "tropical_cyclone", "EC": "severe_storm",
         "ST": "severe_storm", "SS": "tropical_cyclone", "FL": "flood", "FF": "flood",
         "VO": "volcano", "DR": "drought", "WF": "wildfire", "TS": "tsunami",
         "LS": "landslide", "MS": "landslide", "AV": "snow", "CW": "extreme_temperature",
         "HT": "extreme_temperature", "EP": "epidemic", "IN": "other", "AC": "manmade",
         "OT": "other"}
# field names / filters validated live 2026-11-23 (the API checks them before the appname:
# 400 for an unknown field, e.g. "primary_country.location"; the whole object carries it)
DISASTER_FIELDS = ["id", "name", "glide", "status", "date.created", "date.changed",
                   "date.event", "primary_country", "country.name", "country.iso3",
                   "primary_type.code", "primary_type.name", "url_alias", "url"]
REPORT_FIELDS = ["id", "title", "url_alias", "url", "date.created", "date.original",
                 "source.shortname", "source.name", "primary_country.name",
                 "primary_country.iso3", "country.name", "disaster.name",
                 "disaster_type.name", "language.code", "format.name"]
REPORT_MAX_AGE = timedelta(hours=47)  # ingest drops documents published > 48 h ago
USER_AGENT = "noctua/0.1 (ReliefWeb API client; humanitarian situational awareness)"


def _names(items: object, key: str = "name") -> list[str]:
    if isinstance(items, dict):
        items = [items]
    return [str(i[key]) for i in items or [] if isinstance(i, dict) and i.get(key)]


def parse_disaster(item: dict, now: datetime) -> Observation | None:
    """One /disasters item -> Observation (None without a located primary country)."""
    f = item.get("fields") or {}
    pc = f.get("primary_country") or {}
    loc = pc.get("location") or {}
    if loc.get("lat") is None or loc.get("lon") is None:
        return None
    dates = f.get("date") or {}
    changed = parse_time(dates.get("changed")) or parse_time(dates.get("created"))
    started = parse_time(dates.get("event")) or parse_time(dates.get("created"))
    ptype = f.get("primary_type") or {}
    code = str(ptype.get("code") or "").upper()
    rid = f.get("id") or item["id"]
    return Observation(
        id=f"hazard:reliefweb:{rid}", version=str(dates.get("changed") or ""),
        type="hazard", ts=observed_ts(changed, now), lat=float(loc["lat"]),
        lon=float(loc["lon"]), source="reliefweb", value=None,
        attrs={
            "source": "reliefweb", "hazard": TYPES.get(code, "other"), "code": code,
            "event_id": rid, "name": f.get("name") or "", "alert_level": None,
            "status": f.get("status"), "glide": f.get("glide") or "",
            "description": ptype.get("name") or "", "severity_text": "",
            "current": f.get("status") in ("alert", "current", "ongoing"),
            "country": pc.get("name") or "", "iso3": str(pc.get("iso3") or "").upper(),
            "countries": _names(f.get("country")), "iso2": [],
            "iso3s": [x.upper() for x in _names(f.get("country"), "iso3")],
            "t_start": iso(started), "t_end": None, "updated_at": iso(changed),
            "precision": "country", "agency": "ReliefWeb",
            "url": f.get("url_alias") or f.get("url"),
        },
    )


def parse_report(item: dict, now: datetime) -> Document | None:
    """One /reports item -> Document (None when too old for ingest or without a URL)."""
    f = item.get("fields") or {}
    url = f.get("url_alias") or f.get("url")
    title = " ".join(str(f.get("title") or "").split())
    dates = f.get("date") or {}
    published = parse_time(dates.get("created")) or parse_time(dates.get("original"))
    if not url or not title or published is None or now - published > REPORT_MAX_AGE:
        return None
    sources = _names(f.get("source"), "shortname") or _names(f.get("source"))
    countries = _names(f.get("country")) or _names(f.get("primary_country"))
    disasters = _names(f.get("disaster"))
    kinds = _names(f.get("disaster_type"))
    formats = _names(f.get("format"))
    langs = _names(f.get("language"), "code")
    snippet = " · ".join(x for x in (", ".join(sources), ", ".join(formats),
                                      ", ".join(countries), "; ".join(disasters or kinds)) if x)
    rid = str(f.get("id") or item.get("id") or "")
    return Document(
        id=document_id(url), source="reliefweb", url=url, title=title,
        snippet=clip_snippet(snippet), language=(langs[0].lower() if langs else ""),
        published_at=min(published, now), fetched_at=now, kind="article", origin_id=rid,
        attrs={"reliefweb_id": rid, "sources": sources, "countries": countries,
               "disasters": disasters, "disaster_types": kinds, "format": formats,
               "original_date": dates.get("original"),
               "licence": "ReliefWeb (UN OCHA); content rights remain with each source"},
    )


@register
class ReliefWebCollector(PollCollector):
    name = "reliefweb"
    domain = "hazards"
    licence = "ReliefWeb (UN OCHA) API terms; report content rights remain with each source"
    commercial_ok = False
    default_interval_s = 3600.0
    poll_timeout_s = 120.0

    def _appname(self, ctx: CollectorContext) -> str:
        name = str(self.cfg.get("appname") or getattr(ctx.settings, "reliefweb_appname", "")
                   or "").strip()
        if not name:
            raise RuntimeError("reliefweb: no approved appname (set RELIEFWEB_APPNAME; request "
                               "one at https://apidoc.reliefweb.int/parameters#appname)")
        return name

    async def _get(self, ctx: CollectorContext, path: str, params: dict) -> list[dict]:
        r = await ctx.http.get(f"{self.cfg.get('url', API_URL)}/{path}", params=params,
                               headers={"User-Agent": self.cfg.get("user_agent", USER_AGENT)},
                               timeout=60)
        if r.status_code == 406:
            raise RuntimeError(f"reliefweb: request blocked by the API firewall (HTTP 406): "
                               f"{r.text[:200]}")
        if r.status_code == 403:
            raise RuntimeError(f"reliefweb: appname rejected (HTTP 403): {r.text[:200]}")
        r.raise_for_status()
        data = (r.json() or {}).get("data")
        if not isinstance(data, list):
            raise TypeError(f"unexpected ReliefWeb {path} payload (no data list)")
        return data

    async def poll(self, ctx: CollectorContext) -> Batch:
        now = utcnow()
        app = self._appname(ctx)
        batch = Batch(source=self.name)
        skipped = 0
        disasters = await self._get(ctx, "disasters", {
            "appname": app, "limit": int(self.cfg.get("disaster_limit", 100)),
            "fields[include][]": DISASTER_FIELDS, "sort[]": "date.changed:desc",
            "filter[field]": "status", "filter[value][]": ["alert", "current", "ongoing"],
            "filter[operator]": "OR"})
        for item in disasters:
            try:
                obs = parse_disaster(item, now)
            except (KeyError, ValueError, TypeError, AttributeError):
                skipped += 1
                continue
            if obs is not None:
                batch.observations.append(obs)
        if self.cfg.get("reports", True):
            cur = ctx.get_cursor(self.name) or {}
            since = parse_time(cur.get("reports_since")) or now - REPORT_MAX_AGE
            since = max(since - timedelta(hours=1), now - REPORT_MAX_AGE)  # 1 h overlap
            reports = await self._get(ctx, "reports", {
                "appname": app, "limit": int(self.cfg.get("report_limit", 100)),
                "fields[include][]": REPORT_FIELDS, "sort[]": "date.created:desc",
                "filter[field]": "date.created",
                "filter[value][from]": since.strftime("%Y-%m-%dT%H:%M:%S+00:00")})
            newest = since
            for item in reports:
                try:
                    doc = parse_report(item, now)
                except (KeyError, ValueError, TypeError, AttributeError):
                    skipped += 1
                    continue
                if doc is not None:
                    batch.documents.append(doc)
                    newest = max(newest, doc.published_at)
            ctx.set_cursor(self.name, {"reports_since": newest.isoformat(),
                                       "last_poll": now.isoformat()})
        if skipped:
            log.warning("reliefweb: skipped %d malformed record(s)", skipped)
        return batch
