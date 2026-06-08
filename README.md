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
