# Vacation Deal Agent

Phase 1 backend and integration for tracking vacation manifests.

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
