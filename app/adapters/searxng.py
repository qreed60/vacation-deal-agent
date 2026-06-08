from __future__ import annotations

from typing import Any

import httpx


def skipped_result(reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "normalized_result": {"source_name": "searxng", "result_type": "web_context", "reason": reason},
        "raw_result": {},
        "error_message": reason,
    }


def search(
    text_query: str,
    *,
    base_url: str,
    timeout_seconds: float = 5.0,
    categories: list[str] | None = None,
    engines: list[str] | None = None,
) -> dict[str, Any]:
    if not base_url:
        return skipped_result("SEARXNG_BASE_URL is empty")
    params: dict[str, Any] = {"q": text_query, "format": "json"}
    if categories:
        params["categories"] = ",".join(categories)
    if engines:
        params["engines"] = ",".join(engines)

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(f"{base_url.rstrip('/')}/search", params=params)
            response.raise_for_status()
            raw = response.json()
    except Exception as exc:
        return {
            "status": "error",
            "normalized_result": {"source_name": "searxng", "result_type": "web_context"},
            "raw_result": {},
            "error_message": str(exc),
        }

    normalized_results = []
    for item in raw.get("results", []):
        normalized_results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content") or item.get("snippet"),
                "engine": item.get("engine"),
                "source": item.get("source") or item.get("engine"),
                "score": item.get("score"),
            }
        )

    return {
        "status": "completed",
        "normalized_result": {
            "source_name": "searxng",
            "result_type": "web_context",
            "query": text_query,
            "results": normalized_results,
        },
        "raw_result": raw,
        "error_message": None,
    }
