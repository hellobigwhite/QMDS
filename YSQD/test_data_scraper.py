import data_scraper


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_google_search_xcrawl_page_uses_pagination_next_start(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(
            {
                "organic_results": [
                    {"link": "https://example.com/a"},
                    {"link": "https://example.com/b"},
                ],
                "pagination": {
                    "next": "https://www.google.com/search?q=test&start=20",
                },
            }
        )

    monkeypatch.setattr(data_scraper.requests, "post", fake_post)

    page = data_scraper.google_search_xcrawl_page("test", "token", start=10, max_results_per_page=10)

    assert captured["url"] == data_scraper.XCRAWL_SERP_URL
    assert captured["json"]["start"] == 10
    assert captured["json"]["engine"] == "google_search"
    assert captured["json"]["location"] == "US"
    assert captured["json"]["hl"] == "en"
    assert "google_domain" not in captured["json"]
    assert page["urls"] == ["https://example.com/a", "https://example.com/b"]
    assert page["has_next"] is True
    assert page["next_start"] == 20


def test_google_search_xcrawl_page_stops_when_results_do_not_fill_page(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(
            {
                "organic_results": [
                    {"link": "https://example.com/a"},
                    {"link": "https://example.com/b"},
                    {"link": "https://example.com/c"},
                ]
            }
        )

    monkeypatch.setattr(data_scraper.requests, "post", fake_post)

    page = data_scraper.google_search_xcrawl_page("test", "token", start=0, max_results_per_page=10)

    assert page["has_next"] is False
    assert page["next_start"] is None
    assert page["raw_result_count"] == 3
