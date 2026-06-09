from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CANDIDATES = (
    "fast-flights",
    "fli",
    "trvl",
    "flights-skill",
    "travel-hacking-toolkit",
)

REPORT_DIR = Path("data/free_source_probes")
SECRET_NAMES = ("KEY", "SECRET", "TOKEN", "PASSWORD")


@dataclass(frozen=True)
class ProbeRequest:
    candidate: str
    origin: str | None = None
    destination: str | None = None
    depart: str | None = None
    return_date: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    adults: int = 1
    children: int = 0


@dataclass
class CandidateProbeResult:
    candidate: str
    status: str
    component_type: str
    provider: str | None = None
    provider_code: str | None = None
    label: str | None = None
    total_price: float | None = None
    currency: str | None = None
    departure: str | None = None
    arrival: str | None = None
    source_url: str | None = None
    search_reference_url: str | None = None
    link_type: str = "none"
    link_label: str | None = None
    raw_result_available: bool = False
    notes: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    install_hint: str | None = None
    raw_result: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _secret_values() -> list[str]:
    values: list[str] = []
    for name, value in os.environ.items():
        if value and len(value) >= 6 and any(marker in name.upper() for marker in SECRET_NAMES):
            values.append(value)
    return values


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: scrub_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    if not isinstance(value, str):
        return value
    scrubbed = value
    for secret in _secret_values():
        scrubbed = scrubbed.replace(secret, "[redacted]")
    return scrubbed


def _module_exists(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _load_json_text(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _price_value(payload: dict[str, Any]) -> float | None:
    for key in (
        "total_price",
        "price",
        "price_raw",
        "amount",
        "total",
        "fare",
        "cost",
        "extracted_price",
        "extracted_lowest",
    ):
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            nested = _price_value(value)
            if nested is not None:
                return nested
            continue
        try:
            return float(str(value).replace("$", "").replace(",", "").strip())
        except ValueError:
            continue
    return None


def _currency(payload: dict[str, Any]) -> str | None:
    currency = _first_string(payload, ("currency", "currency_code"))
    if currency:
        return currency.upper()
    price = payload.get("price")
    if isinstance(price, str) and "$" in price:
        return "USD"
    return None


def _source_url(payload: dict[str, Any]) -> str | None:
    value = _first_string(payload, ("source_url", "url", "link", "booking_url", "deep_link"))
    if value and value.startswith(("http://", "https://")):
        return value
    return None


def _concise_error(value: str, limit: int = 1200) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}... [truncated]"


def _provider_value(payload: dict[str, Any]) -> str | None:
    for key in (
        "provider",
        "airline",
        "airline_name",
        "airlines",
        "carrier",
        "name",
        "hotel_name",
        "title",
    ):
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, list):
            labels = [str(item) for item in value if item]
            if labels:
                return ", ".join(labels)
            continue
        return str(value)
    return None


def _iter_dicts(raw: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        found.append(raw)
        for value in raw.values():
            if isinstance(value, (dict, list)):
                found.extend(_iter_dicts(value))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, (dict, list)):
                found.extend(_iter_dicts(item))
    return found


def normalize_candidate_result(
    candidate: str,
    raw: Any,
    component_type: str,
    label: str | None = None,
    *,
    stdout: str = "",
    stderr: str = "",
) -> CandidateProbeResult:
    raw_available = raw not in (None, "", [], {})
    dicts = _iter_dicts(raw)
    notes: list[str] = []
    if raw_available and not dicts:
        return CandidateProbeResult(
            candidate=candidate,
            status="not_usable_for_pricing",
            component_type=component_type,
            label=label,
            raw_result_available=True,
            raw_result=scrub_secrets(raw),
            notes=["Candidate returned text or non-JSON data without reliable structured price fields."],
            stdout=scrub_secrets(stdout),
            stderr=scrub_secrets(stderr),
        )

    for payload in dicts:
        provider = _provider_value(payload)
        price = _price_value(payload)
        if provider and price is not None:
            provider_code = _first_string(payload, ("provider_code", "airline_code", "carrier_code", "code"))
            source_url = _source_url(payload)
            result_label = label or _first_string(payload, ("label", "route", "itinerary_summary", "title", "name"))
            return CandidateProbeResult(
                candidate=candidate,
                status="usable",
                component_type=component_type,
                provider=provider,
                provider_code=provider_code,
                label=result_label,
                total_price=price,
                currency=_currency(payload) or "USD",
                departure=_first_string(payload, ("departure", "depart", "departure_time", "check_in")),
                arrival=_first_string(payload, ("arrival", "arrival_time", "check_out")),
                source_url=source_url,
                search_reference_url=None,
                link_type="exact_source" if source_url else "none",
                link_label="View source price" if source_url else None,
                raw_result_available=raw_available,
                raw_result=scrub_secrets(raw),
                notes=notes,
                stdout=scrub_secrets(stdout),
                stderr=scrub_secrets(stderr),
            )
        if price is not None and not provider:
            notes.append("Structured price was present, but no provider field was found.")
        if provider and price is None:
            notes.append("Structured provider was present, but no reliable price field was found.")

    return CandidateProbeResult(
        candidate=candidate,
        status="not_usable_for_pricing" if raw_available else "failed",
        component_type=component_type,
        label=label,
        raw_result_available=raw_available,
        raw_result=scrub_secrets(raw) if raw_available else None,
        notes=notes or ["No structured provider and total price pair was found."],
        stdout=scrub_secrets(stdout),
        stderr=scrub_secrets(stderr),
    )


def missing_dependency(candidate: str, component_type: str, install_hint: str) -> CandidateProbeResult:
    return CandidateProbeResult(
        candidate=candidate,
        status="missing_dependency",
        component_type=component_type,
        install_hint=install_hint,
        notes=["Dependency was not found locally. The probe does not install packages or binaries."],
    )


def _failed(
    candidate: str,
    component_type: str,
    exc: Exception,
    stdout: str = "",
    stderr: str = "",
    notes: list[str] | None = None,
) -> CandidateProbeResult:
    return CandidateProbeResult(
        candidate=candidate,
        status="failed",
        component_type=component_type,
        raw_result_available=False,
        notes=notes or [],
        error=scrub_secrets(_concise_error(str(exc))),
        stdout=scrub_secrets(stdout),
        stderr=scrub_secrets(stderr),
    )


def _unsupported(candidate: str, component_type: str, status: str, note: str, stdout: str = "", stderr: str = "") -> CandidateProbeResult:
    return CandidateProbeResult(
        candidate=candidate,
        status=status,
        component_type=component_type,
        notes=[note],
        stdout=scrub_secrets(stdout),
        stderr=scrub_secrets(stderr),
    )


def _run_command(command: list[str], timeout_seconds: float = 30.0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout_seconds, check=False)


def probe_fast_flights(request: ProbeRequest) -> CandidateProbeResult:
    if not _module_exists("fast_flights"):
        return missing_dependency("fast-flights", "flight", "Install the optional fast-flights package in the active environment.")
    stdout = io.StringIO()
    stderr = io.StringIO()
    notes = ["api_style=v2", "fetch_mode=common"]
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            module = importlib.import_module("fast_flights")
            if all(hasattr(module, name) for name in ("FlightData", "Passengers", "get_flights")):
                signature = inspect.signature(module.get_flights)
                if not {"flight_data", "trip", "passengers", "seat"}.issubset(signature.parameters):
                    return _unsupported(
                        "fast-flights",
                        "flight",
                        "unsupported_api_shape",
                        "fast_flights is installed, but get_flights does not match the supported v2 keyword API.",
                        stdout.getvalue(),
                        stderr.getvalue(),
                    )
                flight_data = [
                    module.FlightData(date=request.depart, from_airport=request.origin, to_airport=request.destination),
                ]
                if request.return_date:
                    flight_data.append(
                        module.FlightData(date=request.return_date, from_airport=request.destination, to_airport=request.origin)
                    )
                passengers = module.Passengers(adults=request.adults, children=request.children)
                raw = module.get_flights(
                    flight_data=flight_data,
                    trip="round-trip" if request.return_date else "one-way",
                    passengers=passengers,
                    seat="economy",
                    fetch_mode="common",
                    max_stops=None,
                )
            elif hasattr(module, "search"):
                notes = ["api_style=search"]
                raw = module.search(
                    origin=request.origin,
                    destination=request.destination,
                    depart=request.depart,
                    return_date=request.return_date,
                    adults=request.adults,
                    children=request.children,
                )
            else:
                return _unsupported(
                    "fast-flights",
                    "flight",
                    "available",
                    "fast_flights is installed, but no recognized search API was found.",
                    stdout.getvalue(),
                    stderr.getvalue(),
                )
    except Exception as exc:
        return _failed("fast-flights", "flight", exc, stdout.getvalue(), stderr.getvalue(), notes)
    result = normalize_candidate_result(
        "fast-flights",
        _object_to_data(raw),
        "flight",
        f"{request.origin} to {request.destination}",
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )
    result.notes = notes + result.notes
    return result


def _object_to_data(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [_object_to_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _object_to_data(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return _object_to_data(asdict(value))
    if hasattr(value, "model_dump"):
        return _object_to_data(value.model_dump())
    if hasattr(value, "dict"):
        return _object_to_data(value.dict())
    if hasattr(value, "__dict__"):
        data = {key: _object_to_data(item) for key, item in vars(value).items() if not key.startswith("_")}
        data.setdefault("raw_repr", repr(value))
        return data
    return str(value)


def probe_fli(request: ProbeRequest) -> CandidateProbeResult:
    if _module_exists("fli"):
        try:
            module = importlib.import_module("fli")
            if hasattr(module, "search"):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    raw = module.search(
                        origin=request.origin,
                        destination=request.destination,
                        depart=request.depart,
                        return_date=request.return_date,
                        adults=request.adults,
                        children=request.children,
                    )
                return normalize_candidate_result("fli", _object_to_data(raw), "flight", f"{request.origin} to {request.destination}", stdout=stdout.getvalue(), stderr=stderr.getvalue())
            return _unsupported("fli", "flight", "available", "fli module is installed, but no recognized search API was found.")
        except Exception as exc:
            return _failed("fli", "flight", exc)

    command = shutil.which("fli")
    if not command:
        return missing_dependency("fli", "flight", "Install a local fli CLI or Python module before probing.")
    try:
        result = _run_command([command, "--help"], timeout_seconds=10)
    except Exception as exc:
        return _failed("fli", "flight", exc)
    help_text = f"{result.stdout}\n{result.stderr}".lower()
    if "api key" in help_text or "token" in help_text:
        return _unsupported("fli", "flight", "unsupported_for_free_source_goal", "fli help output appears to require an API key.", result.stdout, result.stderr)
    if "playwright" in help_text or "selenium" in help_text or "browser" in help_text:
        return _unsupported("fli", "flight", "unsupported_for_current_phase", "fli help output appears to require browser automation.", result.stdout, result.stderr)
    return _unsupported("fli", "flight", "available", "fli CLI is installed, but this probe does not know a documented JSON search command from local help output.", result.stdout, result.stderr)


def probe_trvl(request: ProbeRequest) -> list[CandidateProbeResult]:
    command = shutil.which("trvl")
    if not command:
        return [missing_dependency("trvl", "flight", "Install a local trvl command before probing.")]
    try:
        result = _run_command([command, "--help"], timeout_seconds=10)
    except Exception as exc:
        return [_failed("trvl", "flight", exc)]
    help_text = f"{result.stdout}\n{result.stderr}".lower()
    if "api key" in help_text or "token" in help_text:
        return [_unsupported("trvl", "flight", "unsupported_for_free_source_goal", "trvl help output appears to require an API key.", result.stdout, result.stderr)]
    if "playwright" in help_text or "selenium" in help_text or "browser" in help_text:
        return [_unsupported("trvl", "flight", "unsupported_for_current_phase", "trvl help output appears to require browser automation.", result.stdout, result.stderr)]

    results: list[CandidateProbeResult] = []
    if "flight" in help_text:
        results.append(_probe_trvl_mode(command, request, "flight"))
    if "hotel" in help_text:
        results.append(_probe_trvl_mode(command, request, "hotel"))
    if not results:
        results.append(_unsupported("trvl", "flight", "available", "trvl CLI is installed, but local help output does not document flight or hotel JSON query modes.", result.stdout, result.stderr))
    return results


def _probe_trvl_mode(command: str, request: ProbeRequest, component_type: str) -> CandidateProbeResult:
    if component_type == "hotel":
        args = [
            command,
            "hotel",
            "search",
            "--destination",
            request.destination or "",
            "--check-in",
            request.check_in or request.depart or "",
            "--check-out",
            request.check_out or request.return_date or "",
            "--adults",
            str(request.adults),
            "--children",
            str(request.children),
            "--json",
        ]
    else:
        args = [
            command,
            "flight",
            "search",
            "--origin",
            request.origin or "",
            "--destination",
            request.destination or "",
            "--depart",
            request.depart or "",
            "--return",
            request.return_date or "",
            "--adults",
            str(request.adults),
            "--children",
            str(request.children),
            "--json",
        ]
    try:
        result = _run_command([arg for arg in args if arg != ""], timeout_seconds=30)
    except Exception as exc:
        return _failed("trvl", component_type, exc)
    raw = _load_json_text(result.stdout)
    if result.returncode != 0 and raw is None:
        return _failed("trvl", component_type, RuntimeError(f"trvl exited with code {result.returncode}"), result.stdout, result.stderr)
    label = request.destination if component_type == "hotel" else f"{request.origin} to {request.destination}"
    return normalize_candidate_result("trvl", raw if raw is not None else result.stdout, component_type, label, stdout=result.stdout, stderr=result.stderr)


def probe_configured_json_command(candidate: str, request: ProbeRequest, env_name: str) -> CandidateProbeResult:
    command_value = os.environ.get(env_name, "").strip()
    component_type = "hotel" if request.check_in or request.check_out else "flight"
    if not command_value:
        return missing_dependency(candidate, component_type, f"Set {env_name} to a local read-only JSON command for this candidate.")
    command = shlex.split(command_value)
    if not command:
        return missing_dependency(candidate, component_type, f"Set {env_name} to a non-empty local read-only JSON command.")
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        return missing_dependency(candidate, component_type, f"{command[0]} was not found locally.")
    query = {
        "origin": request.origin,
        "destination": request.destination,
        "depart": request.depart,
        "return": request.return_date,
        "check_in": request.check_in,
        "check_out": request.check_out,
        "adults": request.adults,
        "children": request.children,
    }
    try:
        result = subprocess.run(
            command,
            input=json.dumps(query),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return _failed(candidate, component_type, exc)
    raw = _load_json_text(result.stdout)
    if result.returncode != 0 and raw is None:
        return _failed(candidate, component_type, RuntimeError(f"{candidate} command exited with code {result.returncode}"), result.stdout, result.stderr)
    label = request.destination if component_type == "hotel" else f"{request.origin} to {request.destination}"
    return normalize_candidate_result(candidate, raw if raw is not None else result.stdout, component_type, label, stdout=result.stdout, stderr=result.stderr)


def probe_candidate(request: ProbeRequest) -> list[CandidateProbeResult]:
    candidate = request.candidate
    if candidate == "fast-flights":
        return [probe_fast_flights(request)]
    if candidate == "fli":
        return [probe_fli(request)]
    if candidate == "trvl":
        return probe_trvl(request)
    if candidate == "flights-skill":
        return [probe_configured_json_command(candidate, request, "FREE_TRAVEL_PROBE_FLIGHTS_SKILL_COMMAND")]
    if candidate == "travel-hacking-toolkit":
        return [probe_configured_json_command(candidate, request, "FREE_TRAVEL_PROBE_TRAVEL_HACKING_TOOLKIT_COMMAND")]
    return [CandidateProbeResult(candidate=candidate, status="unsupported", component_type="unknown", notes=["Unknown candidate."])]


def run_probe(requests: list[ProbeRequest], report_dir: Path = REPORT_DIR) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)
    for request in requests:
        start = time.monotonic()
        try:
            candidate_results = probe_candidate(request)
        except Exception as exc:
            candidate_results = [_failed(request.candidate, "unknown", exc)]
        elapsed = round(time.monotonic() - start, 3)
        for result in candidate_results:
            payload = result.to_dict()
            payload["elapsed_seconds"] = elapsed
            results.append(scrub_secrets(payload))

    report = {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"free_travel_probe_{_now_slug()}.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report
