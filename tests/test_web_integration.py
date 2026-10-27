"""Compatibility tests against the actual loopback HTTP server."""
import http.client
import json
from pathlib import Path
import shutil
import threading

import pytest

from src.web.server import ConsoleServer, ConsoleState


@pytest.fixture
def console(tmp_path):
    shutil.copytree(Path(__file__).resolve().parents[1] / "profiles", tmp_path / "profiles")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/config.json").write_text(json.dumps({
        "ai": {"provider": "openai", "model": "${LOCAL_MODEL}", "api_protocol": "responses", "api_key_env": "OPENAI_API_KEY"},
        "processing": {"profiles_dir": "profiles", "default_profile": "tech-news"},
        "sources": {"rss": [{"name": "Feed", "url": "https://example.com/rss"}]},
        "digest": {"max_items": 7},
    }))
    (tmp_path / ".env").write_text("LOCAL_MODEL=local-model\nOPENAI_API_KEY=private-key\n")
    state = ConsoleState(tmp_path)
    server = ConsoleServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(server, method, path, payload=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Horizon-Token"] = server.state.token
    conn.request(method, path, None if payload is None else json.dumps(payload), headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body.decode()


def test_profiles_incrementally_available_without_breaking_legacy(console):
    status, listing = request(console, "GET", "/api/profiles")
    assert status == 200
    assert "tech-news" in [p["id"] for p in listing["profiles"]]
    status, copied = request(console, "POST", "/api/profiles/tech-news/copy", {"id": "my-news", "name": "My News"})
    assert status == 201
    assert copied["editable"] is True
    status, legacy = request(console, "GET", "/api/state")
    assert status == 200
    assert legacy["ai"]["model"] == "local-model"
    assert legacy["config"]["ai"]["api_protocol"] == "responses"
    assert "my-news" in [p["id"] for p in legacy["profiles"]]
    assert "private-key" not in json.dumps(legacy)
    assert request(console, "GET", "/api/run")[1]["status"] == "idle"
    assert request(console, "GET", "/api/summaries") == (200, [])


def test_profile_crud_routing_revision_and_run_lock(console):
    status, copied = request(console, "POST", "/api/profiles/tech-news/copy", {"id": "my-news"})
    assert status == 201
    status, detail = request(console, "GET", "/api/profiles/my-news")
    assert status == 200
    detail["prompts"]["match"] = "Match local news."
    assert request(console, "PUT", "/api/profiles/my-news", detail)[0] == 200
    assert request(console, "PUT", "/api/profiles/my-news", detail)[0] == 409
    status, routing = request(console, "GET", "/api/profile-routing")
    assert status == 200
    routing["routes"] = {"rss.0": ["my-news", "tech-news"]}
    assert request(console, "PUT", "/api/profile-routing", routing)[0] == 200
    status, detail = request(console, "GET", "/api/profiles/my-news")
    assert any("sources.rss.0" in ref for ref in detail["references"])
    assert request(console, "DELETE", "/api/profiles/my-news", detail)[0] == 409
    assert request(console, "POST", "/api/profiles/tech-news/copy", {"id": "denied"}, token=False)[0] == 403
    console.state.run["status"] = "running"
    assert request(console, "POST", "/api/profiles/tech-news/copy", {"id": "locked"})[0] == 409
    console.state.run["status"] = "idle"
    status, routing = request(console, "GET", "/api/profile-routing")
    routing["routes"] = {"rss.0": None}
    assert request(console, "PUT", "/api/profile-routing", routing)[0] == 200
    assert request(console, "DELETE", "/api/profiles/my-news", detail)[0] == 200
    assert request(console, "GET", "/api/profiles/my-news")[0] == 404
    legacy = request(console, "GET", "/api/state")[1]
    assert legacy["config"]["digest"]["max_items"] == 7
    assert legacy["config"]["ai"]["api_protocol"] == "responses"


def test_profile_editor_is_part_of_original_ui(console):
    status, html = request(console, "GET", "/")
    assert status == 200
    assert 'id="page-profiles"' in html
    assert 'id="reader-article"' in html
    script = request(console, "GET", "/app.js")[1]
    assert "/api/profile-routing" in script
    assert "/api/profiles" in script
