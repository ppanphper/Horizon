import json
import shutil
from pathlib import Path

import pytest

from src.web.errors import ConsoleError
from src.web.service import ConsoleService

REPO_PROFILES = Path(__file__).resolve().parents[1] / "profiles"
PROMPTS = {"match": "Match.", "analysis": "Score 0-10.", "enrichment": "Summarize."}
DEFINITION = {
    "name": "Local Test",
    "enrichment": {"blocks": [{"id": "summary", "type": "section", "tools": []}]},
}


class IdleRunner:
    running = False
    extra_secrets = None

    def is_running(self):
        return self.running

    def start(self, extra_secrets=()):
        self.running = True
        self.extra_secrets = set(extra_secrets)
        return {"state": "running"}

    def status(self):
        return {"state": "running" if self.running else "idle", "log": []}


@pytest.fixture
def project(tmp_path):
    shutil.copytree(REPO_PROFILES, tmp_path / "profiles")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "ai": {"provider": "openai", "model": "gpt-4", "api_key_env": "OPENAI_API_KEY"},
                "sources": {"rss": [{"name": "Feed", "url": "https://example.com/rss"}]},
                "processing": {"profiles_dir": "profiles", "default_profile": "tech-news"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def runner():
    return IdleRunner()


@pytest.fixture
def service(project, runner):
    return ConsoleService(
        project / "data" / "config.json",
        base_dir=project,
        summaries_dir=project / "data" / "summaries",
        runner=runner,
    )


def create_local_profile(service):
    return service.create_profile({"id": "local-test", "definition": DEFINITION, "prompts": PROMPTS})


@pytest.mark.parametrize("operation", ["list", "get"])
def test_profile_reads_wait_for_default_profile_directory_swap(service, project, monkeypatch, operation):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from src.web.server import ConsoleState

    service = ConsoleState(project).profiles_service
    created = create_local_profile(service)
    service.update_config(
        {"revision": service.get_config()["revision"], "default_profile": "local-test"}
    )
    live = project / "profiles" / "local-test"
    swapped_out, resume, read_started = (threading.Event() for _ in range(3))
    rename = os.rename

    def pause_after_backup(source, target):
        rename(source, target)
        if Path(source) == live:
            swapped_out.set()
            assert resume.wait(5), "Timed out waiting to complete directory swap"

    monkeypatch.setattr(os, "rename", pause_after_backup)

    def read():
        read_started.set()
        return service.list_profiles() if operation == "list" else service.get_profile("local-test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(service.update_profile, "local-test", {
            "revision": created["revision"], "definition": DEFINITION,
            "prompts": {**PROMPTS, "analysis": "Updated score."},
        })
        try:
            assert swapped_out.wait(5)
            reader = pool.submit(read)
            assert read_started.wait(5)
            # Give the reader an opportunity to enter config validation during the gap.
            reader_done_during_swap = reader.done()
            try:
                reader.result(timeout=0.2)
                reader_done_during_swap = True
            except TimeoutError:
                pass
            except ConsoleError:
                reader_done_during_swap = True
        finally:
            resume.set()
        updated = writer.result(timeout=5)
        result = reader.result(timeout=5)
    assert not reader_done_during_swap
    assert updated["prompts"]["analysis"] == "Updated score."
    if operation == "list":
        assert "local-test" in {item["id"] for item in result["profiles"]}
    else:
        assert result["revision"] == updated["revision"]
        assert result["prompts"] == updated["prompts"]


def test_delete_rejects_referenced_profile_and_names_references(service, project):
    create_local_profile(service)
    config = service.get_config()
    service.update_config(
        {
            "revision": config["revision"],
            "routes": {"rss.0": ["tech-news", "local-test"]},
            "profile_settings": {"local-test": {"threshold": 5, "topic_dedup": True}},
        }
    )
    detail = service.get_profile("local-test")

    with pytest.raises(ConsoleError) as exc:
        service.delete_profile("local-test", {"revision": detail["revision"]})

    assert exc.value.status == 409
    assert exc.value.details == [
        "processing.profile_settings.local-test",
        "sources.rss.0 (Feed)",
    ]
    assert detail["references"] == exc.value.details
    assert (project / "profiles" / "local-test" / "profile.json").is_file()


def test_delete_rejects_default_profile(service, project):
    create_local_profile(service)
    service.update_config(
        {"revision": service.get_config()["revision"], "default_profile": "local-test"}
    )

    with pytest.raises(ConsoleError) as exc:
        service.delete_profile(
            "local-test", {"revision": service.get_profile("local-test")["revision"]}
        )

    assert exc.value.status == 409
    assert "processing.default_profile" in exc.value.details
    assert (project / "profiles" / "local-test").is_dir()


def test_clearing_explicit_default_settings_allows_delete(service, project):
    created = create_local_profile(service)
    config_path = project / "data" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["processing"]["profile_settings"] = {"local-test": {}}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    assert service.get_profile("local-test")["references"] == [
        "processing.profile_settings.local-test"
    ]

    service.update_config(
        {
            "revision": service.get_config()["revision"],
            "profile_settings": {"local-test": {"threshold": None, "topic_dedup": True}},
        }
    )
    service.delete_profile("local-test", {"revision": created["revision"]})

    assert not (project / "profiles" / "local-test").exists()


def test_delete_unreferenced_profile(service, project):
    created = create_local_profile(service)

    service.delete_profile("local-test", {"revision": created["revision"]})

    assert "local-test" not in {profile["id"] for profile in service.list_profiles()["profiles"]}


def test_saves_are_rejected_while_run_is_active(service, project, runner):
    created = create_local_profile(service)
    config = service.get_config()
    profiles_before = sorted(path.name for path in (project / "profiles").iterdir())
    config_before = (project / "data" / "config.json").read_bytes()
    service.start_run()

    attempts = [
        lambda: service.create_profile({"id": "other", "definition": DEFINITION, "prompts": PROMPTS}),
        lambda: service.copy_profile("tech-blog", {"id": "copied"}),
        lambda: service.update_profile(
            "local-test",
            {"revision": created["revision"], "definition": DEFINITION, "prompts": PROMPTS},
        ),
        lambda: service.delete_profile("local-test", {"revision": created["revision"]}),
        lambda: service.update_config({"revision": config["revision"], "default_profile": "tech-blog"}),
    ]
    for attempt in attempts:
        with pytest.raises(ConsoleError) as exc:
            attempt()
        assert exc.value.status == 409
        assert "run is in progress" in exc.value.message

    assert sorted(path.name for path in (project / "profiles").iterdir()) == profiles_before
    assert (project / "data" / "config.json").read_bytes() == config_before
    assert service.get_profile("local-test")["revision"] == created["revision"]


def test_run_masks_values_referenced_by_config(service, project, runner, monkeypatch):
    monkeypatch.setenv("HORIZON_TEST_FEED_PATH", "private-feed-path")
    config_path = project / "data" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["sources"]["rss"][0]["url"] = "https://example.com/${HORIZON_TEST_FEED_PATH}"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    service.start_run()

    assert "private-feed-path" in runner.extra_secrets


def test_run_masks_config_values_even_when_config_is_invalid(service, project, runner, monkeypatch):
    monkeypatch.setenv("HORIZON_TEST_FEED_PATH", "private-feed-path")
    config_path = project / "data" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["sources"]["rss"][0]["url"] = "not a url ${HORIZON_TEST_FEED_PATH}"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    service.start_run()

    assert "private-feed-path" in runner.extra_secrets


def test_lists_and_reads_valid_summary_names_only(service, project, tmp_path):
    summaries = project / "data" / "summaries"
    summaries.mkdir()
    (summaries / "horizon-2026-10-22-en.md").write_text("# Older", encoding="utf-8")
    (summaries / "horizon-2026-10-23-zh.md").write_text("# Newer", encoding="utf-8")
    (summaries / "notes.md").write_text("private", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (summaries / "horizon-2026-10-24-en.md").symlink_to(outside)

    assert service.list_summaries()["summaries"] == [
        "horizon-2026-10-23-zh.md",
        "horizon-2026-10-22-en.md",
    ]
    assert service.get_summary("horizon-2026-10-23-zh.md")["markdown"] == "# Newer"

    for name, status in [
        ("notes.md", 400),
        ("../data/config.json", 400),
        ("horizon-2026-10-23-zh.md/..", 400),
        ("horizon-2026-10-21-en.md", 404),
        ("horizon-2026-10-24-en.md", 404),
    ]:
        with pytest.raises(ConsoleError) as exc:
            service.get_summary(name)
        assert exc.value.status == status


def test_missing_summary_directory_lists_nothing(service):
    assert service.list_summaries() == {"summaries": []}
