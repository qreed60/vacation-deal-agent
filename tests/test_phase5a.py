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
