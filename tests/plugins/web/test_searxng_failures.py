"""SearXNG engine outages must not look like successful empty searches."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import httpx
import pytest

from plugins.web.searxng.provider import SearXNGWebSearchProvider


_FAILURES = [
    ["brave", "too many requests"],
    ["duckduckgo", "CAPTCHA"],
    ["google cse", "too many requests"],
    ["startpage", "CAPTCHA"],
]
_ROWS = [
    {"title": "Low", "url": "https://low.example", "content": "L", "score": 1},
    {"title": "High", "url": "https://high.example", "content": "H", "score": 9},
]


@pytest.mark.parametrize(
    "rows,metadata,details",
    [
        ([], {"unresponsive_engines": _FAILURES},
         "brave: too many requests; duckduckgo: CAPTCHA; google cse: too many requests; startpage: CAPTCHA"),
        ([], {"unresponsive_engines": ["brave", "dd"]}, "brave; dd"),
        ([], {"unresponsive_engines": {"brave": "CAPTCHA", "dd": None}}, "brave: CAPTCHA; dd"),
        ([], {"unresponsive_engines": ["brave", ["dd", "CAPTCHA"], ["google", None]]},
         "brave; dd: CAPTCHA; google"),
        ([], {"unresponsive_engines": "brave"}, "brave"),
        ([], {"unresponsive_engines": [["brave"], [], 7]}, "['brave']; []; 7"),
        ([], {"unresponsive_engines": True}, "True"),
        ([], {"unresponsive_engines": []}, None),
        ([], {"unresponsive_engines": None}, None),
        ([], {"unresponsive_engines": {}}, None),
        ([], {}, None),
        (_ROWS, {"unresponsive_engines": _FAILURES}, None),
        (_ROWS, {"unresponsive_engines": ["brave", "dd"]}, None),
    ],
    ids=[
        "upstream-outage", "names", "mapping", "mixed", "string", "malformed-entries",
        "scalar", "empty", "null", "empty-mapping", "legacy-empty", "partial-success", "partial-names",
    ],
)
def test_engine_failure_contract(monkeypatch, rows, metadata, details):
    monkeypatch.setenv("SEARXNG_URL", "http://searxng.example")
    response = httpx.Response(
        200,
        json={"results": rows, **metadata},
        request=httpx.Request("GET", "http://searxng.example/search"),
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    result = SearXNGWebSearchProvider().search("synthetic query", limit=1)

    if not rows and metadata.get("unresponsive_engines"):
        assert result["success"] is False
        assert f"upstream engine failures ({details})" in result["error"]
        assert "synthetic query" not in result["error"]
        assert "http://searxng.example" not in result["error"]
        return

    assert result["success"] is True
    expected = [] if not rows else [
        {"title": "High", "url": "https://high.example", "description": "H", "position": 1}
    ]
    assert result["data"]["web"] == expected


@pytest.mark.parametrize(
    "failures,error_detail",
    [
        (_FAILURES, "CAPTCHA"),
        (["brave", "dd"], "brave; dd"),
        ({"brave": "CAPTCHA", "dd": None}, "brave: CAPTCHA; dd"),
    ],
    ids=["pairs", "names", "mapping"],
)
@pytest.mark.parametrize(
    "scenario",
    ["rescued", "disabled", "ring-failed", "empty", "partial"],
)
def test_dispatch_rescue_contract(monkeypatch, scenario, failures, error_detail):
    from hermes_constants import get_hermes_home
    from plugins.web import keyless_mcp
    from tools.web_result_cache import search_memo
    from tools.web_tools import web_search_tool

    rows = _ROWS if scenario == "partial" else []
    failures = [] if scenario == "empty" else failures
    payload = json.dumps({"results": rows, "unresponsive_engines": failures}).encode()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    config = {
        "web": {
            "backend": "searxng", "extract_backend": "tavily", "use_gateway": False,
            "keyless_rescue": scenario != "disabled", "keyless_fallback": True,
            "cache_enabled": True,
        }
    }
    (get_hermes_home() / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    ring_result = (
        {"success": False, "error": "synthetic ring outage"}
        if scenario == "ring-failed"
        else {"success": True, "data": {"web": [{"url": "https://rescue.example"}]}}
    )
    ring = Mock(side_effect=lambda *args: json.loads(json.dumps(ring_result)))
    monkeypatch.setattr(keyless_mcp, "search_with_failover", ring)
    search_memo.clear()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        monkeypatch.setenv("SEARXNG_URL", f"http://127.0.0.1:{server.server_port}")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            first = json.loads(web_search_tool("synthetic integration", limit=1))
            second = json.loads(web_search_tool("synthetic integration", limit=1))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            search_memo.clear()

    assert second == first
    if scenario in {"empty", "partial"}:
        assert first["success"] is True
        assert "rescued_from" not in first["data"]
        assert len(first["data"]["web"]) == len(rows[:1])
        if rows:
            assert first["data"]["web"][0]["url"] == "https://high.example"
        ring.assert_not_called()
        assert len(requests) == 1  # Legitimate successes still memoize.
        return

    assert len(requests) == 2  # Neither outages nor rescue results are cached.
    if scenario == "rescued":
        assert first["success"] is True
        assert first["data"]["rescued_from"] == "searxng"
        assert first["data"]["web"][0]["url"] == "https://rescue.example"
        assert error_detail in first["data"]["backend_error"]
    else:
        assert first["success"] is False
        assert error_detail in first["error"]
        if scenario == "ring-failed":
            assert "synthetic ring outage" in first["error"]

    assert ring.call_count == (0 if scenario == "disabled" else 2)
