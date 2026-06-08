# Vacation Deal Agent

Phase 1 backend, Phase 2 mock search foundation, and Phase 3 real-source adapters for tracking vacation manifests.

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
