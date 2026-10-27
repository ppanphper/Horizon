"""Persistence and HTTP/process-boundary coverage for the local console."""

import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from dotenv import dotenv_values
import pytest

from src.web import server


@pytest.fixture
def state(tmp_path):
    (tmp_path / "data/summaries").mkdir(parents=True)
    config = {
        "ai": {"provider": "openai", "model": "test-model", "api_key_env": "HORIZON_AI_API_KEY", "languages": ["zh"]},
        "sources": {"rss": [], "hackernews": {"enabled": False}},
        "processing": {"profiles_dir": str(Path(__file__).resolve().parents[1] / "profiles"), "default_profile": "tech-news"},
        "digest": {"max_items": 15},
    }
    (tmp_path / "data/config.json").write_text(json.dumps(config))
    (tmp_path / ".env").write_text("# retain this comment\nHORIZON_AI_API_KEY=old-private-key\nOTHER_SETTING=keep-me\n")
    return server.ConsoleState(tmp_path)


def payload(state, **kwargs):
    snap = state.snapshot()
    return {"config": snap["config"], "revision": snap["revision"], **kwargs}


def test_save_retains_secret_unknown_sections_and_backup(state):
    before = state.config_path.read_text()
    data = payload(state, api_key="")
    data["config"]["sources"]["google_news"] = {"enabled": False, "query": "test"}
    data["config"]["digest"]["max_items"] = 9
    result = state.save(data)
    assert result["config"]["digest"]["max_items"] == 9
    assert result["config"]["sources"]["google_news"]["query"] == "test"
    assert "old-private-key" not in json.dumps(result)
    assert dotenv_values(state.env_path)["HORIZON_AI_API_KEY"] == "old-private-key"
    assert state.config_path.with_suffix(".json.web-backup").read_text() == before


def test_responses_protocol_persists_through_web_save_and_reload(state):
    data = payload(state)
    data["config"]["ai"]["api_protocol"] = "responses"
    saved = state.save(data)
    assert saved["config"]["ai"]["api_protocol"] == "responses"
    reloaded = server.ConsoleState(state.root).snapshot()
    assert reloaded["config"]["ai"]["api_protocol"] == "responses"
    assert "old-private-key" not in json.dumps(reloaded)


def test_responses_protocol_requires_openai_provider(state):
    data = payload(state)
    data["config"]["ai"].update(api_protocol="responses", provider="anthropic")
    before = state.config_path.read_bytes()
    with pytest.raises(server.RequestError):
        state.save(data)
    assert state.config_path.read_bytes() == before


def test_key_save_escapes_quotes_and_keeps_bind_mount_inode(state):
    inode = state.env_path.stat().st_ino
    key = "new-key-'quoted'-$literal-back\\slash"
    state.save(payload(state, api_key=key))
    env = dotenv_values(state.env_path)
    assert env["HORIZON_AI_API_KEY"] == key
    assert env["OTHER_SETTING"] == "keep-me"
    assert "# retain this comment" in state.env_path.read_text()
    assert state.env_path.stat().st_ino == inode
    assert state.env_path.stat().st_mode & 0o777 == 0o600
    assert key not in json.dumps(state.snapshot())
    state.save(payload(state, clear_key=True))
    assert not state.snapshot()["ai"]["key_configured"]


def test_stale_tab_cannot_overwrite_external_edit(state):
    old = payload(state)
    state.env_path.write_text(state.env_path.read_text() + "NEW_SETTING=external\n")
    with pytest.raises(server.RequestError, match="重新加载") as error:
        state.save(old)
    assert error.value.status == 409
    assert "NEW_SETTING=external" in state.env_path.read_text()


def test_failed_config_write_rolls_back_key(state, monkeypatch):
    original = state.env_path.read_bytes()
    write = server._atomic_write_text
    def fail_config(path, content):
        if path == state.config_path:
            raise OSError("simulated disk error")
        write(path, content)
    monkeypatch.setattr(server, "_atomic_write_text", fail_config)
    with pytest.raises(OSError):
        state.save(payload(state, api_key="do-not-keep-this-key"))
    assert state.env_path.read_bytes() == original


@pytest.mark.parametrize("change", [
    lambda c: c["ai"].update(languages=[]),
    lambda c: c["ai"].update(analysis_concurrency=0),
    lambda c: c["ai"].update(api_key_env="sk-not-an-env"),
    lambda c: c["ai"].update(base_url="javascript:alert(1)"),
    lambda c: c["ai"].update(base_url="https://user:password@example.com"),
    lambda c: c["processing"].update(default_profile="missing"),
    lambda c: c["digest"].update(max_items=-1),
    lambda c: c["sources"].update(github=[{"type": "repo_releases", "repo": "missing-owner"}]),
    lambda c: c["sources"].update(hackernews={"enabled": True, "fetch_top_stories": 0}),
    lambda c: c["sources"].update(rss=[{"name": "test", "url": "https://example.com/feed", "profile": "missing"}]),
])
def test_invalid_save_does_not_change_config_or_key(state, change):
    old_config = state.config_path.read_bytes()
    old_env = state.env_path.read_bytes()
    data = payload(state, api_key="replacement-must-not-be-written")
    change(data["config"])
    with pytest.raises(server.RequestError):
        state.save(data)
    assert state.config_path.read_bytes() == old_config
    assert state.env_path.read_bytes() == old_env


def test_summary_path_cannot_escape_and_symlinks_rejected(state):
    name = "horizon-2026-10-16-zh.md"
    (state.summaries / name).write_text("# 本地日报\n<script>alert(1)</script>")
    assert state.read_summary(name)["markdown"].startswith("# 本地日报")
    for unsafe in ["../.env", "../../private.md", "horizon-2026-10-16-../../.env"]:
        with pytest.raises(server.RequestError):
            state.read_summary(unsafe)
    (state.summaries / name).unlink()
    (state.summaries / name).symlink_to(state.env_path)
    assert state.list_summaries() == []
    with pytest.raises(server.RequestError):
        state.read_summary(name)


def test_missing_credentials_fail_before_starting_process(state, monkeypatch):
    state.env_path.write_text("HORIZON_AI_API_KEY=\n")
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(server.RequestError, match="API 密钥") as error:
        state.start()
    assert error.value.status == 422


def test_process_exclusivity_log_redaction_and_exit_state(state, monkeypatch):
    real_popen = subprocess.Popen
    def fixture_process(argv, **kwargs):
        assert argv[1:3] == ["-m", "src.main"]
        return real_popen([sys.executable, "-u", "-c", "import os,time; print(os.environ['HORIZON_AI_API_KEY']); time.sleep(.15)"], **kwargs)
    monkeypatch.setattr(server.subprocess, "Popen", fixture_process)
    assert state.start()["status"] == "running"
    with pytest.raises(server.RequestError):
        state.start()
    with pytest.raises(server.RequestError):
        state.save(payload(state))
    deadline = time.monotonic() + 5
    while state.run_snapshot()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.02)
    run = state.run_snapshot()
    assert run["status"] == "success"
    assert run["exit_code"] == 0
    assert "old-private-key" not in json.dumps(run)
    assert "[已隐藏]" in run["logs"]


def test_stop_terminates_child_and_reports_stopped(state, monkeypatch):
    real_popen = subprocess.Popen
    def fixture_process(argv, **kwargs):
        return real_popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"], **kwargs)
    monkeypatch.setattr(server.subprocess, "Popen", fixture_process)
    state.start()
    state.stop()
    deadline = time.monotonic() + 5
    while state.run_snapshot()["status"] == "stopping" and time.monotonic() < deadline:
        time.sleep(.02)
    assert state.run_snapshot()["status"] == "stopped"
    assert state.process.poll() is not None


@pytest.fixture
def http_server(state):
    httpd = server.ConsoleServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def request(httpd, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection(*httpd.server_address, timeout=5)
    conn.request(method, path, json.dumps(data) if data is not None else None, headers or {})
    response = conn.getresponse()
    body = response.read()
    status, result_headers = response.status, dict(response.getheaders())
    conn.close()
    return status, body, result_headers


def test_http_local_host_csrf_and_cross_origin_checks(http_server, state):
    code, body, headers = request(http_server, "GET", "/api/state")
    assert code == 200
    assert b"old-private-key" not in body
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request(http_server, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert request(http_server, "GET", "/api/state", headers={"Origin": "https://evil.example"})[0] == 403
    assert request(http_server, "GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    headers = {"Content-Type": "application/json"}
    assert request(http_server, "POST", "/api/config", payload(state), headers)[0] == 403
    headers["X-Horizon-Token"] = state.token
    assert request(http_server, "POST", "/api/config", payload(state), headers)[0] == 200
    assert request(http_server, "POST", "/api/validate", {"config": state.read()}, headers)[0] == 200
    assert request(http_server, "GET", "/.env")[0] == 404
    assert request(http_server, "GET", "/api/summaries/%2e%2e%2f.env")[0] == 404


def test_http_serves_packaged_assets(http_server):
    for path, mime in [("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")]:
        code, body, headers = request(http_server, "GET", path)
        assert code == 200 and body
        assert headers["Content-Type"].startswith(mime)


def test_summary_api_renders_and_download_keeps_exact_source(http_server, state):
    name = "horizon-2026-10-16-zh.md"
    content = "# 本地日报\r\n\r\n## 新闻\r\n\r\n**重点**和[来源](https://example.com)。\r\n"
    (state.summaries / name).write_bytes(content.encode("utf-8"))
    code, body, _ = request(http_server, "GET", "/api/summaries/" + name)
    assert code == 200
    data = json.loads(body)
    assert data["markdown"] == content
    assert "<strong>重点</strong>" in data["html"]
    assert data["toc"][0]["title"] == "新闻"
    code, body, headers = request(http_server, "GET", "/api/summaries/" + name + "/download")
    assert code == 200 and body == content.encode("utf-8")
    assert headers["Content-Type"].startswith("text/markdown")
    assert headers["Content-Disposition"] == f'attachment; filename="{name}"'
    assert request(http_server, "GET", "/api/summaries/%2e%2e%2f.env/download")[0] == 404
    assert request(http_server, "GET", "/api/summaries/horizon-2099-01-01-zh.md/download")[0] == 404


def test_summary_invalid_encoding_and_listing(state):
    (state.summaries / "horizon-bad.md").write_text("invalid filename")
    assert state.list_summaries() == []
    name = "horizon-2026-10-16-en.md"
    (state.summaries / name).write_bytes(b"\xff\xfe")
    with pytest.raises(server.RequestError) as error:
        state.read_summary(name)
    assert error.value.status == 422
