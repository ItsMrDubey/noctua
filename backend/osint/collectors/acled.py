"""ACLED (Armed Conflict Location & Event Data) recent events -> curated conflict events.

Auth: ACLED no longer issues API keys. Programmatic access is OAuth with the myACLED login
(``ACLED_EMAIL`` / ``ACLED_PASSWORD``): POST https://acleddata.com/oauth/token
(grant_type=password, client_id=acled, scope=authenticated) returns a Bearer access token
(24 h) and a refresh token (14 d). Tokens live in memory only and are never logged or put in
the cursor; the access token is refreshed shortly before expiry, and a 401 forces one fresh
login. Data: GET https://acleddata.com/api/acled/read (JSON, paged with ``limit``/``page``).

Incremental: the first poll asks for ``event_date`` in the last ``days`` days; later polls ask
for rows whose ``timestamp`` (last upload to the API) is newer than the cursor, so revised
rows are re-fetched and upserted.

Without credentials the collector logs one message and idles. An account whose access level
lacks event-level API data gets ``{"message": "Access denied"}``; that is a tier problem, not
a transient failure: one warning, then no requests for ``denied_backoff_h`` hours (persisted
in the cursor so a restart does not retry immediately). Rejected credentials back off the
same way.

Events are written straight to the event store (``save_events``, idempotent upsert keyed
``acled-<event_id_cnty>``) like the UCDP backfill: ACLED rows are analyst-coded and lag
days to weeks, so they would be dropped as stale by the 48 h document ingest and need no
clustering. Origin/status ``historical`` (curated dataset, never touched by news-event
maintenance); ``updated_at`` is the event date so they do not flood "recently updated".
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import h3
import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import h3_cells, utcnow, valid_coord
from osint.core.models import Batch, clip_snippet
from osint.store.sqlite import HISTORICAL_STATUS

log = logging.getLogger(__name__)

TOKEN_URL = "https://acleddata.com/oauth/token"
READ_URL = "https://acleddata.com/api/acled/read"
SOURCE = "acled"
LICENCE = ("ACLED terms of use - Armed Conflict Location & Event Data (ACLED), "
           "www.acleddata.com (attribution required)")
PAGE_SIZE = 5000  # API default/maximum rows per page
MAX_PAGES = 10
BATCH = 250  # events per save_events transaction
TOKEN_MARGIN_S = 300.0  # refresh this long before the access token expires
# ACLED event_type -> our category (spec §3 taxonomy)
CATEGORIES = {
    "battles": "armed_conflict",
    "explosions/remote violence": "armed_conflict",
    "violence against civilians": "armed_conflict",
    "protests": "civil_unrest",
    "riots": "civil_unrest",
    "strategic developments": "political",
}
# sub-event types of "Strategic developments" that are unrest rather than politics
SUB_CATEGORIES = {"looting/property destruction": "civil_unrest"}
# geo_precision (ACLED codebook): 1 the named town/location, 2 a nearby or part-of-region
# location, 3 a larger region (provincial capital or admin centroid)
GEO_PREC = {1: ("city", 0.85), 2: ("admin1", 0.6), 3: ("admin1", 0.4)}
# time_precision: 1 exact day, 2 within the week, 3 within the month
TIME_PREC = {1: 1.0, 2: 0.7, 3: 0.5}


class AcledAuthError(RuntimeError):
    """Credentials rejected by the token endpoint."""


class AcledAccessDenied(RuntimeError):
    """The account's access level does not include API event data."""


def _int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def _s(row: dict[str, Any], key: str) -> str:
    v = row.get(key)
    return "" if v is None else str(v).strip()


def category(event_type: str, sub_event_type: str) -> str:
    return (SUB_CATEGORIES.get(sub_event_type.casefold())
            or CATEGORIES.get(event_type.casefold(), "armed_conflict"))


def confidence(geo_prec: int | None, time_prec: int | None) -> float:
    geo = GEO_PREC.get(geo_prec or 0, ("admin1", 0.3))[1]
    when = TIME_PREC.get(time_prec or 0, 0.3)
    return round(0.6 * geo + 0.4 * when, 4)


def map_row(row: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """One ACLED API row -> {"event", "document", "fields"} for ``save_events``; None if
    the row lacks an id, date or valid coordinates."""
    uid = _s(row, "event_id_cnty")
    lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
    if not uid or not valid_coord(lat, lon):
        return None
    day = datetime.strptime(_s(row, "event_date")[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    event_type, sub_type = _s(row, "event_type"), _s(row, "sub_event_type")
    cat = category(event_type, sub_type)
    geo_prec, time_prec = _int(row.get("geo_precision")), _int(row.get("time_precision"))
    prec, geo_conf = GEO_PREC.get(geo_prec or 0, ("admin1", 0.3))
    fatalities = _int(row.get("fatalities"), 0) or 0
    actor1, actor2 = _s(row, "actor1"), _s(row, "actor2")
    location, admin1, country = _s(row, "location"), _s(row, "admin1"), _s(row, "country")
    where = ", ".join(p for p in (location, admin1 if admin1 != location else "", country) if p)
    who = " vs ".join(a for a in (actor1, actor2) if a)
    title = clip_snippet(f"{sub_type or event_type or 'Event'}"
                         + (f": {who}" if who else "")
                         + (f" in {where}" if where else "")
                         + (f" ({fatalities} killed)" if fatalities else ""))
    notes = clip_snippet(_s(row, "notes"))
    c3, c5, c7 = h3_cells(lat, lon)
    sources = [s.strip() for s in _s(row, "source").split(";") if s.strip()]
    event_id, doc_id = f"acled-{uid}", f"acled:{uid}"
    t = _iso(day)
    t_end = _iso(day + timedelta(days=1) - timedelta(milliseconds=1))
    event = {
        "id": event_id, "category": cat,
        "subcategory": (sub_type or event_type).casefold() or None, "cameo_root": None,
        "title": title, "summary": notes,
        "actors": [a for a in (
            {"name": actor1, "role": "actor1", "type": _s(row, "inter1"), "country": ""},
            {"name": actor2, "role": "actor2", "type": _s(row, "inter2"), "country": ""})
            if a["name"]],
        "lat": lat, "lon": lon, "precision": prec, "place_name": location or admin1 or None,
        "country": country or None, "geo_confidence": geo_conf,
        "h3_3": h3.int_to_str(c3), "h3_5": h3.int_to_str(c5), "h3_7": h3.int_to_str(c7),
        "t_start": t, "t_end": t_end, "status": HISTORICAL_STATUS, "origin": "historical",
        "confidence": confidence(geo_prec, time_prec), "version": 1,
        "mention_count": max(1, len(sources)), "source_diversity": max(1, len(sources)),
        "gdelt_ids": [], "first_seen": t_end, "updated_at": t_end,
        "cluster_state": {"source": SOURCE, "acled_id": uid,
                          "acled_timestamp": _int(row.get("timestamp")),
                          "imported_at": _iso(now)},
    }
    document = {
        "event_id": event_id, "doc_id": doc_id, "source": SOURCE,
        "url": f"https://acleddata.com/data/#{uid}", "title": title, "published_at": t_end,
        "language": "en", "role": "primary", "added_at": _iso(now)}
    values: dict[str, object] = {
        "fatalities_best": fatalities, "event_type": event_type, "sub_event_type": sub_type,
        "disorder_type": _s(row, "disorder_type"), "actor1": actor1, "actor2": actor2,
        "assoc_actor_1": _s(row, "assoc_actor_1"), "assoc_actor_2": _s(row, "assoc_actor_2"),
        "interaction": _s(row, "interaction"),
        "civilian_targeting": _s(row, "civilian_targeting"),
        "admin1": admin1, "admin2": _s(row, "admin2"), "admin3": _s(row, "admin3"),
        "sources": "; ".join(sources), "source_scale": _s(row, "source_scale"),
        "geo_precision": geo_prec, "time_precision": time_prec, "acled_id": uid,
        "source": SOURCE, "licence": LICENCE, "category": cat, "time": t,
        "location": where,
    }
    fields = [{"event_id": event_id, "field": k, "value": str(v), "doc_id": doc_id,
               "quote": notes or title} for k, v in values.items() if v not in (None, "")]
    return {"event": event, "document": document, "fields": fields,
            "timestamp": _int(row.get("timestamp"), 0) or 0}


def _denied(r: httpx.Response) -> str | None:
    """The API's "Access denied" message, if this response is one."""
    if r.status_code not in (200, 401, 403):
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    msg = body.get("message") if isinstance(body, dict) else None
    if isinstance(msg, str) and msg.lower().startswith("access denied"):
        return msg
    return None


@register
class AcledCollector(PollCollector):
    name = "acled"
    domain = "events"
    licence = "ACLED terms of use (myACLED account; attribution required)"
    commercial_ok = False
    default_interval_s = 6 * 3600.0
    poll_timeout_s = 300.0

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._access: str | None = None
        self._access_exp = 0.0  # time.monotonic() deadline
        self._refresh: str | None = None
        self._refresh_exp = 0.0
        self._warned: set[str] = set()

    def _once(self, key: str, level: int, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.log(level, msg)

    async def _token_request(self, ctx: CollectorContext, data: dict[str, str]) -> None:
        r = await ctx.http.post(self.cfg.get("token_url", TOKEN_URL), data=data, timeout=60)
        if r.status_code in (400, 401, 403):
            raise AcledAuthError(f"token endpoint HTTP {r.status_code}")
        r.raise_for_status()
        body = r.json()
        now = time.monotonic()
        self._access = str(body["access_token"])
        self._access_exp = now + float(body.get("expires_in") or 86400)
        if body.get("refresh_token"):
            self._refresh = str(body["refresh_token"])
            self._refresh_exp = now + 14 * 86400 - 3600

    async def _token(self, ctx: CollectorContext) -> str:
        now = time.monotonic()
        if self._access and now < self._access_exp - TOKEN_MARGIN_S:
            return self._access
        self._access = None
        if self._refresh and now < self._refresh_exp:
            try:
                await self._token_request(ctx, {
                    "grant_type": "refresh_token", "refresh_token": self._refresh,
                    "client_id": "acled"})
            except (AcledAuthError, httpx.HTTPStatusError, KeyError, ValueError):
                self._refresh = None  # fall back to a password login
        if not self._access:
            s = ctx.settings
            await self._token_request(ctx, {
                "username": s.acled_email, "password": s.acled_password,
                "grant_type": "password", "client_id": "acled", "scope": "authenticated"})
        assert self._access is not None
        return self._access

    async def _read(self, ctx: CollectorContext, params: dict[str, Any]) -> list[dict]:
        for attempt in range(2):
            token = await self._token(ctx)
            r = await ctx.http.get(self.cfg.get("read_url", READ_URL), params=params,
                                   headers={"Authorization": f"Bearer {token}"}, timeout=120)
            msg = _denied(r)
            # an expired/revoked token: forget it and log in again once
            if r.status_code == 401 or (msg and "authentication" in msg.lower()):
                self._access = self._refresh = None
                if attempt == 0:
                    continue
            if msg or r.status_code in (401, 403):
                raise AcledAccessDenied(msg or f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise RuntimeError(f"acled: HTTP {r.status_code}")
            body = r.json()
            data = body.get("data") if isinstance(body, dict) else body
            if not isinstance(data, list):
                raise TypeError("acled: unexpected response (no data list)")
            return data
        raise AcledAccessDenied("authentication required")

    def _save(self, ctx: CollectorContext, mapped: list[dict]) -> None:
        for i in range(0, len(mapped), BATCH):
            chunk = mapped[i:i + BATCH]
            ctx.store.save_events([m["event"] for m in chunk], [m["document"] for m in chunk],
                                  [f for m in chunk for f in m["fields"]])

    async def poll(self, ctx: CollectorContext) -> Batch:
        batch = Batch(source=self.name)
        s = ctx.settings
        if not (getattr(s, "acled_email", "") and getattr(s, "acled_password", "")):
            self._once("creds", logging.INFO,
                       "acled: ACLED_EMAIL / ACLED_PASSWORD not set in .env; collector idle")
            return batch
        cursor = ctx.get_cursor(self.name) or {}
        now = utcnow()
        blocked = cursor.get("blocked_until")
        if blocked and datetime.fromisoformat(blocked) > now:
            return batch
        backoff = timedelta(hours=float(self.cfg.get("denied_backoff_h", 12)))
        last_ts = int(cursor.get("last_timestamp") or 0)
        page_size = int(self.cfg.get("page_size", PAGE_SIZE))
        params: dict[str, Any] = {"_format": "json", "limit": page_size}
        if last_ts:
            # "> last - 1" == ">= last": rows uploaded in the same second are not missed
            params.update({"timestamp": last_ts - 1, "timestamp_where": ">"})
        else:
            since = (now - timedelta(days=float(self.cfg.get("days", 14)))).date()
            params.update({"event_date": f"{since.isoformat()}|{now.date().isoformat()}",
                           "event_date_where": "BETWEEN"})
        rows: list[dict] = []
        try:
            for page in range(1, int(self.cfg.get("max_pages", MAX_PAGES)) + 1):
                data = await self._read(ctx, {**params, "page": page})
                rows.extend(data)
                if len(data) < page_size:
                    break
        except (AcledAuthError, AcledAccessDenied) as e:
            denied = isinstance(e, AcledAccessDenied)
            state = "access_denied" if denied else "login_failed"
            if denied:
                msg = ("acled: ACLED account lacks API access (\"Access denied\"); check the "
                       "access level at https://acleddata.com/myacled-faqs - retrying in "
                       f"{backoff.total_seconds() / 3600:g} h")
            else:
                msg = ("acled: login rejected; check ACLED_EMAIL / ACLED_PASSWORD in .env - "
                       f"retrying in {backoff.total_seconds() / 3600:g} h")
            self._once(state, logging.WARNING, msg)
            ctx.set_cursor(self.name, {**cursor, "state": state, "last_poll": now.isoformat(),
                                       "blocked_until": (now + backoff).isoformat()})
            return batch
        except httpx.HTTPError as e:
            raise RuntimeError(f"acled: {type(e).__name__}") from None  # no URL / token
        self._warned.discard("access_denied")
        self._warned.discard("login_failed")
        mapped, bad = [], 0
        for row in rows:
            try:
                m = map_row(row, now)
            except (ValueError, TypeError, KeyError):
                m = None
            if m is None:
                bad += 1
            else:
                mapped.append(m)
        if bad:
            log.warning("acled: skipped %d malformed row(s)", bad)
        if mapped:
            await asyncio.to_thread(self._save, ctx, mapped)
        newest = max([last_ts, *(m["timestamp"] for m in mapped)])
        ctx.set_cursor(self.name, {"state": "ok", "last_timestamp": newest,
                                   "last_poll": now.isoformat(), "events": len(mapped),
                                   "blocked_until": None})
        log.info("acled: %d event(s) upserted", len(mapped))
        return batch
