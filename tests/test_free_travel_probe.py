import json
import subprocess
import sys

from app.adapters import free_travel_probe
from app.adapters.free_travel_probe import (
    CandidateProbeResult,
    ProbeRequest,
    normalize_candidate_result,
    probe_candidate,
    run_probe,
)


def test_probe_cli_help_works():
    result = subprocess.run(
        [sys.executable, "scripts/probe_free_travel_sources.py", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--candidate" in result.stdout
    assert "--all" in result.stdout


def test_missing_candidate_returns_missing_dependency_not_crash(monkeypatch):
    monkeypatch.setattr(free_travel_probe, "_module_exists", lambda name: False)
    monkeypatch.setattr(free_travel_probe.shutil, "which", lambda name: None)

    result = probe_candidate(ProbeRequest(candidate="fast-flights", origin="PIT", destination="MOT"))[0]

    assert result.status == "missing_dependency"
    assert result.install_hint


def test_normalized_flight_result_with_provider_price_is_usable():
    result = normalize_candidate_result(
        "fast-flights",
        {
            "flights": [
                {
                    "airline": "Delta Air Lines",
                    "carrier_code": "DL",
                    "total_price": "425.00",
                    "currency": "USD",
                    "departure": "2026-09-18T06:00:00",
                    "arrival": "2026-09-18T12:00:00",
                }
            ]
        },
        "flight",
        "PIT to MOT",
    )

    assert result.status == "usable"
    assert result.provider == "Delta Air Lines"
    assert result.provider_code == "DL"
    assert result.total_price == 425.0
    assert result.currency == "USD"


def test_normalized_hotel_result_with_provider_price_is_usable():
    result = normalize_candidate_result(
        "trvl",
        {"hotels": [{"hotel_name": "Mainstay Suites", "price": 510, "currency": "USD"}]},
        "hotel",
        "Minot, ND",
    )

    assert result.status == "usable"
    assert result.provider == "Mainstay Suites"
    assert result.total_price == 510.0
    assert result.component_type == "hotel"


def test_text_only_result_is_not_usable_for_pricing():
    result = normalize_candidate_result("fli", "Delta from PIT to MOT is usually cheap", "flight", "PIT to MOT")

    assert result.status == "not_usable_for_pricing"
    assert result.raw_result_available is True
    assert "text" in result.notes[0].lower()


def test_report_file_is_written_under_free_source_probe_dir(tmp_path, monkeypatch):
    def fake_probe(request):
        return [
            CandidateProbeResult(
                candidate=request.candidate,
                status="missing_dependency",
                component_type="flight",
                install_hint="install locally",
            )
        ]

    monkeypatch.setattr(free_travel_probe, "probe_candidate", fake_probe)

    report = run_probe([ProbeRequest(candidate="fast-flights")], report_dir=tmp_path / "data/free_source_probes")

    report_path = tmp_path / "data/free_source_probes" / report["report_path"].split("/")[-1]
    assert report_path.exists()
    assert json.loads(report_path.read_text())["results"][0]["status"] == "missing_dependency"
    assert "data/free_source_probes" in report["report_path"]


def test_one_failed_candidate_does_not_fail_all(tmp_path, monkeypatch):
    def fake_probe(request):
        if request.candidate == "fli":
            raise RuntimeError("candidate exploded")
        return [CandidateProbeResult(candidate=request.candidate, status="missing_dependency", component_type="flight")]

    monkeypatch.setattr(free_travel_probe, "probe_candidate", fake_probe)

    report = run_probe(
        [ProbeRequest(candidate="fli"), ProbeRequest(candidate="fast-flights")],
        report_dir=tmp_path / "data/free_source_probes",
    )

    statuses = {result["candidate"]: result["status"] for result in report["results"]}
    assert statuses["fli"] == "failed"
    assert statuses["fast-flights"] == "missing_dependency"
