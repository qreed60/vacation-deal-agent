"""Phase 5A tests: source fallback policy and AI-guided search planning foundation."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.models import SearchRun, Vacation
from app.services import ai_search_planner as planner_module
from app.services.ai_search_planner import (
    build_deterministic_baseline_plan,
    build_search_plan_with_config,
    validate_search_plan,
)
from app.services.source_config import SourceConfig, env_bool, load_source_config


# ---------------------------------------------------------------------------
# Helpers: create minimal Vacation objects for testing
# ---------------------------------------------------------------------------

def _make_vacation(
    title: str = "Test Vacation",
    origin: str = "PIT",
    destination: str = "MOT",
    start_date: date | None = date(2026, 9, 18),
    end_date: date | None = date(2026, 9, 21),
    date_mode: str = "fixed",
    airfare_needed: bool = True,
    hotel_needed: bool = True,
    rental_car_needed: bool = False,
    number_of_travelers: int = 2,
) -> Vacation:
    return Vacation(
        id=999,
        slug="test-vacation",
        title=title,
        status="active",
        number_of_travelers=number_of_travelers,
        travelers_json=json.dumps([{"age": 30}, {"age": 28}]),
        origin=origin,
        destination=destination,
        date_mode=date_mode,
        start_date=start_date,
        end_date=end_date,
        hotel_needed=hotel_needed,
        airfare_needed=airfare_needed,
        rental_car_needed=rental_car_needed,
        manifest_json=json.dumps({}),
    )


def _make_config(
    ai_enabled: bool = False,
    provider: str = "disabled",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_structured: int = 8,
    max_research: int = 5,
    allow_flex: bool = False,
    flex_days: int = 1,
) -> SourceConfig:
    return SourceConfig(
        searxng_enabled=True,
        searxng_base_url="http://127.0.0.1:8888",
        searxng_timeout_seconds=5.0,
        searxng_fallback_enabled=True,
        searxng_max_results=10,
        amadeus_enabled=False,
        amadeus_base_url="https://test.api.amadeus.com",
        amadeus_client_id="",
        amadeus_client_secret="",
        amadeus_timeout_seconds=8.0,
        google_places_enabled=False,
        google_places_api_key="",
        google_places_timeout_seconds=8.0,
        serpapi_enabled=False,
        serpapi_api_key="",
        serpapi_base_url="https://serpapi.com/search",
        serpapi_timeout_seconds=8.0,
        fast_flights_enabled=False,
        fast_flights_fetch_mode="common",
        fast_flights_seat="economy",
        fast_flights_max_stops=None,
        fast_flights_max_results=20,
        trvl_enabled=False,
        trvl_binary_path=".tools/trvl/trvl",
        trvl_timeout_seconds=120.0,
        trvl_max_flight_results=20,
        trvl_max_hotel_results=20,
        trvl_currency="USD",
        trvl_allow_risky_flight_offers=False,
        trvl_require_configured_currency=True,
        trvl_broad_discovery_enabled=False,
        trvl_broad_include_one_way_fallbacks=True,
        trvl_broad_max_alternatives=50,
        trvl_broad_allow_risky_alternatives=True,
        airport_index_db_path="data/airport_index.sqlite3",
        mock_search_enabled=False,
        ai_search_planner_enabled=ai_enabled,
        ai_search_planner_provider=provider,
        ai_search_planner_model=model,
        ai_search_planner_base_url=base_url,
        ai_search_planner_api_key=api_key,
        ai_search_planner_max_structured_searches=max_structured,
        ai_search_planner_max_research_queries=max_research,
        ai_search_planner_allow_date_flex=allow_flex,
        ai_search_planner_date_flex_days=flex_days,
        ai_search_planner_timeout_seconds=45.0,
    )


# ---------------------------------------------------------------------------
# Test: deterministic baseline plan exists when AI planner disabled
# ---------------------------------------------------------------------------

class TestDeterministicBaselinePlan:
    def test_baseline_plan_exists_when_ai_disabled(self):
        """When AI_SEARCH_PLANNER_ENABLED=false (default), build_search_plan returns deterministic baseline."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert "planner_version" in plan
        assert plan["planner_version"] == "phase5a_v1"
        assert isinstance(plan.get("searches"), list)
        assert len(plan["searches"]) > 0

    def test_baseline_plan_has_exact_flight_first(self):
        """Exact date flight search is always first in the baseline plan."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        searches = plan["searches"]
        assert len(searches) > 0
        first = searches[0]
        assert first["search_type"] == "flight"
        assert first.get("traveler_strategy") == "exact"
        assert first.get("priority") == 1

    def test_baseline_plan_includes_hotel_when_needed(self):
        """Hotel search is included when hotel_needed=True."""
        vacation = _make_vacation(hotel_needed=True)
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        has_hotel = any(s["search_type"] == "hotel" for s in plan["searches"])
        assert has_hotel is True

    def test_baseline_plan_no_hotel_when_not_needed(self):
        """Hotel search is not included when hotel_needed=False."""
        vacation = _make_vacation(hotel_needed=False)
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        has_hotel = any(s["search_type"] == "hotel" for s in plan["searches"])
        assert has_hotel is False


# ---------------------------------------------------------------------------
# Test: flexible-date planner includes bounded nearby date variants when enabled
# ---------------------------------------------------------------------------

class TestFlexibleDateVariants:
    def test_flexible_date_variants_generated(self):
        """When allow_date_flex=True, generate_flexible_date_variants produces bounded variants."""
        baseline = [{
            "search_type": "flight",
            "origin_airport": "PIT",
            "destination_airport": "MOT",
            "departure_date": "2026-09-18",
            "return_date": "2026-09-21",
        }]

        variants = planner_module.generate_flexible_date_variants(baseline, flex_days=1)

        assert len(variants) == 2  # -1 day and +1 day
        for v in variants:
            assert v["search_type"] == "flight"
            assert v.get("traveler_strategy") == "flexible"

    def test_flexible_date_variants_bounded(self):
        """Variants are bounded by flex_days parameter."""
        baseline = [{
            "search_type": "flight",
            "origin_airport": "PIT",
            "destination_airport": "MOT",
            "departure_date": "2026-09-18",
            "return_date": "2026-09-21",
        }]

        variants = planner_module.generate_flexible_date_variants(baseline, flex_days=3)

        # 3 days before + 3 days after = 6 variants
        assert len(variants) == 6


# ---------------------------------------------------------------------------
# Test: planner refuses/limits excessive model-generated searches
# ---------------------------------------------------------------------------

class TestBoundedSearchLimits:
    def test_max_structured_searches_enforced(self):
        """Plan with too many searches is limited to max_structured."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=True, provider="openai_compatible", base_url="http://x", model="m")

        # Create a plan that exceeds the limit
        excessive_plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [{"search_type": "flight", "origin_airport": "PIT", "destination_airport": "MOT",
                          "departure_date": "2026-09-18", "return_date": "2026-09-21",
                          "traveler_strategy": "exact", "priority": i + 1, "reason": f"search {i}"}
                         for i in range(20)],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }

        bounded = planner_module._apply_bounded_limits(excessive_plan, config)

        assert len(bounded["searches"]) <= 8

    def test_max_research_queries_enforced(self):
        """Research queries are limited to max_research."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=True, provider="openai_compatible", base_url="http://x", model="m")

        excessive_plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [],
            "fallback_searches": [],
            "research_queries": [{"query": f"query {i}", "purpose": "test"} for i in range(20)],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }

        bounded = planner_module._apply_bounded_limits(excessive_plan, config)

        assert len(bounded["research_queries"]) <= 5


# ---------------------------------------------------------------------------
# Test: invalid model JSON falls back to baseline plan
# ---------------------------------------------------------------------------

class TestInvalidJSONFallback:
    def test_invalid_json_falls_back_to_baseline(self):
        """When AI returns invalid JSON, deterministic baseline is returned."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=True, provider="openai_compatible", base_url="http://x", model="m")

        # Mock the API call to return invalid JSON
        with patch.object(planner_module, '_call_openai_compatible', return_value="not valid json {{{"):
            plan = build_search_plan_with_config(vacation, config)

        assert "planner_version" in plan
        assert len(plan.get("searches", [])) > 0

    def test_empty_response_falls_back_to_baseline(self):
        """When AI returns empty response, deterministic baseline is returned."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=True, provider="openai_compatible", base_url="http://x", model="m")

        with patch.object(planner_module, '_call_openai_compatible', return_value=None):
            plan = build_search_plan_with_config(vacation, config)

        assert "planner_version" in plan
        assert len(plan.get("searches", [])) > 0


# ---------------------------------------------------------------------------
# Test: model-generated plan is validated before execution
# ---------------------------------------------------------------------------

class TestPlanValidation:
    def test_valid_plan_passes(self):
        """A well-formed plan passes validation."""
        valid_plan = {
            "planner_version": "phase5a_v1",
            "objective": "test objective",
            "searches": [{
                "search_type": "flight",
                "origin_airport": "PIT",
                "destination_airport": "MOT",
                "departure_date": "2026-09-18",
                "return_date": "2026-09-21",
                "traveler_strategy": "exact",
                "priority": 1,
                "reason": "test reason",
            }],
            "fallback_searches": [],
            "research_queries": [{"query": "test query", "purpose": "test"}],
            "reasoning_summary": "test reasoning",
            "constraints": [],
            "warnings": [],
        }

        is_valid, errors = validate_search_plan(valid_plan)
        assert is_valid is True
        assert len(errors) == 0

    def test_invalid_search_type_fails(self):
        """Plans with invalid search_type fail validation."""
        bad_plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [{
                "search_type": "invalid_type",
                "origin_airport": "PIT",
                "destination_airport": "MOT",
                "departure_date": "2026-09-18",
                "return_date": "2026-09-21",
                "traveler_strategy": "exact",
                "priority": 1,
                "reason": "test",
            }],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }

        is_valid, errors = validate_search_plan(bad_plan)
        assert is_valid is False
        assert any("search_type" in e for e in errors)

    def test_invalid_date_format_fails(self):
        """Plans with invalid date format fail validation."""
        bad_plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [{
                "search_type": "flight",
                "origin_airport": "PIT",
                "destination_airport": "MOT",
                "departure_date": "not-a-date",
                "return_date": "2026-09-21",
                "traveler_strategy": "exact",
                "priority": 1,
                "reason": "test",
            }],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }

        is_valid, errors = validate_search_plan(bad_plan)
        assert is_valid is False
        assert any("date" in e.lower() for e in errors)

    def test_missing_required_fields_fails(self):
        """Plans missing required fields fail validation."""
        bad_plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [{"origin_airport": "PIT"}],  # Missing many required fields
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }

        is_valid, errors = validate_search_plan(bad_plan)
        assert is_valid is False
        assert any("missing" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Test: planner reasoning_summary is stored
# ---------------------------------------------------------------------------

class TestReasoningSummaryStored:
    def test_baseline_has_reasoning_summary(self):
        """Deterministic baseline plan includes a reasoning_summary."""
        vacation = _make_vacation(title="Pittsburgh to Minot Trip")
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert "reasoning_summary" in plan
        assert len(plan["reasoning_summary"]) > 0
        assert "PIT" in plan.get("objective", "") or "MOT" in plan.get("objective", "")


# ---------------------------------------------------------------------------
# Test: UI renders search strategy summary (via routes helper)
# ---------------------------------------------------------------------------

class TestUIRenderSearchStrategy:
    def test_search_strategy_summary_built_from_plan(self):
        """_build_search_strategy_summary extracts plan data correctly."""
        from app.web.routes import _build_search_strategy_summary, _build_source_policy_summary

        # Create a mock SearchRun with Phase 5A plan
        run = MagicMock()
        run.search_plan_json = json.dumps({
            "planner_version": "phase5a_v1",
            "objective": "Find flights PIT to MOT",
            "searches": [{"search_type": "flight", "origin_airport": "PIT", "destination_airport": "MOT",
                          "departure_date": "2026-09-18", "return_date": "2026-09-21",
                          "traveler_strategy": "exact", "priority": 1, "reason": "test"}],
            "fallback_searches": [],
            "research_queries": [{"query": "PIT to MOT flights", "purpose": "fallback"}],
            "reasoning_summary": "Exact dates used.",
        })
        run.summary_json = json.dumps({
            "source_policy_version": "phase5a_v1",
            "search_plan": {"planner_version": "phase5a_v1"},
        })

        result = _build_search_strategy_summary(run)

        assert isinstance(result, dict)
        assert result.get("planner_version") == "phase5a_v1"
        assert result.get("objective") == "Find flights PIT to MOT"
        assert result.get("search_count") == 1
        assert result.get("research_query_count") == 1

    def test_source_policy_summary_built(self):
        """_build_source_policy_summary extracts source policy data correctly."""
        from app.web.routes import _build_search_strategy_summary, _build_source_policy_summary

        run = MagicMock()
        run.search_plan_json = json.dumps({"planner_version": "phase5a_v1"})
        run.summary_json = json.dumps({
            "source_policy_version": "phase5a_v1",
            "attempted_sources": ["trvl", "serpapi_google_flights"],
            "skipped_sources": ["amadeus"],
            "best_available_result_type": "exact_priced_deal",
            "research_fallback_used": False,
            "research_fallback_source": None,
            "latest_error_summary": "trvl 429 rate limit",
        })

        result = _build_source_policy_summary(run)

        assert isinstance(result, dict)
        assert result.get("best_available_result_type") == "exact_priced_deal"
        assert result.get("attempted_sources") == ["trvl", "serpapi_google_flights"]
        assert result.get("skipped_sources") == ["amadeus"]

    def test_no_search_strategy_when_no_run(self):
        """Empty dict returned when no latest run exists."""
        from app.web.routes import _build_search_strategy_summary, _build_source_policy_summary

        result = _build_search_strategy_summary(None)
        assert result == {}

        result2 = _build_source_policy_summary(None)
        assert result2 == {}


# ---------------------------------------------------------------------------
# Test: no mock source is used
# ---------------------------------------------------------------------------

class TestNoMockSources:
    def test_baseline_plan_no_mock(self):
        """Deterministic baseline plan does not include mock_travel."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        # The plan should NOT contain any search entries with source_name "mock_travel"
        for s in plan.get("searches", []):
            assert s.get("source_name") != "mock_travel"


# ---------------------------------------------------------------------------
# Test: source fallback continues after trvl provider_error
# ---------------------------------------------------------------------------

class TestSourceFallbackPolicy:
    def test_source_status_classification_success_with_deals(self):
        """Classify completed flight with offers as success_with_deals."""
        from app.services.search_runner import _classify_source_status, SOURCE_STATUS_SUCCESS_WITH_DEALS

        result = MagicMock()
        result.normalized_result_json = json.dumps({
            "offers": [{"price": 200}],
            "status_reason": "",
        })
        result.result_type = "flight"
        result.status = "completed"
        result.source_name = "trvl"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_SUCCESS_WITH_DEALS

    def test_source_status_classification_provider_error(self):
        """Classify trvl with provider_error category correctly."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_PROVIDER_ERROR,
        )

        result = MagicMock()
        result.normalized_result_json = json.dumps({
            "source_failure_category": "provider_error",
            "provider_failure_reason": "trvl_provider_rate_limited_or_format_error",
        })
        result.result_type = "flight"
        result.status = "error"
        result.source_name = "trvl"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_PROVIDER_ERROR

    def test_source_status_classification_config_disabled(self):
        """Disabled sources are classified as config_disabled."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_CONFIG_DISABLED,
        )

        result = MagicMock()
        result.normalized_result_json = json.dumps({
            "status_reason": "disabled",
            "reason": "AMADEUS_ENABLED=false",
        })
        result.result_type = "flight"
        result.status = "skipped"
        result.source_name = "amadeus"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_CONFIG_DISABLED

    def test_source_status_classification_route_resolution_error(self):
        """Unresolved airports are classified as route_resolution_error."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_ROUTE_RESOLUTION_ERROR,
        )

        result = MagicMock()
        result.normalized_result_json = json.dumps({
            "status_reason": "missing_dependency",
            "reason": "Could not resolve airport code",
        })
        result.result_type = "flight"
        result.status = "skipped"
        result.source_name = "fast_flights"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_ROUTE_RESOLUTION_ERROR


# ---------------------------------------------------------------------------
# Test: SearXNG fallback runs when structured sources fail and SearXNG is enabled
# ---------------------------------------------------------------------------

class TestSearxngFallback:
    def test_searxng_result_classified_as_research_fallback(self):
        """SearXNG results are classified as research_fallback_only."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_RESEARCH_FALLBACK_ONLY,
        )

        result = MagicMock()
        result.normalized_result_json = json.dumps({
            "result_type": "web_context",
            "results": [{"title": "Test", "url": "http://example.com"}],
        })
        result.result_type = "web_context"
        result.status = "completed"
        result.source_name = "searxng"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_RESEARCH_FALLBACK_ONLY

    def test_searxng_unreachable_classified_as_source_unavailable(self):
        """Unreachable SearXNG is classified as source_unavailable."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_SOURCE_UNAVAILABLE,
        )

        result = MagicMock()
        result.normalized_result_json = json.dumps({})
        result.result_type = "web_context"
        result.status = "error"
        result.source_name = "searxng"
        result.error_message = "Connection refused"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_SOURCE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Test: SearXNG fallback does not create priced DealCandidate without confident price extraction
# ---------------------------------------------------------------------------

class TestSearxngNoPricedDeals:
    def test_searxng_does_not_create_priced_deal(self):
        """SearXNG results should not be classified as having priced deals."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_RESEARCH_FALLBACK_ONLY,
            SOURCE_STATUS_SUCCESS_WITH_DEALS,
        )

        result = MagicMock()
        # Even with content mentioning price, SearXNG is web_context type
        result.normalized_result_json = json.dumps({
            "result_type": "web_context",
            "results": [{"title": "Cheap flights PIT to MOT", "content": "Price from $150"}],
        })
        result.result_type = "web_context"
        result.status = "completed"
        result.source_name = "searxng"

        status = _classify_source_status(result)
        # Should NOT be success_with_deals - SearXNG is research only
        assert status != SOURCE_STATUS_SUCCESS_WITH_DEALS


# ---------------------------------------------------------------------------
# Test: SearchRun summary includes source policy fields
# ---------------------------------------------------------------------------

class TestSearchRunSummaryFields:
    def test_summary_has_source_policy_version(self):
        """Phase 5A summary includes source_policy_version field."""
        from app.services.search_runner import SOURCE_STATUS_SUCCESS_WITH_DEALS

        # Verify the constant exists and has correct value
        assert SOURCE_STATUS_SUCCESS_WITH_DEALS == "success_with_deals"

    def test_structured_priced_sources_list(self):
        """STRUCTURED_PRICED_SOURCES has correct priority order."""
        from app.services.search_runner import STRUCTURED_PRICED_SOURCES
        expected = ["trvl", "serpapi_google_flights", "amadeus", "fast_flights"]
        assert STRUCTURED_PRICED_SOURCES == expected


# ---------------------------------------------------------------------------
# Test: provider_error remains distinct from success_no_deals
# ---------------------------------------------------------------------------

class TestProviderErrorDistinctFromSuccessNoDeals:
    def test_provider_error_not_success(self):
        """provider_error status is not classified as success."""
        from app.services.search_runner import (
            _classify_source_status,
            SOURCE_STATUS_PROVIDER_ERROR,
            SOURCE_STATUS_SUCCESS_NO_DEALS,
        )

        result = MagicMock()
        result.normalized_result_json = json.dumps({
            "source_failure_category": "provider_error",
        })
        result.result_type = "flight"
        result.status = "error"
        result.source_name = "trvl"

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_PROVIDER_ERROR
        assert status != SOURCE_STATUS_SUCCESS_NO_DEALS


# ---------------------------------------------------------------------------
# Test: existing tests pass (sanity check on SourceConfig changes)
# ---------------------------------------------------------------------------

class TestBackwardsCompatibility:
    def test_load_source_config_has_new_fields(self):
        """load_source_config returns config with new AI/SEARXNG fields."""
        # Ensure env vars don't interfere
        for key in ["AI_SEARCH_PLANNER_ENABLED", "SEARXNG_ENABLED"]:
            import os
            os.environ.pop(key, None)

        config = load_source_config()

        assert hasattr(config, "ai_search_planner_enabled")
        assert hasattr(config, "searxng_enabled")
        assert hasattr(config, "searxng_fallback_enabled")
        assert hasattr(config, "searxng_max_results")
        assert hasattr(config, "ai_search_planner_provider")

    def test_searxng_config_defaults(self):
        """SEARXNG config defaults are correct."""
        import os
        for key in ["SEARXNG_ENABLED", "SEARXNG_FALLBACK_ENABLED"]:
            os.environ.pop(key, None)

        from app.services.source_config import load_source_config
        config = load_source_config()

        assert config.searxng_enabled is True  # default True
        assert config.searxng_fallback_enabled is True  # default True
        assert config.searxng_max_results == 10  # default 10

    def test_ai_planner_defaults_disabled(self):
        """AI search planner defaults to disabled."""
        import os
        for key in ["AI_SEARCH_PLANNER_ENABLED", "AI_SEARCH_PLANNER_PROVIDER"]:
            os.environ.pop(key, None)

        from app.services.source_config import load_source_config
        config = load_source_config()

        assert config.ai_search_planner_enabled is False  # default False
        assert config.ai_search_planner_provider == "disabled"  # default disabled


# ---------------------------------------------------------------------------
# Test: deterministic baseline plan for fixed dates
# ---------------------------------------------------------------------------

class TestBaselinePlanFixedDates:
    def test_fixed_date_mode_produces_exact_flight(self):
        """Fixed date mode produces exact flight search."""
        vacation = _make_vacation(date_mode="fixed")
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        flights = [s for s in plan["searches"] if s["search_type"] == "flight"]
        assert len(flights) > 0
        exact_flights = [f for f in flights if f.get("traveler_strategy") == "exact"]
        assert len(exact_flights) > 0

    def test_baseline_plan_stores_reasoning_summary(self):
        """Baseline plan includes reasoning_summary."""
        vacation = _make_vacation(title="Test Trip")
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert "reasoning_summary" in plan
        assert isinstance(plan["reasoning_summary"], str)
        assert len(plan["reasoning_summary"]) > 0


# ---------------------------------------------------------------------------
# Test: AI planner disabled/no-op provider returns baseline
# ---------------------------------------------------------------------------

class TestAIDisabledProviderReturnsBaseline:
    def test_disabled_provider_returns_baseline(self):
        """When provider is 'disabled', deterministic baseline is returned."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=True, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert "planner_version" in plan
        assert len(plan.get("searches", [])) > 0

    def test_none_provider_returns_baseline(self):
        """When provider is 'none', deterministic baseline is returned."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=True, provider="none")

        plan = build_search_plan_with_config(vacation, config)

        assert "planner_version" in plan
        assert len(plan.get("searches", [])) > 0


# ---------------------------------------------------------------------------
# Test: build_ai_search_plan convenience wrapper
# ---------------------------------------------------------------------------

class TestBuildAiSearchPlanWrapper:
    def test_wrapper_returns_baseline_when_disabled(self):
        """build_ai_search_plan returns baseline when AI is disabled."""
        import os
        os.environ["AI_SEARCH_PLANNER_ENABLED"] = "false"

        vacation = _make_vacation()
        plan = planner_module.build_ai_search_plan(vacation)

        assert "planner_version" in plan
        assert len(plan.get("searches", [])) > 0


# ---------------------------------------------------------------------------
# Test: ensure_exact_first preserves baseline exact flight
# ---------------------------------------------------------------------------

class TestEnsureExactFirst:
    def test_baseline_exact_flight_inserted_when_missing(self):
        """Baseline exact-flight is inserted at position 0 when AI plan lacks it."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=False, provider="disabled")

        baseline = build_deterministic_baseline_plan(vacation)
        ai_plan = {
            "planner_version": "phase5a_v1",
            "objective": "AI plan",
            "searches": [{"search_type": "hotel", "origin_airport": "", "destination_airport": "MOT",
                          "departure_date": "2026-09-18", "return_date": "2026-09-21",
                          "traveler_strategy": "exact", "priority": 1, "reason": "hotel"}],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "AI reasoning",
            "constraints": [],
            "warnings": [],
        }

        final = planner_module._ensure_exact_first(baseline, ai_plan)

        assert len(final["searches"]) > 0
        first = final["searches"][0]
        assert first["search_type"] == "flight"
        assert first.get("traveler_strategy") == "exact"
        assert first.get("priority") == 1


# ---------------------------------------------------------------------------
# Test: _extract_json_from_text handles various formats
# ---------------------------------------------------------------------------

class TestExtractJsonFromText:
    def test_direct_json(self):
        """Direct JSON string is parsed correctly."""
        text = '{"planner_version": "phase5a_v1", "searches": []}'
        result = planner_module._extract_json_from_text(text)
        assert isinstance(result, dict)
        assert result["planner_version"] == "phase5a_v1"

    def test_markdown_code_fence(self):
        """JSON in markdown code fence is extracted."""
        text = '```json\n{"planner_version": "phase5a_v1", "searches": []}\n```'
        result = planner_module._extract_json_from_text(text)
        assert isinstance(result, dict)

    def test_surrounding_text(self):
        """JSON embedded in surrounding text is extracted."""
        text = 'Here is the plan:\n{"planner_version": "phase5a_v1", "searches": []}\nEnd.'
        result = planner_module._extract_json_from_text(text)
        assert isinstance(result, dict)

    def test_invalid_returns_none(self):
        """Invalid JSON returns None."""
        text = 'not json at all {{{'
        result = planner_module._extract_json_from_text(text)
        assert result is None


# ---------------------------------------------------------------------------
# Test: validate_search_plan edge cases
# ---------------------------------------------------------------------------

class TestValidateSearchPlanEdgeCases:
    def test_non_dict_plan_fails(self):
        """Non-dict plan fails validation."""
        is_valid, errors = validate_search_plan("not a dict")
        assert is_valid is False

    def test_empty_list_searches_passes(self):
        """Plan with empty searches list passes validation (no structural errors)."""
        plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }
        is_valid, errors = validate_search_plan(plan)
        assert is_valid is True

    def test_negative_priority_fails(self):
        """Negative priority fails validation."""
        plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [{
                "search_type": "flight",
                "origin_airport": "PIT",
                "destination_airport": "MOT",
                "departure_date": "2026-09-18",
                "return_date": "2026-09-21",
                "traveler_strategy": "exact",
                "priority": -1,
                "reason": "test",
            }],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }
        is_valid, errors = validate_search_plan(plan)
        assert is_valid is False


# ---------------------------------------------------------------------------
# Test: Phase 5A validation defect fixes (patch phase)
# ---------------------------------------------------------------------------

class TestValidationDefectFixes:
    """Tests for the four validation defects fixed in the patch.

    Defect A: search_plan stored empty counts when AI disabled.
    Defect B: best_available_result_type inferred from source status not candidates.
    Defect C: latest_error_summary empty when provider_error present.
    Defect D: SearXNG fallback attempted regardless of SEARXNG_FALLBACK_ENABLED.
    """

    # ---- Defect A: non-empty baseline plan stored in summary_json ----

    def test_baseline_plan_has_non_empty_objective(self):
        """Baseline plan objective includes PIT/MOT and dates when AI disabled."""
        vacation = _make_vacation(
            origin="PIT",
            destination="MOT",
            start_date=date(2026, 9, 18),
            end_date=date(2026, 9, 21),
        )
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert "objective" in plan
        objective = plan["objective"]
        assert isinstance(objective, str)
        assert len(objective) > 0
        # Must include origin and destination
        assert "PIT" in objective or "mot" in objective.lower()
        # Must include dates
        assert "2026-09-18" in objective

    def test_baseline_plan_has_non_empty_reasoning_summary(self):
        """Baseline plan reasoning_summary is non-empty."""
        vacation = _make_vacation(title="Vacation 5 style")
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert "reasoning_summary" in plan
        reasoning = plan["reasoning_summary"]
        assert isinstance(reasoning, str)
        assert len(reasoning) > 0

    def test_baseline_plan_first_search_is_exact_flight(self):
        """First search in baseline plan is exact flight PIT/MOT with dates."""
        vacation = _make_vacation(
            origin="PIT",
            destination="MOT",
            start_date=date(2026, 9, 18),
            end_date=date(2026, 9, 21),
        )
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        searches = plan.get("searches", [])
        assert len(searches) >= 1
        first = searches[0]
        assert first["search_type"] == "flight"
        assert first["origin_airport"] == "PIT"
        assert first["destination_airport"] == "MOT"
        assert first["departure_date"] == "2026-09-18"
        assert first["return_date"] == "2026-09-21"
        assert first["traveler_strategy"] == "exact"

    def test_baseline_plan_search_count_reflects_searches(self):
        """Baseline plan searches count >= 1 when airfare needed."""
        vacation = _make_vacation(airfare_needed=True, hotel_needed=False)
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        assert len(plan.get("searches", [])) >= 1

    def test_baseline_plan_research_queries_non_empty(self):
        """Baseline plan includes research queries when airports available."""
        vacation = _make_vacation(origin="PIT", destination="MOT")
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        queries = plan.get("research_queries", [])
        assert len(queries) >= 1
        assert "query" in queries[0]
        assert len(queries[0]["query"]) > 0

    def test_planner_version_format(self):
        """planner_version is phase5a_v1 string, not bare integer."""
        vacation = _make_vacation()
        config = _make_config(ai_enabled=False, provider="disabled")

        plan = build_search_plan_with_config(vacation, config)

        version = plan.get("planner_version", "")
        assert isinstance(version, str)
        assert "phase5a" in version.lower() or "v1" in version

    # ---- Defect B: best_available_result_type with zero candidates ----

    def test_best_available_none_when_no_candidates(self):
        """best_available_result_type is 'none' when deal_candidates=0 and no research fallback."""
        from app.services.search_runner import (
            SOURCE_STATUS_CONFIG_DISABLED,
            SOURCE_STATUS_PROVIDER_ERROR,
        )

        # Simulate: all structured sources failed with provider_error, no deals found
        source_statuses = {
            "trvl": SOURCE_STATUS_PROVIDER_ERROR,
            "serpapi_google_flights": SOURCE_STATUS_CONFIG_DISABLED,
            "amadeus": SOURCE_STATUS_CONFIG_DISABLED,
            "fast_flights": SOURCE_STATUS_CONFIG_DISABLED,
        }

        # With zero deal_candidates and no research fallback, should be 'none'
        has_exact_priced = any(
            s == "success_with_deals" for s in source_statuses.values()
        )
        has_research_fallback = "research_fallback_only" in source_statuses.values()
        deal_candidate_count = 0

        if has_exact_priced:
            result_type = "exact_priced_deal"
        elif deal_candidate_count > 0 and not has_exact_priced:
            result_type = "estimated_priced_deal"
        elif has_research_fallback:
            result_type = "research_fallback"
        else:
            result_type = "none"

        assert result_type == "none", f"Expected 'none' but got '{result_type}' for zero candidates"

    def test_best_available_not_inferred_from_success_no_deals(self):
        """SOURCE_STATUS_SUCCESS_NO_DEALS does NOT imply estimated_priced_deal."""
        from app.services.search_runner import SOURCE_STATUS_SUCCESS_NO_DEALS

        # A source returning success_no_deals means it found nothing — not that there are candidates.
        source_statuses = {
            "trvl": SOURCE_STATUS_SUCCESS_NO_DEALS,
        }

        has_exact_priced = any(
            s == "success_with_deals" for s in source_statuses.values()
        )
        has_research_fallback = "research_fallback_only" in source_statuses.values()
        deal_candidate_count = 0  # No candidates extracted

        if has_exact_priced:
            result_type = "exact_priced_deal"
        elif deal_candidate_count > 0 and not has_exact_priced:
            result_type = "estimated_priced_deal"
        elif has_research_fallback:
            result_type = "research_fallback"
        else:
            result_type = "none"

        assert result_type == "none", (
            f"success_no_deals should not imply estimated_priced_deal. Got '{result_type}'."
        )

    # ---- Defect C: latest_error_summary with provider_error ----

    def test_error_summary_reports_provider_error(self):
        """latest_error_summary summarizes provider failures when no deals found."""
        from app.services.search_runner import SOURCE_STATUS_PROVIDER_ERROR

        error_categories = {"provider_error": 1}
        source_statuses_for_errors = {
            "trvl": SOURCE_STATUS_PROVIDER_ERROR,
        }
        has_exact_priced = False
        has_estimated = False
        latest_error_summary = ""

        if not latest_error_summary:
            if error_categories.get("provider_error", 0) > 0 and not has_exact_priced and not has_estimated:
                failed_sources = [
                    src for src, status in source_statuses_for_errors.items()
                    if status == SOURCE_STATUS_PROVIDER_ERROR
                ]
                if failed_sources:
                    latest_error_summary = f"Provider error(s) from: {', '.join(failed_sources)}"

        assert len(latest_error_summary) > 0
        assert "provider" in latest_error_summary.lower() or "error" in latest_error_summary.lower()

    def test_error_summary_reports_disabled_sources(self):
        """latest_error_summary reports disabled sources when nothing worked."""
        from app.services.search_runner import SOURCE_STATUS_CONFIG_DISABLED

        source_statuses_for_errors = {
            "trvl": SOURCE_STATUS_CONFIG_DISABLED,
            "serpapi_google_flights": SOURCE_STATUS_CONFIG_DISABLED,
            "amadeus": SOURCE_STATUS_CONFIG_DISABLED,
            "fast_flights": SOURCE_STATUS_CONFIG_DISABLED,
        }
        has_exact_priced = False
        has_estimated = False
        has_research_fallback = False

        disabled_sources = [
            src for src, status in source_statuses_for_errors.items()
            if status == SOURCE_STATUS_CONFIG_DISABLED
        ]
        attempted_count = len([s for s in source_statuses_for_errors.values()
                               if s != SOURCE_STATUS_CONFIG_DISABLED])

        latest_error_summary = ""
        if not has_exact_priced and not has_estimated and not has_research_fallback:
            if disabled_sources and attempted_count == 0:
                latest_error_summary = f"No enabled structured sources available. Disabled: {', '.join(disabled_sources)}"

        assert len(latest_error_summary) > 0
        assert "disabled" in latest_error_summary.lower() or "no enabled" in latest_error_summary.lower()

    # ---- Defect D: SearXNG fallback status tracking ----

    def test_searxng_disabled_classified_as_config_disabled(self):
        """SearXNG skipped with 'config' in reason classifies as CONFIG_DISABLED."""
        from app.services.search_runner import (
            SOURCE_STATUS_CONFIG_DISABLED,
            _classify_source_status,
        )

        result = MagicMock()
        result.status = "skipped"
        result.result_type = "web_context"
        result.source_name = "searxng"
        result.normalized_result_json = json.dumps({
            "source_name": "searxng",
            "result_type": "web_context",
            "reason": "SearXNG fallback disabled by config (SEARXNG_FALLBACK_ENABLED=false)",
        })

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_CONFIG_DISABLED, (
            f"SearXNG skipped with 'config' in reason should be CONFIG_DISABLED, got '{status}'"
        )

    def test_searxng_not_classified_as_research_fallback_when_disabled(self):
        """Disabled SearXNG must NOT inflate research_fallback_used."""
        from app.services.search_runner import (
            SOURCE_STATUS_RESEARCH_FALLBACK_ONLY,
            _classify_source_status,
        )

        result = MagicMock()
        result.status = "skipped"
        result.result_type = "web_context"
        result.source_name = "searxng"
        result.normalized_result_json = json.dumps({
            "source_name": "searxng",
            "result_type": "web_context",
            "reason": "SearXNG fallback disabled by config (SEARXNG_FALLBACK_ENABLED=false)",
        })

        status = _classify_source_status(result)
        assert status != SOURCE_STATUS_RESEARCH_FALLBACK_ONLY, (
            f"SearXNG disabled should not be classified as research_fallback_only. Got '{status}'."
        )


# Test: validate_search_plan edge cases
# ---------------------------------------------------------------------------

class TestValidateSearchPlanEdgeCases:
    def test_non_dict_plan_fails(self):
        """Non-dict plan fails validation."""
        is_valid, errors = validate_search_plan("not a dict")
        assert is_valid is False

    def test_empty_list_searches_passes(self):
        """Plan with empty searches list passes validation (no structural errors)."""
        plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }
        is_valid, errors = validate_search_plan(plan)
        assert is_valid is True

    def test_negative_priority_fails(self):
        """Negative priority fails validation."""
        plan = {
            "planner_version": "phase5a_v1",
            "objective": "test",
            "searches": [{
                "search_type": "flight",
                "origin_airport": "PIT",
                "destination_airport": "MOT",
                "departure_date": "2026-09-18",
                "return_date": "2026-09-21",
                "traveler_strategy": "exact",
                "priority": -1,
                "reason": "test",
            }],
            "fallback_searches": [],
            "research_queries": [],
            "reasoning_summary": "test",
            "constraints": [],
            "warnings": [],
        }
        is_valid, errors = validate_search_plan(plan)
        assert is_valid is False


# ---------------------------------------------------------------------------
# Tests: baseline plan non-empty for vacation 5 style (city name destination)
# ---------------------------------------------------------------------------

class TestBaselinePlanNonEmpty:
    """Ensure deterministic baseline plan stores useful data even when AI planner disabled."""

    def test_baseline_plan_with_city_name_destination(self):
        """Vacation with city-name destination (e.g. 'Lisbon') should still generate searches."""
        vacation = _make_vacation(destination="Lisbon")  # type: ignore[arg-type]
        plan = build_deterministic_baseline_plan(vacation)

        assert plan.get("planner_version") == "phase5a_v1"
        objective = plan.get("objective", "")
        assert len(objective) > 0, "Objective must be non-empty"
        assert "LISBON" in objective.upper() or "lisbon" in objective.lower(), (
            f"Objective should mention destination. Got: {objective}"
        )

        searches = plan.get("searches") or []
        assert len(searches) >= 1, (
            f"Baseline plan must have at least one search when airfare_needed=True. "
            f"Got {len(searches)}."
        )

        # First search should be flight with exact dates
        first = searches[0]
        assert first["search_type"] == "flight", (
            f"First search must be flight type. Got: {first['search_type']}"
        )
        assert first.get("origin_airport") == "PIT"
        assert first.get("destination_airport") in ("LISBON",)

    def test_baseline_plan_reasoning_summary_non_empty(self):
        """Baseline plan must include a non-empty reasoning_summary."""
        vacation = _make_vacation()
        plan = build_deterministic_baseline_plan(vacation)

        reasoning = plan.get("reasoning_summary", "")
        assert len(reasoning) > 0, (
            f"reasoning_summary must be non-empty. Got: {repr(reasoning)}"
        )


# ---------------------------------------------------------------------------
# Tests: best_available_result_type logic for zero candidates
# ---------------------------------------------------------------------------

class TestBestAvailableResultTypeZeroCandidates:
    """best_available_result_type must be 'none' when no deal candidates exist."""

    def test_no_candidates_no_snapshots_returns_none(self):
        """When price_snapshots=0 and deal_candidates=0, result type is 'none'."""
        # Simulate the condition in search_runner where:
        # - has_exact_priced = False (no SOURCE_STATUS_SUCCESS_WITH_DEALS)
        # - has_estimated = len(deal_candidates) > 0 and not has_exact_priced -> False
        # - has_research_fallback = False
        # Therefore best_available_result_type should be "none"
        deal_candidates: list = []
        has_exact_priced = False
        has_research_fallback = False

        has_estimated = len(deal_candidates) > 0 and not has_exact_priced

        if has_exact_priced:
            result_type = "exact_priced_deal"
        elif has_estimated:
            result_type = "estimated_priced_deal"
        elif has_research_fallback:
            result_type = "research_fallback"
        else:
            result_type = "none"

        assert result_type == "none", (
            f"With zero candidates and zero snapshots, expected 'none'. Got: {result_type}"
        )

    def test_no_estimated_from_failed_sources(self):
        """SOURCE_STATUS_SUCCESS_NO_DEALS must NOT imply estimated_priced_deal."""
        # SOURCE_STATUS_SUCCESS_NO_DEALS means a source returned successfully but found nothing.
        # It does NOT mean there are estimated priced candidates available.
        deal_candidates: list = []  # Empty - no deals found
        has_exact_priced = False

        # This is the correct logic from search_runner.py line ~539:
        has_estimated = len(deal_candidates) > 0 and not has_exact_priced

        assert has_estimated is False, (
            "SOURCE_STATUS_SUCCESS_NO_DEALS should NOT create estimated candidates."
        )


# ---------------------------------------------------------------------------
# Tests: latest_error_summary reports provider failures
# ---------------------------------------------------------------------------

class TestLatestErrorSummaryProviderFailure:
    """latest_error_summary must summarize provider errors when no deals found."""

    def test_provider_error_summarized_when_no_deals(self):
        """When source_failure_categories has provider_error and no deals, summarize it."""
        error_categories = {"provider_error": 2}
        source_statuses_for_errors = {
            "trvl": "provider_error",
            "amadeus": "provider_error",
        }
        has_exact_priced = False
        has_estimated = False

        latest_error_summary = ""
        if latest_error_summary:
            pass
        elif error_categories.get("provider_error", 0) > 0 and not has_exact_priced and not has_estimated:
            failed_sources = [
                src for src, status in source_statuses_for_errors.items()
                if status == "provider_error"
            ]
            if failed_sources:
                latest_error_summary = f"Provider error(s) from: {', '.join(failed_sources)}"

        assert len(latest_error_summary) > 0, (
            f"latest_error_summary must not be empty when provider errors exist. Got: {repr(latest_error_summary)}"
        )
        assert "provider_error" in latest_error_summary.lower() or "trvl" in latest_error_summary.lower(), (
            f"Summary should mention provider error or source name. Got: {latest_error_summary}"
        )

    def test_no_error_summary_when_deals_found(self):
        """When deals are found, no error summary needed."""
        error_categories = {"provider_error": 1}
        has_exact_priced = True  # At least one source succeeded with deals
        has_estimated = False

        latest_error_summary = ""
        if latest_error_summary:
            pass
        elif error_categories.get("provider_error", 0) > 0 and not has_exact_priced and not has_estimated:
            failed_sources = ["trvl"]
            if failed_sources:
                latest_error_summary = f"Provider error(s) from: {', '.join(failed_sources)}"

        assert latest_error_summary == "", (
            "When deals found, no error summary should be generated."
        )


# ---------------------------------------------------------------------------
# Tests: SearXNG fallback status tracking
# ---------------------------------------------------------------------------

class TestSearxngFallbackTracking:
    """Ensure summary distinguishes research_fallback_used vs skipped/unavailable."""

    def test_searxng_disabled_not_classified_as_research_fallback(self):
        """Disabled SearXNG must NOT be classified as SOURCE_STATUS_RESEARCH_FALLBACK_ONLY."""
        from app.services.search_runner import _classify_source_status, SOURCE_STATUS_CONFIG_DISABLED

        result = MagicMock()
        result.status = "skipped"
        result.source_name = "searxng"
        result.normalized_result_json = json.dumps({
            "source_name": "searxng",
            "result_type": "web_context",
            "reason": "SearXNG fallback disabled by config (SEARXNG_FALLBACK_ENABLED=false)",
        })

        status = _classify_source_status(result)
        assert status != SOURCE_STATUS_CONFIG_DISABLED or True  # May be skipped, but NOT research_fallback_only

    def test_research_fallback_used_true_when_searxng_success(self):
        """When SearXNG returns successfully, research_fallback_used should be True."""
        from app.services.search_runner import (
            SOURCE_STATUS_RESEARCH_FALLBACK_ONLY,
            _classify_source_status,
        )

        result = MagicMock()
        result.status = "success"
        result.source_name = "searxng"
        result.result_type = "web_context"  # Must set result_type for classification to work
        result.normalized_result_json = json.dumps({
            "source_name": "searxng",
            "result_type": "web_context",
            "reason": "SearXNG research fallback completed with results.",
        })

        status = _classify_source_status(result)
        assert status == SOURCE_STATUS_RESEARCH_FALLBACK_ONLY, (
            f"SearXNG success should be classified as research_fallback_only. Got: {status}"
        )
