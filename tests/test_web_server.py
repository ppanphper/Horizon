import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from src.web import profile_server as web_server
from src.web.profile_server import MAX_BODY_BYTES, ConsoleServer
from src.web.service import ConsoleService

REPO_PROFILES = Path(__file__).resolve().parents[1] / "profiles"
CSRF_TOKEN = "test-csrf-token"
FAKE_TOKEN = "fake-token-for-tests-321"
PROMPTS = {"match": "Match.", "analysis": "Score 0-10.", "enrichment": "Summarize."}
DEFINITION = {
    "name": "Local Test",
    "enrichment": {"blocks": [{"id": "summary", "type": "section", "tools": []}]},
}


class IdleRunner:
    running = False

    def is_running(self):
        return self.running

    def start(self, extra_secrets=()):
        self.running = True
        return self.status()

    def status(self):
        return {"state": "running" if self.running else "idle", "log": []}


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("HORIZON_TEST_TOKEN", FAKE_TOKEN)
    shutil.copytree(REPO_PROFILES, tmp_path / "profiles")
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "ai": {"provider": "openai", "model": "gpt-4", "api_key_env": "OPENAI_API_KEY"},
                "sources": {
                    "rss": [
                        {
                            "name": "Feed",
                            "url": "https://example.com/rss?token=${HORIZON_TEST_TOKEN}",
                        }
                    ]
                },
                "processing": {"profiles_dir": "profiles", "default_profile": "tech-news"},
                "webhook": {"headers": "Authorization: Bearer ${HORIZON_TEST_TOKEN}"},
            }
        ),
        encoding="utf-8",
    )
    runner = IdleRunner()
    service = ConsoleService(
        config_path,
        base_dir=tmp_path,
        summaries_dir=tmp_path / "data" / "summaries",
        runner=runner,
    )
    server = ConsoleServer(("127.0.0.1", 0), service, csrf_token=CSRF_TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, method, path, body=None, headers=None):
    port = server.server_address[1]
    final_headers = {}
    if method != "GET":
        final_headers = {
            "Origin": f"http://127.0.0.1:{port}",
            "X-CSRF-Token": CSRF_TOKEN,
            "Content-Type": "application/json",
        }
    final_headers.update(headers or {})
    final_headers = {name: value for name, value in final_headers.items() if value is not None}
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=final_headers)
        response = connection.getresponse()
        return response, response.read()
    finally:
        connection.close()


def test_index_is_self_contained_with_security_headers(console):
    response, body = request(console, "GET", "/")

    assert response.status == 200
    policy = response.getheader("Content-Security-Policy")
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Cache-Control") == "no-store"
    assert f'content="{CSRF_TOKEN}"'.encode() in body
    for asset in ("/app.js", "/app.css"):
        asset_response, asset_body = request(console, "GET", asset)
        assert asset_response.status == 200
        assert b"https://" not in asset_body and b"http://" not in asset_body
    assert b"https://" not in body and b"http://" not in body


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "localhost:1"])
def test_rejects_unexpected_host_header(console, host):
    response, _ = request(console, "GET", "/api/profiles", headers={"Host": host})

    assert response.status == 403


@pytest.mark.parametrize("origin", [None, "null", "http://evil.example", "https://127.0.0.1"])
def test_writes_require_same_origin(console, origin):
    response, _ = request(console, "POST", "/api/run", body={}, headers={"Origin": origin})

    assert response.status == 403
    assert console.service.runner.running is False


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_writes_require_csrf_token(console, token):
    response, _ = request(console, "POST", "/api/run", body={}, headers={"X-CSRF-Token": token})

    assert response.status == 403
    assert console.service.runner.running is False


def test_rejects_malformed_or_oversized_bodies(console):
    response, _ = request(
        console, "PUT", "/api/config", body=b"{}", headers={"Content-Type": "text/plain"}
    )
    assert response.status == 415

    response, _ = request(console, "PUT", "/api/config", body=b"{not json")
    assert response.status == 400

    response, _ = request(console, "PUT", "/api/config", body=[])
    assert response.status == 400

    response, _ = request(console, "PUT", "/api/config", body=b"[" * 100_000)
    assert response.status == 400

    port = console.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest("PUT", "/api/config")
        connection.putheader("Origin", f"http://127.0.0.1:{port}")
        connection.putheader("X-CSRF-Token", CSRF_TOKEN)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        assert connection.getresponse().status == 413
    finally:
        connection.close()


def test_profile_lifecycle_over_http(console):
    response, body = request(
        console,
        "POST",
        "/api/profiles",
        body={"id": "local-test", "definition": DEFINITION, "prompts": PROMPTS},
    )
    assert response.status == 201
    created = json.loads(body)
    assert created["editable"] is True

    response, body = request(
        console,
        "PUT",
        "/api/profiles/local-test",
        body={
            "revision": created["revision"],
            "definition": {**DEFINITION, "name": "Renamed"},
            "prompts": PROMPTS,
        },
    )
    assert response.status == 200
    updated = json.loads(body)
    assert updated["name"] == "Renamed"

    response, body = request(console, "POST", "/api/profiles/tech-blog/copy", body={"id": "my-blog"})
    assert response.status == 201
    assert json.loads(body)["editable"] is True

    response, _ = request(
        console, "DELETE", "/api/profiles/local-test", body={"revision": updated["revision"]}
    )
    assert response.status == 200
    response, _ = request(console, "GET", "/api/profiles/local-test")
    assert response.status == 404


def test_traversal_in_profile_path_is_rejected(console, tmp_path):
    for path in ("/api/profiles/..%2F..%2Fdata", "/api/profiles/../data/copy"):
        response, _ = request(console, "POST", path, body={"id": "evil"})
        assert response.status in (400, 404)
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path / "profiles" / "evil").exists()


def test_api_responses_never_include_secrets(console):
    for path in ("/api/config", "/api/profiles", "/api/profiles/tech-news", "/api/run", "/"):
        response, body = request(console, "GET", path)
        assert response.status == 200
        assert FAKE_TOKEN.encode() not in body
        assert b"Bearer" not in body


def test_saves_are_rejected_while_run_is_active(console):
    _, body = request(console, "GET", "/api/config")
    revision = json.loads(body)["revision"]

    response, body = request(console, "POST", "/api/run", body={})
    assert response.status == 200
    assert json.loads(body)["state"] == "running"

    response, body = request(
        console, "PUT", "/api/config", body={"revision": revision, "default_profile": "tech-blog"}
    )
    assert response.status == 409
    assert "run is in progress" in json.loads(body)["error"]


def test_unknown_routes_are_not_found(console):
    assert request(console, "GET", "/api/missing")[0].status == 404
    assert request(console, "DELETE", "/api/config", body={})[0].status == 404


def test_allowed_hosts_include_the_bound_loopback_address():
    assert "127.0.0.2:8765" in web_server.allowed_hosts("127.0.0.2", 8765)
    assert "[::1]:8765" in web_server.allowed_hosts("::1", 8765)
    assert "localhost:8765" in web_server.allowed_hosts("127.0.0.1", 8765)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_main_refuses_non_loopback_bind(host):
    with pytest.raises(SystemExit) as exc:
        web_server.main(["--host", host])

    assert exc.value.code == 2


def test_main_requires_existing_config(tmp_path):
    assert web_server.main(["--data-dir", str(tmp_path / "missing"), "--port", "0"]) == 1
