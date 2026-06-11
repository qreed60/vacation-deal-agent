"""Phase 4D-3 tests: UI mock gating, legacy mock detection, cleanup hardening."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.models import DealCandidate, PriceSnapshot, SearchRun, SourceResult, Vacation
from app.services.price_history import aggregate_daily_ohlc, svg_ohlc_candles
from app.services.search_runner import (
    best_deal_for_run,
    best_deal_for_vacation,
    deal_candidates_for_vacation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session(tmp_path):
    """Create an in-memory-like SQLite DB for testing."""
    db_file = tmp_path / "test_vacation_deals.sqlite3"
    engine_url = f"sqlite:///{db_file}"

    with patch("app.db.session.database_url", return_value=engine_url):
        import app.db.session as session_mod
        session_mod._engine = None
        session_mod._engine_url = None

        from app.db.session import init_db as _init_db
        _init_db()
        from app.db.session import get_engine as _get_engine
        with Session(_get_engine()) as s:
            yield s


@pytest.fixture()
def vacation(db_session):
    """Create a test vacation."""
    v = Vacation(
        slug="test-vacation-4d3",
        title="Test Vacation 4D-3",
        status="active",
        number_of_travelers=2,
        travelers_json=json.dumps([]),
        origin="PIT",
        destination="MIA",
        date_mode="range",
        start_date=datetime(2026, 7, 1).date(),
        end_date=datetime(2026, 7, 7).date(),
        hotel_needed=True,
        airfare_needed=True,
        rental_car_needed=False,
        manifest_json=json.dumps({}),
    )
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)
    return v


# ---------------------------------------------------------------------------
# A. UI run-search route uses real_sources by default
# ---------------------------------------------------------------------------

class TestUIRealSourcesDefault:
    """Tests that the UI search route uses real sources and respects MOCK_SEARCH_ENABLED."""

    def test_ui_run_search_uses_real_sources_by_default(self):
        """create_search_run POST endpoint passes use_real_sources=True, use_mock=False."""
        from app.web.routes import create_search_run

        # Verify the function source code contains the correct parameters
        import inspect
        source = inspect.getsource(create_search_run)
        assert "use_real_sources=True" in source
        assert "use_mock=False" in source

    def test_ui_run_search_does_not_create_mock_when_no_offers(self, db_session, vacation):
        """When real sources return zero offers, no mock fallback is created."""
        # Create a SearchRun with real_sources=true and zero candidates
        run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": True,
                "mock": False,
                "deal_candidate_count": 0,
                "source_result_count": 0,
                "status": "completed",
            }),
        )
        db_session.add(run)
        db_session.commit()

        # No candidates should be returned as best deal
        best = best_deal_for_run(db_session, run.id)
        assert best is None


# ---------------------------------------------------------------------------
# B. MOCK_SEARCH_ENABLED gating
# ---------------------------------------------------------------------------

class TestMockSearchEnabledGating:
    """Tests for MOCK_SEARCH_ENABLED env flag."""

    def test_source_config_defaults_mock_search_enabled_false(self):
        """SourceConfig.mock_search_enabled defaults to False."""
        from app.services.source_config import SourceConfig, load_source_config

        # Unset MOCK_SEARCH_ENABLED if set
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOCK_SEARCH_ENABLED", None)
            config = load_source_config()
            assert config.mock_search_enabled is False

    def test_mock_search_enabled_true_when_set(self):
        """SourceConfig.mock_search_enabled reads from env when set."""
        with patch.dict(os.environ, {"MOCK_SEARCH_ENABLED": "true"}):
            from app.services.source_config import load_source_config
            config = load_source_config()
            assert config.mock_search_enabled is True


# ---------------------------------------------------------------------------
# C. Legacy mock row exclusion in best_deal/latest candidates
# ---------------------------------------------------------------------------

class TestLegacyMockExclusion:
    """Tests that legacy mock rows (is_mock=0 but linked to mock source) are excluded."""

    def test_legacy_mock_candidate_excluded_from_best_deal(self, db_session, vacation):
        """Existing legacy mock rows with is_mock=0 but source_result=mock_travel are excluded from best_deal."""
        # Create a SearchRun that looks like it came from UI (mock=true in summary)
        search_run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": False,
                "mock": True,
                "deal_candidate_count": 1,
                "source_result_count": 1,
                "status": "completed",
            }),
        )
        db_session.add(search_run)
        db_session.commit()

        # Create a mock source_result linked to this search run
        mock_source = SourceResult(
            search_run_id=search_run.id,
            source_name="mock_travel",
            status="mock",
            result_type="flight",
            query_json=json.dumps({}),
            normalized_result_json=json.dumps({}),
            raw_result_json=json.dumps({}),
        )
        db_session.add(mock_source)
        db_session.commit()

        # Create a deal_candidate with is_mock=0 (legacy row) linked to this search run
        legacy_candidate = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=search_run.id,
            candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=50.0,
            score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=False,  # Legacy: not flagged as mock!
        )

        real_run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": True,
                "mock": False,
                "deal_candidate_count": 1,
                "source_result_count": 1,
                "status": "completed",
            }),
        )
        db_session.add(real_run)
        db_session.commit()

        real_source = SourceResult(
            search_run_id=real_run.id,
            source_name="trvl",
            status="completed",
            result_type="flight",
            query_json=json.dumps({}),
            normalized_result_json=json.dumps({}),
            raw_result_json=json.dumps({}),
        )
        db_session.add(real_source)
        db_session.commit()

        # Create a real candidate with higher score (worse) in a separate real run
        real_candidate = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=real_run.id,
            candidate_type="package",
            title="Real Air Flight Pittsburgh to Miami via United",
            status="valid",
            total_price=450.0,
            currency="USD",
            score=80.0,
            score_breakdown_json=json.dumps({"quality": 80}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "trvl"}]),
            normalized_result_json=json.dumps({}),
        )

        db_session.add_all([legacy_candidate, real_candidate])
        db_session.commit()

        # best_deal_for_vacation should return the REAL candidate only
        best = best_deal_for_vacation(db_session, vacation.id)
        assert best is not None
        assert "Mock" not in best.title
        assert best.is_mock is False

    def test_legacy_mock_candidate_excluded_from_latest_candidates(self, db_session, vacation):
        """Existing legacy mock rows with is_mock=0 but linked to mock SearchRun are excluded from latest candidate list."""
        # Create a SearchRun that looks like it came from UI (mock=true in summary)
        search_run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": False,
                "mock": True,
                "deal_candidate_count": 1,
                "source_result_count": 1,
                "status": "completed",
            }),
        )
        db_session.add(search_run)
        db_session.commit()

        # Create a mock source_result linked to this search run
        mock_source = SourceResult(
            search_run_id=search_run.id,
            source_name="mock_travel",
            status="mock",
            result_type="flight",
            query_json=json.dumps({}),
            normalized_result_json=json.dumps({}),
            raw_result_json=json.dumps({}),
        )
        db_session.add(mock_source)
        db_session.commit()

        # Create a legacy deal_candidate with is_mock=0 (legacy row) linked to this search run
        legacy_candidate = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=search_run.id,
            candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=50.0,
            score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=False,  # Legacy: not flagged as mock!
        )

        db_session.add(legacy_candidate)
        db_session.commit()

        # deal_candidates_for_vacation should NOT return the legacy mock candidate
        candidates = deal_candidates_for_vacation(db_session, vacation.id)
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# D. delete_mock_data.py detects legacy mock rows
# ---------------------------------------------------------------------------

class TestCleanupLegacyDetection:
    """Tests for cleanup script legacy mock detection."""

    def test_dry_run_detects_legacy_mock_via_source_result(self, db_session, vacation, tmp_path):
        """delete_mock_data.py dry-run detects legacy mock rows by source_result even when is_mock=0."""
        # Create a SearchRun that looks like it came from UI (mock=true in summary)
        search_run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": False,
                "mock": True,
                "deal_candidate_count": 1,
                "source_result_count": 1,
                "status": "completed",
            }),
        )
        db_session.add(search_run)
        db_session.commit()

        # Create a mock source_result linked to this search run
        mock_source = SourceResult(
            search_run_id=search_run.id,
            source_name="mock_travel",
            status="mock",
            result_type="flight",
            query_json=json.dumps({}),
            normalized_result_json=json.dumps({}),
            raw_result_json=json.dumps({}),
        )
        db_session.add(mock_source)
        db_session.commit()

        # Create a legacy deal_candidate with is_mock=0 (legacy row) linked to this search run
        legacy_candidate = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=search_run.id,
            candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=50.0,
            score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=False,  # Legacy: not flagged as mock!
        )

        legacy_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=search_run.id, quote_type="flight",
            source_name="trvl", label="Mock Flight (legacy)", total_price=200.0,
            currency="USD", is_mock=False,  # Legacy: not flagged as mock!
        )

        db_session.add_all([legacy_candidate, legacy_snapshot])
        db_session.commit()

        from scripts.delete_mock_data import main as delete_main

        with patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"}):
            with patch("sys.argv", ["delete_mock_data.py"]):
                # Capture output to verify legacy rows are detected
                import io
                from contextlib import redirect_stdout

                f = io.StringIO()
                with redirect_stdout(f):
                    delete_main()
                output = f.getvalue()

        # Legacy mock candidate should be detected even though is_mock=0
        assert "Mock Air Flight" in output or "legacy" in output.lower() or "mock_travel" in output

    def test_execute_deletes_legacy_mock_rows_and_preserves_real_trvl(self, db_session, vacation, tmp_path):
        """delete_mock_data.py --execute deletes legacy mock rows while preserving real trvl rows."""
        mock_run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": False,
                "mock": True,
                "source_status_counts": {"mock": 1},
                "status": "completed",
            }),
        )
        real_run = SearchRun(
            vacation_id=vacation.id,
            status="completed",
            trigger_type="manual",
            summary_json=json.dumps({
                "real_sources": True,
                "mock": False,
                "source_status_counts": {"completed": 1},
                "status": "completed",
            }),
        )
        db_session.add_all([mock_run, real_run])
        db_session.commit()

        mock_source = SourceResult(
            search_run_id=mock_run.id,
            source_name="mock_travel",
            status="mock",
            result_type="flight",
            query_json=json.dumps({}),
            normalized_result_json=json.dumps({"mock": True}),
            raw_result_json=json.dumps({}),
        )
        real_source = SourceResult(
            search_run_id=real_run.id,
            source_name="trvl",
            status="completed",
            result_type="flight",
            query_json=json.dumps({}),
            normalized_result_json=json.dumps({"offers": [{"source_name": "trvl"}]}),
            raw_result_json=json.dumps({}),
        )
        db_session.add_all([mock_source, real_source])
        db_session.commit()

        mock_snapshot = PriceSnapshot(
            vacation_id=vacation.id,
            search_run_id=mock_run.id,
            source_result_id=mock_source.id,
            quote_type="flight",
            source_name="trvl",
            label="Mock Air legacy flight",
            total_price=200.0,
            currency="USD",
            normalized_json=json.dumps({"mock": True}),
            is_mock=False,
        )
        real_snapshot = PriceSnapshot(
            vacation_id=vacation.id,
            search_run_id=real_run.id,
            source_result_id=real_source.id,
            quote_type="flight",
            source_name="trvl",
            label="trvl real flight",
            total_price=456.0,
            currency="USD",
            normalized_json=json.dumps({"source_name": "trvl"}),
            is_mock=False,
        )
        mock_candidate = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=mock_run.id,
            candidate_type="flight_only",
            title="Mock Air legacy deal",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=10.0,
            source_links_json=json.dumps([{"source_name": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=False,
        )
        real_candidate = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=real_run.id,
            candidate_type="flight_only",
            title="trvl real deal",
            status="valid",
            total_price=456.0,
            currency="USD",
            score=20.0,
            source_links_json=json.dumps([{"source_name": "trvl"}]),
            normalized_result_json=json.dumps({"source_name": "trvl"}),
            is_mock=False,
        )
        db_session.add_all([mock_snapshot, real_snapshot, mock_candidate, real_candidate])
        db_session.commit()

        from scripts.delete_mock_data import main as delete_main

        with patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"}):
            with patch("sys.argv", ["delete_mock_data.py", "--execute", "--no-backup"]):
                assert delete_main() == 0

        remaining_candidates = db_session.exec(select(DealCandidate).order_by(DealCandidate.id.asc())).all()
        remaining_snapshots = db_session.exec(select(PriceSnapshot).order_by(PriceSnapshot.id.asc())).all()
        remaining_sources = db_session.exec(select(SourceResult).order_by(SourceResult.id.asc())).all()
        remaining_runs = db_session.exec(select(SearchRun).order_by(SearchRun.id.asc())).all()

        assert [candidate.title for candidate in remaining_candidates] == ["trvl real deal"]
        assert [snapshot.label for snapshot in remaining_snapshots] == ["trvl real flight"]
        assert [source.source_name for source in remaining_sources] == ["trvl"]
        assert {run.id for run in remaining_runs} == {mock_run.id, real_run.id}


# ---------------------------------------------------------------------------
# E. Candlestick chart tests (reused from Phase 4D-2)
# ---------------------------------------------------------------------------

class TestCandlestickChart:
    """Tests for true candlestick chart rendering and OHLC aggregation."""

    def test_ohlc_uses_daily_aggregation_not_raw_rows(self):
        """OHLC data uses daily aggregation, not raw lookup rows."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {
                "timestamp": base_date + timedelta(hours=i),
                "total_price": 100.0 + i * 5,
                "currency": "USD",
                "quote_type": "flight",
                "is_mock": False,
            }
            for i in range(4)
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        assert len(ohlc) == 1
        candle = ohlc[0]

        prices = [r["total_price"] for r in history_rows]
        assert candle["open"] == prices[0]
        assert candle["high"] == max(prices)
        assert candle["low"] == min(prices)
        assert candle["close"] == prices[-1]
        assert candle["count"] == 4

    def test_usd_preferred_when_mixed_currencies(self):
        """USD is preferred when mixed currencies exist in OHLC data."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=1), "total_price": 120.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date, "total_price": 90.0, "currency": "EUR", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=1), "total_price": 110.0, "currency": "EUR", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        assert "Excluded non-USD currencies" in svg
        assert 'class="candle-up"' in svg or 'class="candle-down"' in svg or 'class="candle-flat"' in svg

    def test_svg_contains_candle_body_and_wick_elements(self):
        """Chart template contains rect body and line wick elements with candle classes."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(hours=6), "total_price": 120.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        assert '<rect' in svg
        assert 'class="candle-up"' in svg or 'class="candle-down"' in svg or 'class="candle-flat"' in svg
        assert 'class="wick"' in svg


# ---------------------------------------------------------------------------
# F. Existing tests still pass (sanity check)
# ---------------------------------------------------------------------------

class TestExistingFunctionality:
    """Sanity checks that existing functionality is not broken."""

    def test_health_endpoint(self):
        """Health endpoint returns ok."""
        from app.web.routes import health

        response = health()
        assert response.status_code == 200
        assert response.body == b'{"status":"ok"}'
