import json
import shutil
import types
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.models import SourcesConfig
from src.orchestrator import HorizonOrchestrator
from src.storage.manager import StorageManager
from src.web.errors import ConsoleError
from src.web.profiles import ProfileStore
from src.web.runtime_config import ROUTE_LOCATIONS, RuntimeConfigStore

REPO_PROFILES = Path(__file__).resolve().parents[1] / "profiles"
FAKE_TOKEN = "fake-token-for-tests-123"
FAKE_PASSWORD = "fake-password-for-tests-456"
RAW_CONFIG = {
    "ai": {
        "provider": "openai",
        "model": "gpt-4",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "${HORIZON_TEST_BASE_URL}",
    },
    "sources": {
        "rss": [
            {
                "name": "Fixed feed",
                "url": "https://example.com/feed.xml?token=${HORIZON_TEST_TOKEN}",
                "profile": "tech-news",
            },
            {"name": "Auto feed", "url": "https://example.com/auto.xml"},
        ],
        "telegram": {
            "channels": [{"channel": "candidates", "profile": ["tech-news", "finance-news"]}]
        },
        "hackernews": {"enabled": False},
    },
    "processing": {
        "profiles_dir": "profiles",
        "default_profile": "tech-news",
        "profile_settings": {"tech-news": {"threshold": 7.0}},
    },
    "webhook": {
        "enabled": False,
        "url_env": "HORIZON_WEBHOOK_URL",
        "headers": "Authorization: Bearer ${HORIZON_TEST_TOKEN}",
    },
    "digest": {"max_items": 20},
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HORIZON_TEST_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv(
        "HORIZON_TEST_BASE_URL", f"https://user:{FAKE_PASSWORD}@llm.example.com/v1"
    )
    shutil.copytree(REPO_PROFILES, tmp_path / "profiles")
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(RAW_CONFIG, indent=2), encoding="utf-8")
    return tmp_path


@pytest.fixture
def store(project):
    return RuntimeConfigStore(project / "data" / "config.json", base_dir=project)


def read_raw(project):
    return json.loads((project / "data" / "config.json").read_text(encoding="utf-8"))


def test_snapshot_lists_routes_and_settings_without_secrets(store):
    snapshot = store.snapshot()

    sources = {source["key"]: source for source in snapshot["sources"]}
    assert sources["rss.0"]["route"] == "tech-news"
    assert sources["rss.0"]["label"] == "Fixed feed"
    assert sources["rss.1"]["route"] is None
    assert sources["telegram.channels.0"]["route"] == ["tech-news", "finance-news"]
    assert sources["hackernews"]["enabled"] is False
    assert snapshot["default_profile"] == "tech-news"
    assert snapshot["profile_settings"] == {"tech-news": {"threshold": 7.0, "topic_dedup": True}}
    assert "tech-blog" in snapshot["profiles"]

    serialized = json.dumps(snapshot)
    for secret in (FAKE_TOKEN, FAKE_PASSWORD, "Bearer", "example.com", "HORIZON_WEBHOOK_URL"):
        assert secret not in serialized


def test_update_changes_only_managed_fields(store, project):
    revision = store.snapshot()["revision"]

    snapshot = store.update(
        {
            "revision": revision,
            "default_profile": "tech-blog",
            "profile_settings": {
                "tech-news": {"threshold": 7.0, "topic_dedup": True},
                "tech-blog": {"threshold": 4.0, "topic_dedup": False},
            },
            "routes": {
                "rss.0": None,
                "rss.1": "tech-blog",
                "telegram.channels.0": ["tech-news", "tech-blog"],
            },
        }
    )

    raw = read_raw(project)
    assert snapshot["revision"] != revision
    assert raw["ai"] == RAW_CONFIG["ai"]
    assert raw["webhook"] == RAW_CONFIG["webhook"]
    assert raw["digest"] == RAW_CONFIG["digest"]
    assert raw["sources"]["hackernews"] == {"enabled": False}
    assert raw["sources"]["rss"][0] == {
        "name": "Fixed feed",
        "url": "https://example.com/feed.xml?token=${HORIZON_TEST_TOKEN}",
    }
    assert raw["sources"]["rss"][1]["profile"] == "tech-blog"
    assert raw["sources"]["telegram"]["channels"][0]["profile"] == ["tech-news", "tech-blog"]
    assert raw["processing"]["default_profile"] == "tech-blog"
    assert raw["processing"]["profile_settings"] == {
        "tech-news": {"threshold": 7.0},
        "tech-blog": {"threshold": 4.0, "topic_dedup": False},
    }


def test_default_settings_remove_explicit_entry(store, project):
    store.update(
        {
            "revision": store.snapshot()["revision"],
            "profile_settings": {"tech-news": {"threshold": None, "topic_dedup": True}},
        }
    )

    assert read_raw(project)["processing"]["profile_settings"] == {}


def write_raw(project, raw):
    (project / "data" / "config.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")


@pytest.mark.parametrize(
    "stored",
    [{}, {"threshold": None}, {"topic_dedup": True}, {"threshold": None, "topic_dedup": True}],
)
def test_explicit_default_settings_are_removed(store, project, stored):
    raw = read_raw(project)
    raw["processing"]["profile_settings"]["tech-blog"] = stored
    write_raw(project, raw)

    store.update(
        {
            "revision": store.snapshot()["revision"],
            "profile_settings": {"tech-blog": {"threshold": None, "topic_dedup": True}},
        }
    )

    assert read_raw(project)["processing"]["profile_settings"] == {"tech-news": {"threshold": 7.0}}


@pytest.mark.parametrize("processing", [None, {}, {"profile_settings": {}}])
def test_default_settings_leave_config_without_settings_unchanged(store, project, processing):
    raw = read_raw(project)
    if processing is None:
        del raw["processing"]
    else:
        raw["processing"] = processing
    write_raw(project, raw)
    before = (project / "data" / "config.json").read_bytes()

    store.update(
        {
            "revision": store.snapshot()["revision"],
            "profile_settings": {
                profile_id: {"threshold": None, "topic_dedup": True}
                for profile_id in store.snapshot()["profiles"]
            },
        }
    )

    assert (project / "data" / "config.json").read_bytes() == before


def test_update_rejects_stale_revision(store, project):
    before = (project / "data" / "config.json").read_bytes()

    with pytest.raises(ConsoleError) as exc:
        store.update({"revision": "stale", "default_profile": "tech-blog"})

    assert exc.value.status == 409
    assert (project / "data" / "config.json").read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"default_profile": "missing"},
        {"default_profile": ""},
        {"routes": {"rss.0": "missing"}},
        {"routes": {"rss.0": ["tech-news", "tech-news"]}},
        {"routes": {"rss.0": []}},
        {"routes": {"rss.0": ["tech-news", "auto"]}},
        {"routes": {"rss.0": 5}},
        {"routes": {"rss.9": "tech-news"}},
        {"routes": {"ai": "tech-news"}},
        {"routes": []},
        {"profile_settings": {"missing": {"threshold": 5}}},
        {"profile_settings": {"tech-news": {"threshold": 11}}},
        {"profile_settings": {"tech-news": {"filter": True}}},
    ],
)
def test_invalid_update_leaves_config_unchanged(store, project, changes):
    before = (project / "data" / "config.json").read_bytes()

    with pytest.raises(ConsoleError) as exc:
        store.update({"revision": store.snapshot()["revision"], **changes})

    assert exc.value.status == 400
    assert (project / "data" / "config.json").read_bytes() == before


def test_validation_errors_do_not_echo_secrets(store, project, monkeypatch):
    monkeypatch.setenv("HORIZON_TEST_BASE_URL", "")
    raw = read_raw(project)
    raw["sources"]["rss"][1]["url"] = f"not a url {FAKE_TOKEN}"
    (project / "data" / "config.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConsoleError) as exc:
        store.snapshot()

    assert exc.value.status == 400
    assert FAKE_TOKEN not in json.dumps(exc.value.to_dict())
    assert "sources.rss.1.url" in " ".join(exc.value.details)


def test_saved_profile_and_config_load_in_next_pipeline_run(store, project, monkeypatch):
    profile_store = ProfileStore(project / "profiles", default_profile="tech-news")
    profile_store.copy_profile("tech-blog", "my-blog")
    store.update(
        {
            "revision": store.snapshot()["revision"],
            "default_profile": "my-blog",
            "routes": {"rss.1": "my-blog"},
        }
    )
    monkeypatch.chdir(project)

    storage = StorageManager(data_dir=str(project / "data"))
    config = storage.load_config()
    orchestrator = HorizonOrchestrator(config, storage)

    assert orchestrator.profiles.default_profile == "my-blog"
    assert orchestrator.profiles.get("my-blog").definition.name == "Technology Blog (copy)"
    assert config.sources.rss[1].profile == "my-blog"


def test_references_identify_every_profile_use(store, project):
    raw = read_raw(project)
    raw["processing"]["profile_settings"]["finance-news"] = {"threshold": 5}
    raw["digest"] = {
        "group_by": "profile",
        "profile_order": ["finance-news"],
        "category_groups": {"markets": {"limit": 3, "categories": ["finance-news"]}},
    }
    (project / "data" / "config.json").write_text(json.dumps(raw), encoding="utf-8")

    references = store.references(store.read(), "finance-news")

    assert references == [
        "processing.profile_settings.finance-news",
        "digest.profile_order",
        "digest.category_groups.markets",
        "sources.telegram.channels.0 (candidates)",
    ]
    assert store.references(store.read(), "tech-news")[0] == "processing.default_profile"


def _unwrap(annotation):
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        annotation = next(arg for arg in typing.get_args(annotation) if arg is not type(None))
    if typing.get_origin(annotation) is list:
        return typing.get_args(annotation)[0], True
    return annotation, False


def test_route_locations_cover_every_source_model():
    expected = set()
    for name, field in SourcesConfig.model_fields.items():
        model, is_list = _unwrap(field.annotation)
        if "profile" in model.model_fields:
            expected.add((name, is_list))
            continue
        for child_name, child in model.model_fields.items():
            child_model, child_is_list = _unwrap(child.annotation)
            if isinstance(child_model, type) and issubclass(child_model, BaseModel):
                if "profile" in child_model.model_fields:
                    expected.add((f"{name}.{child_name}", child_is_list))

    assert set(ROUTE_LOCATIONS) == expected
