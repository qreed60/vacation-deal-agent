"""Bounded AI Search Planner for vacation deal searches (Phase 5A).

Provides a deterministic baseline plan generator and an optional bounded
AI planner that can propose additional search variants. All plans are
validated before execution. The AI never invents prices or overrides
source truth.

Planner output schema (strict JSON):
{
    "planner_version": "phase5a_v1",
    "objective": "...",
    "searches": [...],
    "fallback_searches": [...],
    "research_queries": [...],
    "reasoning_summary": "...",
    "constraints": [],
    "warnings": []
}

Each search entry:
{
    "search_type": "flight" | "hotel" | "rental_car",
    "origin_airport": "...",
    "destination_airport": "...",
    "departure_date": "YYYY-MM-DD",
    "return_date": "YYYY-MM-DD",
    "traveler_strategy": "exact" | "flexible",
    "priority": int,
    "reason": "..."
}

Each research query entry:
{
    "query": "...",
    "purpose": "..."
}
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.services.manifest_io import manifest_for_vacation
from app.services.source_config import SourceConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SEARCH_TYPES = {"flight", "hotel", "rental_car"}
VALID_TRAVELER_STRATEGIES = {"exact", "flexible"}
DEFAULT_MAX_STRUCTURED_SEARCHES = 8
DEFAULT_MAX_RESEARCH_QUERIES = 5
DATE_FLEX_DAYS_DEFAULT = 1


# ---------------------------------------------------------------------------
# Deterministic baseline plan generator
# ---------------------------------------------------------------------------

def _iso(d: date | None) -> str | None:
    """Return ISO-format string or None."""
    return d.isoformat() if isinstance(d, date) else None


def _parse_date(value: Any) -> date | None:
    """Parse a date from various input types. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return None


def build_deterministic_baseline_plan(vacation: Any) -> dict[str, Any]:
    """Build a deterministic baseline search plan from vacation data.

    This is the always-available fallback when AI planner is disabled or fails.
    It produces exactly one primary flight search (exact dates) plus optional
    hotel/rental_car entries.
    """
    manifest = manifest_for_vacation(vacation)
    start_date = _parse_date(manifest.get("start_date"))
    end_date = _parse_date(manifest.get("end_date"))

    origin_raw = str(manifest.get("origin", "") or "").strip()
    destination_raw = str(manifest.get("destination", "") or "").strip()

    # Resolve airports from manifest preferred/alternate if available
    origin_airport = None
    destination_airport = None
    preferred = manifest.get("preferred_airports") or []
    alternate = manifest.get("alternate_airports") or []
    if isinstance(preferred, list) and len(preferred) > 0:
        first_pref = str(preferred[0]).strip().upper()
        if re.fullmatch(r"[A-Z]{3}", first_pref):
            origin_airport = first_pref
    elif isinstance(alternate, list) and len(alternate) > 0:
        first_alt = str(alternate[0]).strip().upper()
        if re.fullmatch(r"[A-Z]{3}", first_alt):
            origin_airport = first_alt

    if not origin_raw or re.fullmatch(r"[A-Z]{3}", origin_raw):
        origin_airport = origin_airport or (origin_raw.upper() if origin_raw else None)

    if isinstance(alternate, list) and len(alternate) > 0:
        first_alt_dest = str(alternate[0]).strip().upper()
        if re.fullmatch(r"[A-Z]{3}", first_alt_dest):
            destination_airport = first_alt_dest
    elif not destination_raw or re.fullmatch(r"[A-Z]{3}", destination_raw):
        destination_airport = destination_airport or (destination_raw.upper() if destination_raw else None)

    date_mode = manifest.get("date_mode", "fixed")
    travelers = manifest.get("travelers") or []
    traveler_count = int(manifest.get("number_of_travelers") or len(travelers) or 1)

    searches: list[dict[str, Any]] = []
    fallback_searches: list[dict[str, Any]] = []
    research_queries: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Primary flight search (exact dates, always first)
    if manifest.get("airfare_needed") and origin_airport and destination_airport and start_date and end_date:
        searches.append({
            "search_type": "flight",
            "origin_airport": origin_airport,
            "destination_airport": destination_airport,
            "departure_date": _iso(start_date),
            "return_date": _iso(end_date),
            "traveler_strategy": "exact",
            "priority": 1,
            "reason": f"Exact requested trip dates: {origin_airport} to {destination_airport}.",
        })

    # Hotel search if needed
    if manifest.get("hotel_needed") and end_date and start_date:
        searches.append({
            "search_type": "hotel",
            "destination_airport": destination_airport or "",
            "departure_date": _iso(start_date),
            "return_date": _iso(end_date),
            "traveler_strategy": "exact",
            "priority": 2 if manifest.get("airfare_needed") else 1,
            "reason": f"Hotel needed for {destination_airport or destination_raw} stay.",
        })

    # Rental car search if needed
    if manifest.get("rental_car_needed"):
        fallback_searches.append({
            "search_type": "rental_car",
            "destination_airport": destination_airport or "",
            "departure_date": _iso(start_date) if start_date else None,
            "return_date": _iso(end_date) if end_date else None,
            "traveler_strategy": "exact",
            "priority": 3 if manifest.get("airfare_needed") and manifest.get("hotel_needed") else 2,
            "reason": f"Rental car needed at {destination_airport or destination_raw}.",
        })

    # Research query for web fallback
    if origin_airport and destination_airport:
        research_queries.append({
            "query": f"{origin_airport} to {destination_airport} flights {start_date or ''} {end_date or ''}",
            "purpose": "Fallback web research if structured providers fail.",
        })

    # Date mode warnings
    if date_mode == "flexible" and not start_date:
        warnings.append("Date mode is flexible but no start/end dates provided; plan uses available data.")

    objective_parts = []
    if origin_airport and destination_airport:
        objective_parts.append(f"{origin_airport} to {destination_airport}")
    if start_date and end_date:
        objective_parts.append(f"{start_date.isoformat()} to {end_date.isoformat()}")
    objective = "Find deals for " + ", ".join(objective_parts) if objective_parts else "Search vacation deal."

    return {
        "planner_version": "phase5a_v1",
        "objective": objective,
        "searches": searches,
        "fallback_searches": fallback_searches,
        "research_queries": research_queries,
        "reasoning_summary": f"Deterministic baseline plan for {vacation.title if hasattr(vacation, 'title') else 'vacation'}. Exact dates used: {_iso(start_date)} / {_iso(end_date)}.",
        "constraints": [f"date_mode={date_mode}", f"travelers={traveler_count}"],
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Plan validator
# ---------------------------------------------------------------------------

def _validate_search_entry(entry: Any) -> tuple[bool, list[str]]:
    """Validate a single search entry. Returns (is_valid, errors)."""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return False, ["search entry is not a dict"]

    # Required fields
    for field in ("search_type", "origin_airport", "destination_airport", "departure_date"):
        if field not in entry:
            errors.append(f"missing required field: {field}")

    if errors:
        return False, errors

    # Validate search_type
    if entry["search_type"] not in VALID_SEARCH_TYPES:
        errors.append(f"invalid search_type: {entry['search_type']!r}")

    # Validate airport codes (3 uppercase letters or empty)
    for field in ("origin_airport", "destination_airport"):
        val = str(entry.get(field, "")).strip()
        if val and not re.fullmatch(r"[A-Z]{3}", val):
            errors.append(f"{field} must be 3-letter IATA code or empty; got {val!r}")

    # Validate dates (YYYY-MM-DD)
    for field in ("departure_date", "return_date"):
        val = entry.get(field)
        if val is not None:
            try:
                datetime.strptime(str(val), "%Y-%m-%d")
            except (ValueError, TypeError):
                errors.append(f"invalid date format for {field}: {val!r}")

    # Validate priority
    priority = entry.get("priority")
    if not isinstance(priority, int) or priority < 1:
        errors.append(f"priority must be positive integer; got {priority!r}")

    # Validate traveler_strategy
    strategy = entry.get("traveler_strategy", "exact")
    if strategy not in VALID_TRAVELER_STRATEGIES:
        errors.append(f"invalid traveler_strategy: {strategy!r}")

    return len(errors) == 0, errors


def _validate_research_query(entry: Any) -> tuple[bool, list[str]]:
    """Validate a single research query entry."""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return False, ["research query is not a dict"]
    if "query" not in entry or not str(entry["query"]).strip():
        errors.append("missing or empty 'query' field")
    if "purpose" not in entry:
        errors.append("missing 'purpose' field")
    return len(errors) == 0, errors


def validate_search_plan(plan: Any) -> tuple[bool, list[str]]:
    """Validate a complete search plan. Returns (is_valid, errors)."""
    errors: list[str] = []

    if not isinstance(plan, dict):
        return False, ["plan is not a dict"]

    # Required top-level fields
    for field in ("planner_version", "searches"):
        if field not in plan:
            errors.append(f"missing required field: {field}")

    if errors:
        return False, errors

    # Validate searches
    searches = plan.get("searches") or []
    if not isinstance(searches, list):
        errors.append("'searches' must be a list")
        searches = []

    for i, entry in enumerate(searches):
        valid, entry_errors = _validate_search_entry(entry)
        if not valid:
            for err in entry_errors:
                errors.append(f"search[{i}]: {err}")

    # Validate fallback_searches
    fallbacks = plan.get("fallback_searches") or []
    if not isinstance(fallbacks, list):
        errors.append("'fallback_searches' must be a list")
        fallbacks = []

    for i, entry in enumerate(fallbacks):
        valid, entry_errors = _validate_search_entry(entry)
        if not valid:
            for err in entry_errors:
                errors.append(f"fallback[{i}]: {err}")

    # Validate research_queries
    queries = plan.get("research_queries") or []
    if not isinstance(queries, list):
        errors.append("'research_queries' must be a list")
        queries = []

    for i, entry in enumerate(queries):
        valid, entry_errors = _validate_research_query(entry)
        if not valid:
            for err in entry_errors:
                errors.append(f"research[{i}]: {err}")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# AI provider interface (bounded)
# ---------------------------------------------------------------------------

def _build_planner_prompt(vacation: Any, config: SourceConfig | None = None) -> str:
    """Build a bounded prompt for the AI planner."""
    manifest = manifest_for_vacation(vacation)
    start_date = _parse_date(manifest.get("start_date"))
    end_date = _parse_date(manifest.get("end_date"))

    origin_raw = str(manifest.get("origin", "") or "").strip()
    destination_raw = str(manifest.get("destination", "") or "").strip()
    date_mode = manifest.get("date_mode", "fixed")
    travelers = int(manifest.get("number_of_travelers") or 1)

    prompt_lines = [
        "You are a bounded search planner for a vacation deal finder.",
        "",
        f"Vacation: {vacation.title if hasattr(vacation, 'title') else 'Unknown'}",
        f"Origin: {origin_raw}",
        f"Destination: {destination_raw}",
        f"Date mode: {date_mode}",
        f"Start date: {_iso(start_date) or 'not set'}",
        f"End date: {_iso(end_date) or 'not set'}",
        f"Travelers: {travelers}",
        f"Airfare needed: {manifest.get('airfare_needed', False)}",
        f"Hotel needed: {manifest.get('hotel_needed', False)}",
        f"Rental car needed: {manifest.get('rental_car_needed', False)}",
        "",
        "RULES:",
        "- Return ONLY valid JSON. No markdown, no explanation.",
        "- Never invent prices or fares.",
        "- Limit total structured searches to a maximum of 8.",
        "- Limit research queries to a maximum of 5.",
        "- Always include exact requested dates first (priority=1).",
        f"- If date_mode is 'flexible', you may add up to {DATE_FLEX_DAYS_DEFAULT} nearby-date variants per search type.",
        "- Do NOT create unbounded calendar sweeps.",
        "- Do NOT search every possible airport unless alternates are configured.",
        "",
        "OUTPUT SCHEMA:",
        json.dumps({
            "planner_version": "phase5a_v1",
            "objective": "string",
            "searches": [{"search_type": "flight|hotel|rental_car", "origin_airport": "IATA3", "destination_airport": "IATA3", "departure_date": "YYYY-MM-DD", "return_date": "YYYY-MM-DD", "traveler_strategy": "exact|flexible", "priority": 1, "reason": "string"}],
            "fallback_searches": [{"search_type": "flight|hotel|rental_car", "origin_airport": "IATA3", "destination_airport": "IATA3", "departure_date": "YYYY-MM-DD", "return_date": "YYYY-MM-DD", "traveler_strategy": "exact|flexible", "priority": 2, "reason": "string"}],
            "research_queries": [{"query": "string", "purpose": "string"}],
            "reasoning_summary": "string",
            "constraints": [],
            "warnings": []
        }, indent=2),
    ]

    return "\n".join(prompt_lines)


def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout_seconds: float = 45.0,
) -> str | None:
    """Call an OpenAI-compatible API and return raw text response or None."""
    try:
        import httpx
    except ImportError:
        return None

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "You are a bounded search planner. Return ONLY valid JSON matching the provided schema. "
                "Never invent prices. Never exceed the maximum search count limits."
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if choices:
                return str(choices[0].get("message", {}).get("content", ""))
    except Exception:
        pass

    return None


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Extract JSON from AI response text (may contain markdown or surrounding text)."""
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        pass

    # Look for JSON block in markdown code fences
    fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)
    match = fence_pattern.search(text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # Look for first { ... } block
    brace_pattern = re.compile(r"\{.*\}", re.DOTALL)
    match = brace_pattern.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            pass

    return None


# ---------------------------------------------------------------------------
# Public planner interface
# ---------------------------------------------------------------------------

def build_search_plan_with_config(vacation: Any, config: SourceConfig | None = None) -> dict[str, Any]:
    """Build a search plan using AI if enabled/configured, else deterministic baseline.

    Falls back to deterministic baseline when:
    - AI_SEARCH_PLANNER_ENABLED=false (default)
    - Provider is disabled/unavailable
    - Model returns invalid JSON or fails validation
    """
    # Always compute deterministic baseline first (serves as fallback)
    baseline_plan = build_deterministic_baseline_plan(vacation)

    if config is None:
        return baseline_plan

    # Check AI planner configuration
    if not config.ai_search_planner_enabled:
        return baseline_plan

    provider = config.ai_search_planner_provider
    if provider in ("disabled", "none", ""):
        return baseline_plan

    # Only support openai_compatible and lmstudio providers (both use OpenAI-compatible API)
    if provider not in ("openai_compatible", "lmstudio"):
        return baseline_plan

    base_url = config.ai_search_planner_base_url
    api_key = config.ai_search_planner_api_key
    model = config.ai_search_planner_model

    if not base_url or not model:
        return baseline_plan

    # Build prompt and call AI
    prompt = _build_planner_prompt(vacation, config)

    raw_response = _call_openai_compatible(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=prompt,
        timeout_seconds=config.ai_search_planner_timeout_seconds,
    )

    if not raw_response:
        return baseline_plan

    # Extract JSON from response
    extracted = _extract_json_from_text(raw_response)
    if extracted is None:
        return baseline_plan

    # Validate the plan
    is_valid, validation_errors = validate_search_plan(extracted)
    if not is_valid:
        return baseline_plan

    # Apply bounded limits to AI-generated plan
    bounded_plan = _apply_bounded_limits(extracted, config)

    # If AI produced no searches, fall back to baseline
    ai_searches = bounded_plan.get("searches") or []
    if not ai_searches:
        return baseline_plan

    # Ensure exact dates are first (baseline guarantee)
    final_plan = _ensure_exact_first(baseline_plan, bounded_plan)

    return final_plan


def build_ai_search_plan(vacation: Any) -> dict[str, Any]:
    """Build a search plan using AI if enabled/configured, else deterministic baseline.

    Convenience wrapper that loads SourceConfig internally.
    """
    from app.services.source_config import load_source_config
    config = load_source_config()
    return build_search_plan_with_config(vacation, config)


# ---------------------------------------------------------------------------
# Bounded limits enforcement
# ---------------------------------------------------------------------------

def _apply_bounded_limits(plan: dict[str, Any], config: SourceConfig) -> dict[str, Any]:
    """Apply bounded limits to an AI-generated plan."""
    max_structured = config.ai_search_planner_max_structured_searches
    max_research = config.ai_search_planner_max_research_queries

    searches = plan.get("searches") or []
    if isinstance(searches, list) and len(searches) > max_structured:
        plan["searches"] = searches[:max_structured]

    fallbacks = plan.get("fallback_searches") or []
    total_with_fallbacks = len(plan.get("searches") or []) + len(fallbacks)
    if total_with_fallbacks > max_structured:
        remaining = max(0, max_structured - len(plan.get("searches") or []))
        plan["fallback_searches"] = fallbacks[:remaining]

    queries = plan.get("research_queries") or []
    if isinstance(queries, list) and len(queries) > max_research:
        plan["research_queries"] = queries[:max_research]

    return plan


def _ensure_exact_first(baseline_plan: dict[str, Any], ai_plan: dict[str, Any]) -> dict[str, Any]:
    """Ensure exact-date searches from baseline appear first in the final plan."""
    # Start with AI searches that have priority >= 1
    ai_searches = list(ai_plan.get("searches") or [])

    # Find any flight search with exact dates and priority > 1; demote it
    for i, s in enumerate(ai_searches):
        if (s.get("search_type") == "flight"
                and s.get("traveler_strategy") == "exact"
                and s.get("priority", 1) > 1):
            ai_searches[i]["priority"] = 2

    # Insert baseline exact-flight search at position 0 if not already present
    baseline_exact_flight = None
    for s in baseline_plan.get("searches") or []:
        if s.get("search_type") == "flight" and s.get("traveler_strategy") == "exact":
            baseline_exact_flight = dict(s)
            break

    if baseline_exact_flight:
        # Check if AI already has an identical exact-flight search
        ai_has_exact = any(
            a.get("search_type") == "flight"
            and a.get("origin_airport") == baseline_exact_flight["origin_airport"]
            and a.get("destination_airport") == baseline_exact_flight["destination_airport"]
            and a.get("departure_date") == baseline_exact_flight["departure_date"]
            for a in ai_searches
        )
        if not ai_has_exact:
            ai_searches.insert(0, baseline_exact_flight)

    # Re-number priorities sequentially starting from 1
    for i, s in enumerate(ai_searches):
        s["priority"] = i + 1

    ai_plan["searches"] = ai_searches

    # Merge reasoning summaries
    ai_reasoning = ai_plan.get("reasoning_summary", "")
    baseline_reasoning = baseline_plan.get("reasoning_summary", "")
    if ai_reasoning and baseline_reasoning:
        ai_plan["reasoning_summary"] = f"{ai_reasoning} | Baseline: {baseline_reasoning}"
    elif baseline_reasoning:
        ai_plan["reasoning_summary"] = baseline_reasoning

    # Merge warnings
    all_warnings = list(ai_plan.get("warnings") or [])
    for w in (baseline_plan.get("warnings") or []):
        if w not in all_warnings:
            all_warnings.append(w)
    ai_plan["warnings"] = all_warnings

    return ai_plan


# ---------------------------------------------------------------------------
# Flexible date variant generator (for AI planner use when enabled)
# ---------------------------------------------------------------------------

def generate_flexible_date_variants(
    baseline_searches: list[dict[str, Any]],
    flex_days: int = DATE_FLEX_DAYS_DEFAULT,
) -> list[dict[str, Any]]:
    """Generate bounded nearby-date variants for a list of baseline searches.

    Used internally when AI date flexibility is enabled. Returns fallback searches.
    """
    variants: list[dict[str, Any]] = []

    for search in baseline_searches:
        if search.get("search_type") != "flight":
            continue

        departure = _parse_date(search.get("departure_date"))
        return_d = _parse_date(search.get("return_date"))

        if not departure or not return_d:
            continue

        for offset in range(1, flex_days + 1):
            earlier = departure - timedelta(days=offset)
            variants.append({
                "search_type": "flight",
                "origin_airport": search.get("origin_airport", ""),
                "destination_airport": search.get("destination_airport", ""),
                "departure_date": _iso(earlier),
                "return_date": _iso(return_d),
                "traveler_strategy": "flexible",
                "priority": 99,  # Will be re-numbered later
                "reason": f"One-day earlier departure ({_iso(earlier)}) may reduce fare.",
            })

            later = departure + timedelta(days=offset)
            variants.append({
                "search_type": "flight",
                "origin_airport": search.get("origin_airport", ""),
                "destination_airport": search.get("destination_airport", ""),
                "departure_date": _iso(later),
                "return_date": _iso(return_d),
                "traveler_strategy": "flexible",
                "priority": 99,
                "reason": f"One-day later departure ({_iso(later)}) may reduce fare.",
            })

    return variants


# ---------------------------------------------------------------------------
# Backwards compatibility: build_search_plan (existing API)
# ---------------------------------------------------------------------------

def build_search_plan(vacation: Any) -> dict[str, Any]:
    """Build a search plan. Uses AI if enabled, else deterministic baseline.

    This is the existing public API; it loads SourceConfig internally for
    backwards compatibility with callers that don't pass config.
    """
    return build_ai_search_plan(vacation)
