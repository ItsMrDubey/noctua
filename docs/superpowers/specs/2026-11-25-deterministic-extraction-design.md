# Deterministic Extraction & Country Normalisation — Design (Sub-project A)

**Date:** 2026-11-25
**Status:** Approved in brainstorming, awaiting spec review
**Goal:** Keep event fields current without the LLM. Every event gets a normalised country, actor countries, a headline and summary, casualties, weapons/platforms and a lifecycle state, all from deterministic rules. The LLM extraction job (`pipeline/extract/job.py`) remains as an optional upgrade, held back by its existing hourly and daily caps.
**Runs in parallel with:** C (reskin, frontend only). **Blocks:** B (country context).

---

## 1. Problem

- `events.country` holds three code systems at once:
  - GDELT events store FIPS 10-4 codes from `ActionGeo_CountryCode` (`pipeline/cluster.py:1115`), e.g. `UK`, `GM`, `RS`.
  - News and gazetteer events store ISO2.
  - ACLED events (`collectors/acled.py:148`) and UCDP events (`backfill/ucdp.py:159`) store full country names.
- `intel/context.py:470` `resolve_cc` reads any two-letter value as ISO2, so FIPS codes are misread. For example, `GM` (Germany) is read as Gambia.
- `actors[].country` is a CAMEO three-letter code for GDELT and empty for ACLED/UCDP. LLM actors have no country.
- Summary, actors, casualties and weapons only exist once the LLM has extracted the event. Hitting the caps leaves most events without them.
- There is no notion of whether an event is still developing.

## 2. Components

### 2.1 `osint/core/countries.py` — country registry
- Static tables loaded once:
  - ISO2 ↔ ISO3 ↔ FIPS 10-4 ↔ CAMEO country code ↔ English short name;
  - aliases (e.g. "Russian Federation", "DRC", "Côte d'Ivoire" without the accent);
  - demonyms ("Russian", "Ukrainian", "Israeli" …).
- Data file: `config/countries.yaml`, generated once from public ISO/FIPS/CAMEO tables and committed. No network access at runtime.
- API:
  - `to_iso2(value, hint: Literal["fips","cameo","iso2","iso3","name","auto"]) -> str | None`
  - `name(iso2) -> str`
  - `demonym_index() -> dict[str, str]`
- `auto` never treats a two-letter value as ISO2 when the caller knows it is FIPS. Callers always pass the correct hint.
- Non-state CAMEO codes (e.g. `IGO`, `NGO`, `REB` without a country prefix) return `None`.

### 2.2 Write-path normalisation
- `cluster.py`: GDELT `ActionGeo_CountryCode` uses `to_iso2(hint="fips")`. Actor1/Actor2 country codes use `to_iso2(hint="cameo")`, and the original value is kept as `actors[].cameo`.
- `acled.py` and `backfill/ucdp.py`: the country name uses `to_iso2(hint="name")`.
- News, gazetteer and `PlaceHit.country` are already ISO2. They are checked against the registry, and unknown values become `None`.
- `verdict.py`: LLM `where.country` is already ISO2 and is checked against the registry.
- `resolve_cc` trusts `events.country` once the migration (§2.7) has run. Until then it keeps its lat/lon fallback.

### 2.3 Actor-country resolution (`osint/enrich/actors.py`)
- Lexicon `config/actors.yaml`. Each entry maps a pattern to an ISO2 code and an actor class:

  ```yaml
  - match: ["Russian forces", "Russian army", "VKS", "Russian Armed Forces"]
    country: RU
    class: state_military
  - match: ["IDF", "Israel Defense Forces", "Israeli army"]
    country: IL
    class: state_military
  - match: ["Military Forces of Ukraine (2019-)"]   # ACLED actor name
    country: UA
    class: state_military
  ```

- Generated entries cover `<demonym> forces|army|navy|air force|military|police|government|ministry`.
- Hand-written entries cover common ACLED/UCDP side names and non-state groups with a home country, seeded from the terror designations in `osint/terror`.
- Matching folds case and diacritics with `enrich.gazetteer.fold` and uses whole-word matches. The longest match wins.
- Resolution order for each event, merged with duplicates removed by `(name, country)`:
  1. GDELT actor CAMEO country codes.
  2. ACLED/UCDP actor names through the lexicon.
  3. Title and snippet of every linked document through the lexicon. Such actors get role `mentioned`.
  4. LLM actors' `name` through the lexicon, when an extraction exists.
- Output: `actors[]` entries always carry `country` (ISO2 or `null`) and `source` (`gdelt|acled|ucdp|rule|llm`).
- Derived per event and stored as JSON column `actor_countries` (sorted ISO2 list, nulls dropped):
  - `actor_countries`;
  - `foreign_involved: bool`, true when any actor country differs from `events.country`;
  - `attributed: bool`, true when at least one actor country is known.

### 2.4 Rule extractors (`osint/enrich/rules/`)
Each extractor is a pure function `(event, docs) -> list[FieldValue]`. They write to `event_fields` with `source='rule'` and a `quote`, the matched span.

| Field | Method |
|---|---|
| `headline` | Title of the document whose embedding is nearest the cluster centroid. Without an embedder, use the primary-role document, then the earliest. Re-picked on every change. |
| `summary` | Snippet or lead of that same document, cut at a sentence boundary to ≤ 280 characters. |
| `casualties.killed` / `.injured` | Multilingual regex patterns in `config/extract_rules.yaml`. Covers EN, FR, ES, AR, RU and UK; for example "killed 12", "12 dead", "at least 12 people were killed", "قتل 12", "погибли 12", "загинули 12", and number words up to twenty. Take the **median** over distinct domains, ignoring documents that state no number. |
| `weapons_platforms` | Lexicon in `config/extract_rules.yaml` (drone/UAV/Shahed, ballistic missile, cruise missile, artillery, MLRS, IED, VBIED, airstrike, naval vessel classes …), normalised to canonical labels. |

**Precedence:** when the API returns a field, an `llm` value whose verdict is `accepted` outranks a `rule` value. Otherwise the `rule` value is used. Both are stored, and the API response marks which one was used (`field_source`).

### 2.5 Lifecycle
- New column `events.lifecycle`, calculated in each EventBuilder pass from `last_doc_at`, the time the newest linked document was published:
  - `developing`: newest document ≤ 6h old **and** ≥ 2 documents in the last 6h;
  - `active`: newest document ≤ 48h old;
  - `cooling`: newest document ≤ 7d old;
  - `closed`: older.
- Sensor-derived events use the time the signal was last seen.
- The thresholds live in `config/events.yaml` under `lifecycle:`.

### 2.6 Re-extraction trigger
- The EventBuilder records the event ids touched in each pass: new event, document added, or merged.
- After the pass commits, rule extraction and actor resolution run for those ids in the same worker. Both are CPU-only and target < 5 ms per event.
- The LLM queue's `priority_key` is unchanged, so the LLM only takes what its caps allow.

### 2.7 Migration
- `python -m osint.pipeline.migrate_countries [--dry-run]`:
  - rewrites `events.country` and `actors[].country` using the source of each event (GDELT → FIPS/CAMEO hints, ACLED/UCDP → names);
  - fills in `actor_countries`, `foreign_involved`, `attributed` and `lifecycle`;
  - runs the rule extractors for every event from the last 30 days.
- Takes `data/.event_rebuild.lock`, the same lock as `retriage --rebuild-events`, so the live server pauses its builder.
- Adds the new columns `actor_countries` (JSON), `foreign_involved`, `attributed` and `lifecycle`, plus an index on `events(country)`. Schema changes go in `store/sqlite.py` as idempotent `ALTER TABLE`s.
- Prints a before/after report: country-code distribution, the number of FIPS codes rewritten, unresolved values, and the attributed share.

## 3. Error handling
- Values that don't resolve become `null` and are logged once per distinct value per pass at debug level. They're also counted in the source-health snapshot (`unresolved_country` counter), so gaps show up in the status popover.
- A rule extractor that raises an exception is skipped for that event and the error is logged. One bad document can't stall the builder.
- If the lexicon fails to load at startup, the error is fatal. A silent empty lexicon would wrongly make every event "domestic".

## 4. Testing
- `tests/core/test_countries.py`:
  - FIPS/ISO collisions (`GM`, `UK`, `RS`, `AU`/`AS`, `CH`, `BG`);
  - CAMEO codes (`USA`, `RUS`, `ISR`, non-state codes → `None`);
  - names and aliases.
- `tests/enrich/test_actors.py`:
  - lexicon matches, longest match wins, whole words only (so "Iranian" is not matched by a shorter "Iran" entry);
  - ACLED and UCDP side names.
- `tests/enrich/rules/`: fixture headlines per language for casualties (including the median across conflicting reports), weapons, and headline and summary picks.
- `tests/pipeline/test_lifecycle.py`: boundaries at 6h, 48h and 7d, and the `developing` volume rule.
- Migration test on a small SQLite fixture holding all four country formats.
- Regression: run the migration with `--dry-run` on the real `data/` and check that the attributed share and the country distribution are sensible. The report is attached to the PR.

## 5. Out of scope
- New collectors or faster polling (ETags, Telegram push). These are tracked separately.
- Changes to the LLM extraction prompt or schema, beyond mapping actor names to countries.
- Any UI. B uses these fields.
