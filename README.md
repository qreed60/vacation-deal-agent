# Vacation Deal Agent

Phase 1 backend, Phase 2 mock search foundation, Phase 3 real-source adapters, and Phase 4 source-grounded deal scoring for tracking vacation manifests.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8095
```

The application uses SQLite at `data/vacation_deals.sqlite3`.

## Test

```bash
python -m pytest
```

## Run One Mock Search

By default, search runs use deterministic mock source responses only.

Run a mock search for one vacation:

```bash
python scripts/run_search_once.py --vacation-id 1
```

Run once for every active vacation:

```bash
python scripts/run_search_once.py --all-active
```

Run configured Phase 3 sources for one vacation:

```bash
python scripts/run_search_once.py --vacation-id 1 --use-real-sources
```

Include both real sources and deterministic mock rows:

```bash
python scripts/run_search_once.py --vacation-id 1 --use-real-sources --use-mock
```

Real API keys are optional. Disabled sources, missing credentials, and unresolved
airport/city codes are stored as `source_result` rows with `status="skipped"`.
Provider failures are stored as `source_result` rows with `status="error"` and
the error message. The runner does not invent prices, ratings, hotel details, or
flight details.

After each search run, Phase 4 creates source-grounded `price_snapshot` rows for
priced quotes and `deal_candidate` rows for scored flight-only, hotel-only,
rental-car-only, or package candidates. CLI output includes price snapshot count,
deal candidate count, and the best deal total price when one is available.

## Source Configuration

Configuration is loaded from environment variables or `.env`. `.env` is ignored
by git and should not be committed.

`.env.example` fields:

- `SEARXNG_BASE_URL` defaults to `http://127.0.0.1:8888`
- `SEARXNG_TIMEOUT_SECONDS`
- `AMADEUS_ENABLED`
- `AMADEUS_BASE_URL`
- `AMADEUS_CLIENT_ID`
- `AMADEUS_CLIENT_SECRET`
- `AMADEUS_TIMEOUT_SECONDS`
- `GOOGLE_PLACES_ENABLED`
- `GOOGLE_PLACES_API_KEY`
- `GOOGLE_PLACES_TIMEOUT_SECONDS`
- `SERPAPI_ENABLED`
- `SERPAPI_API_KEY`
- `SERPAPI_BASE_URL`
- `SERPAPI_TIMEOUT_SECONDS`
- `FAST_FLIGHTS_ENABLED` optional free/unofficial flight source, default `false`
- `FAST_FLIGHTS_FETCH_MODE` defaults to `common`; unsafe fallback/browser modes are forced back to `common`
- `FAST_FLIGHTS_SEAT` defaults to `economy`
- `FAST_FLIGHTS_MAX_STOPS` optional integer
- `TRVL_ENABLED` optional free/no-key structured flight and hotel source, default `false`
- `TRVL_BINARY_PATH` defaults to `.tools/trvl/trvl`; if missing, the runner also checks `.tools/trvl/trvl` and then `trvl` on `PATH`
- `TRVL_TIMEOUT_SECONDS` defaults to `120`
- `TRVL_MAX_FLIGHT_RESULTS` defaults to `20`
- `TRVL_MAX_HOTEL_RESULTS` defaults to `20`
- `TRVL_CURRENCY` defaults to `USD`
- `FREE_TRAVEL_PROBE_FLIGHTS_SKILL_COMMAND` optional local JSON command for the isolated free-source probe
- `FREE_TRAVEL_PROBE_TRAVEL_HACKING_TOOLKIT_COMMAND` optional local JSON command for the isolated free-source probe

## Optional trvl Source

`trvl` can be enabled as the preferred free/no-key structured source for
flights and hotels:

```bash
TRVL_ENABLED=true FAST_FLIGHTS_ENABLED=false python scripts/run_search_once.py --vacation-id 1 --use-real-sources
```

The source is disabled by default. The runner resolves the binary in this order:
`TRVL_BINARY_PATH`, `.tools/trvl/trvl`, then `trvl` on `PATH`. If
`TRVL_ENABLED=true` but no binary is found, the search run records a skipped
SourceResult with a clear missing-dependency error and continues safely.

trvl may write partial provider warnings to stderr while still returning usable
JSON on stdout. When stdout contains `success=true`, those stderr lines are
stored as bounded warnings and do not fail the source result.

Flight searches use resolved IATA origin/destination fields and only create
price snapshots from rows with numeric price, currency, and source identity
from airline, provider, or cheapest source fields. trvl flight passenger
pricing uses the CLI's `--adults` argument for the total traveler count; when
children are present, the normalized metadata notes that all travelers were
priced as adults because the CLI does not expose child passenger pricing.

Hotel searches treat trvl hotel price as nightly, matching the CLI's per-night
hotel price flags. Normalized hotel offers preserve `nightly_price`, `nights`,
`total_price`, and `price_basis=nightly`. Hotel snapshots are created only from
rows with a hotel name, numeric price, and currency.

trvl prices are source-grounded but still need user verification before booking.
The app does not perform booking, payment, reservation, or browser-scraping
actions.

## Optional fast-flights Source

`fast-flights` can be enabled as an optional real airfare source:

```bash
python -m pip install fast-flights
FAST_FLIGHTS_ENABLED=true python scripts/run_search_once.py --vacation-id 1 --use-real-sources
```

The source is disabled by default. It does not require SerpAPI or another paid
API key. The adapter uses only `fetch_mode=common`; fallback, force-fallback,
and local modes are not used because they may trigger browser automation.

### Airport code resolution

fast-flights requires IATA airport codes (e.g., `PIT`, `MOT`). The app resolves
origin/destination using this priority:

1. **preferred_airports** from the vacation manifest — first entry is used.
2. **alternate_airports** from the vacation manifest — first entry when preferred is empty.
3. Raw origin/destination value if it already looks like a 3-letter IATA code.
4. A small fallback city map for known common values (e.g., `Pittsburgh, PA` → `PIT`, `Minot, ND` → `MOT`).

If neither airport can be resolved, the fast-flights source returns a skipped
result with a clear error message rather than guessing or calling external APIs.

### Result bounding

fast-flights result normalization is bounded by `FAST_FLIGHTS_MAX_RESULTS` (default: 20).
Deduplication is applied first (by provider + price + departure + arrival + label),
then the top N priced quotes are kept sorted by total_price ascending. All raw and
diagnostic source data is preserved in the SourceResult row even when offers are limited.

### Caveats

- fast-flights is free/unofficial and may be route-fragile.
- Search reference links (`link_type=search_reference`) are not booking links or exact price guarantees.
- Hotels and rental cars still require separate structured sources.

## Free-Source Probe

Phase 3/4 includes an isolated investigation CLI for checking whether
free/open-source travel-source candidates can return structured provider, price,
currency, and link data that Phase 4 could eventually consume. These probes are
not production search sources and are not wired into `scripts/run_search_once.py`,
the search runner, scheduled jobs, or the UI.

Candidates:

- `fast-flights`
- `fli`
- `trvl` (uses the same local binary detection as the optional production source)
- `flights-skill`
- `travel-hacking-toolkit`

Run a single flight candidate:

```bash
python scripts/probe_free_travel_sources.py --candidate fast-flights --origin PIT --destination MOT --depart 2026-09-18 --return 2026-09-21 --adults 2 --children 3
```

Run the `fli` candidate:

```bash
python scripts/probe_free_travel_sources.py --candidate fli --origin PIT --destination MOT --depart 2026-09-18 --return 2026-09-21 --adults 2 --children 3
```

Run a hotel-oriented `trvl` probe:

```bash
python scripts/probe_free_travel_sources.py --candidate trvl --destination "Minot, ND" --check-in 2026-09-18 --check-out 2026-09-21 --adults 2 --children 3
```

Run a flight-oriented `trvl` probe:

```bash
python scripts/probe_free_travel_sources.py --candidate trvl --origin PIT --destination MOT --depart 2026-09-18 --return 2026-09-21 --adults 2 --children 3
```

Run every known candidate:

```bash
python scripts/probe_free_travel_sources.py --all --origin PIT --destination MOT --depart 2026-09-18 --return 2026-09-21 --adults 2 --children 3
```

Each run prints a readable summary and writes a JSON report under
`data/free_source_probes/`. The probe does not install dependencies, does not
require paid API keys, does not start persistent MCP servers, and does not use
browser scraping or browser automation.

Status meanings:

- `usable`: the candidate returned structured provider and price data.
- `missing_dependency`: a required local package, binary, or configured local command is missing.
- `failed`: the candidate was available but the probe call failed.
- `unsupported`: the candidate name or mode is unknown to this probe.
- `unsupported_for_free_source_goal`: local help or behavior indicates an API key or paid service requirement.
- `unsupported_for_current_phase`: local help or behavior indicates browser automation or another excluded mechanism.
- `not_usable_for_pricing`: output was present but did not include a reliable structured provider and price pair.
- `available`: the dependency exists, but no documented local JSON search path was recognized.

Reverse-engineered and free travel sources may be fragile. Keep any candidate
isolated behind adapters, preserve the no-fabricated-price rule, and do not use
a candidate for Phase 5 automation until it is marked `usable` with structured
provider and price data for the needed trip shape.

## Phase 1 Scope

Implemented:

- FastAPI web app
- SQLite database
- Vacation create, edit, list, detail, delete, and JSON export routes
- JSON manifest import with required-field validation
- Basic `status` field
- Backend tests
- Simple Jinja2 templates for backend integration placeholders

Out of scope for Phase 1:

- Travel search
- SearXNG, Amadeus, Google Places, Duffel, or MCP tool integrations
- Deal scoring
- Price history
- Periodic automation
- Systemd services
- LLM summaries
- Polished UI styling

Dyad owns later UI polish, templates, and styling. The current templates are intentionally simple and replaceable.

## Phase 2 Scope

Implemented:

- Search runner foundation
- `scripts/run_search_once.py`
- Active vacation manifest loader
- Deterministic query planner
- `search_run` table
- `source_result` table
- Manual "Run search now" button
- Mocked adapter responses for flight, hotel, and rental car queries

Out of scope for Phase 2:

- Real SearXNG, Amadeus, Google Places, or Duffel calls
- MCP tools
- Deal scoring
- Price history charts
- Periodic automation
- Systemd services or timers
- LLM summaries
- Email or text notifications

## Phase 3 Scope

Implemented:

- SearXNG JSON search adapter
- Amadeus OAuth2 client credentials configuration and token caching
- Amadeus flight offer search adapter
- Amadeus hotel list and hotel offer lookup adapters
- Basic Google Places Text Search and Place Details normalization
- Optional SerpAPI Google Flights and Google Hotels adapter for structured broad flight/hotel price results
- Optional fast-flights adapter for free/unofficial structured airfare price results
- Real-source integration in the existing Phase 2 search runner
- CLI flags for `--use-real-sources` and `--use-mock`
- Source statuses: `completed`, `skipped`, `error`, and `mock`

Out of scope for Phase 3:

- Deal scoring
- Price history graphs
- 14/30/90 day low notifications
- Periodic automation
- Systemd services or timers
- MCP tools
- LLM summaries
- Booking, purchase, or payment flows
- Browser scraping

## Phase 4 Scope

Implemented:

- Normalized flight, hotel, rental-car, and package quote snapshots
- `price_snapshot` table with source name, source result ID, source URL when available, timestamps, and normalized references
- `deal_candidate` table with components, source links, deterministic score breakdowns, and normalized candidate JSON
- Package candidate builder for single-service and multi-service vacations
- Deterministic scoring where lower total price ranks better
- Basic penalties for low hotel ratings, high distance values, missing required components, and skipped/error source rows when those fields exist
- Best deal summary on vacation detail pages
- Deal list and deal detail pages
- Vacation price history page with a simple SVG graph
- Search-run summaries with priced snapshot count, deal candidate count, and best deal metadata
- Provider/source/link metadata normalized into quote snapshots, deal candidates, deal pages, and search-run detail pages
- Exact source links are shown only when the upstream source provides a specific source/deep URL
- Generated fallback links are labeled `Search reference`, not exact booking or exact-price links
- Amadeus airfare coverage is not a complete broad-airline source; broader airline coverage may require future source adapters
- Generic SearXNG links are not considered sufficient priced travel quotes
- SerpAPI Google Flights and Google Hotels can be enabled as optional real priced sources
- Rental car broad pricing still requires a structured source adapter; generic links are not used to fabricate rental car prices

Source-grounding rules:

- Deal and quote rows are created only from `source_result.normalized_result_json` or clearly marked mock data.
- Unpriced source results are not scored and no prices are fabricated.
- SearXNG web/context results can provide reference/source links, but arbitrary snippets are not treated as authoritative price records.
- Search reference links are not exact price guarantees.
- Mock hotel nightly prices and rental-car daily prices are converted to totals only from the vacation's own date span or target nights.
- Skipped and error source results remain stored as `source_result` rows.

Out of scope for Phase 4:

- Periodic automation
- Systemd services or timers
- Email or text notifications
- 14/30/90-day low alerts
- MCP tools
- LLM summaries
- Booking, purchase, or payment flows
- Browser scraping
- New real source integrations beyond the existing Phase 3 adapters
- Notifications and periodic automation until Phase 5
