from app.adapters import searxng


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    calls = []

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def get(self, url, params):
        self.calls.append((url, params))
        return FakeResponse(
            {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.test/result",
                        "content": "Snippet",
                        "engine": "test_engine",
                        "score": 1.5,
                    }
                ]
            }
        )


def test_searxng_search_normalizes_results(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(searxng.httpx, "Client", FakeClient)

    result = searxng.search("cheap flights", base_url="http://searxng.test", timeout_seconds=2.0)

    assert result["status"] == "completed"
    assert FakeClient.calls[0][0] == "http://searxng.test/search"
    assert FakeClient.calls[0][1]["q"] == "cheap flights"
    assert FakeClient.calls[0][1]["format"] == "json"
    normalized = result["normalized_result"]["results"][0]
    assert normalized["title"] == "Result"
    assert normalized["url"] == "https://example.test/result"
    assert normalized["source_url"] == "https://example.test/result"
    assert normalized["link_type"] == "search_reference"
    assert normalized["link_label"] == "Search reference"
    assert normalized["content"] == "Snippet"
    assert normalized["engine"] == "test_engine"


def test_searxng_empty_base_url_skips():
    result = searxng.search("anything", base_url="")

    assert result["status"] == "skipped"
    assert "SEARXNG_BASE_URL" in result["error_message"]
