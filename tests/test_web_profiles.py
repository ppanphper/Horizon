import json
import os
import shutil
import stat
import threading
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

import src.processing.profiles as profile_module
from src.processing import ProfileRegistry
from src.web.errors import ConsoleError
from src.web.profiles import LOCK_FILE, USER_PROFILE_MARKER, ProfileStore

REPO_PROFILES = Path(__file__).resolve().parents[1] / "profiles"
PROMPTS = {
    "match": "Items about local testing.",
    "analysis": "Score from 0 to 10.",
    "enrichment": "Write a short summary.",
}


def definition(profile_id, **overrides):
    payload = {
        "id": profile_id,
        "name": "Local Test",
        "match": "match.md",
        "analysis": "analysis.md",
        "enrichment": {
            "prompt": "enrichment.md",
            "blocks": [{"id": "summary", "type": "section", "tools": [], "primary": True}],
        },
    }
    payload.update(overrides)
    return payload


def snapshot(root):
    """File contents below root, ignoring the console's lock file."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != LOCK_FILE
    }


def leftover_work_entries(root):
    return [path.name for path in root.iterdir() if path.name.startswith(".") and path.name != LOCK_FILE]


@pytest.fixture
def profiles_root(tmp_path):
    root = tmp_path / "profiles"
    shutil.copytree(REPO_PROFILES, root)
    return root


@pytest.fixture
def store(profiles_root):
    return ProfileStore(profiles_root, default_profile="tech-news")


def test_lists_builtin_profiles_as_read_only(store):
    profiles = {profile["id"]: profile for profile in store.list_profiles()}

    assert {"tech-news", "tech-blog", "finance-news", "ai-creator"} <= set(profiles)
    assert all(profile["editable"] is False for profile in profiles.values())
    assert profiles["tech-news"]["default"] is True
    assert profiles["tech-blog"]["default"] is False


def test_profile_detail_contains_content_without_filesystem_paths(store, profiles_root):
    detail = store.get_profile("tech-blog")

    assert detail["definition"]["id"] == "tech-blog"
    assert set(detail["prompts"]) == {"match", "analysis", "enrichment"}
    assert detail["prompts"]["analysis"]
    assert detail["revision"]
    assert str(profiles_root) not in json.dumps(detail)


def test_unknown_profile_detail_is_not_found(store):
    with pytest.raises(ConsoleError) as exc:
        store.get_profile("missing")

    assert exc.value.status == 404


def test_create_profile_is_editable_and_loads_in_fresh_registry(store, profiles_root):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)

    assert created["editable"] is True
    assert (profiles_root / "local-test" / USER_PROFILE_MARKER).is_file()
    registry = ProfileRegistry.load(profiles_root, "tech-news")
    assert registry.get("local-test").match_prompt == PROMPTS["match"]
    assert leftover_work_entries(profiles_root) == []


def test_create_fills_fixed_id_and_prompt_paths(store, profiles_root):
    payload = definition("filled")
    del payload["id"], payload["match"], payload["analysis"], payload["enrichment"]["prompt"]

    store.create_profile("filled", payload, PROMPTS)

    saved = json.loads((profiles_root / "filled" / "profile.json").read_text(encoding="utf-8"))
    assert saved["id"] == "filled"
    assert saved["match"] == "match.md"
    assert saved["enrichment"]["prompt"] == "enrichment.md"


@pytest.mark.parametrize(
    "profile_id",
    [
        "../evil",
        "evil/child",
        "Evil",
        "",
        ".hidden",
        "1abc",
        "-abc",
        "has space",
        "nul\x00",
        "auto",
        "con",
        "a" * 65,
        None,
    ],
)
def test_rejects_unsafe_or_reserved_profile_ids(store, profiles_root, profile_id):
    before = snapshot(profiles_root.parent)

    with pytest.raises(ConsoleError) as exc:
        store.create_profile(profile_id, definition("evil"), PROMPTS)

    assert exc.value.status == 400
    assert snapshot(profiles_root.parent) == before


def test_rejects_existing_profile_id(store, profiles_root):
    before = snapshot(profiles_root)

    with pytest.raises(ConsoleError) as exc:
        store.create_profile("tech-news", definition("tech-news"), PROMPTS)

    assert exc.value.status == 409
    assert snapshot(profiles_root) == before


@pytest.mark.parametrize(
    ("payload", "prompts", "message"),
    [
        (definition("bad", extra=True), PROMPTS, "Extra inputs"),
        (definition("other"), PROMPTS, "cannot be changed"),
        (definition("bad", match="../outside.md"), PROMPTS, "fixed"),
        (
            definition(
                "bad",
                enrichment={
                    "prompt": "enrichment.md",
                    "blocks": [{"id": "summary", "type": "section", "tools": ["shell"]}],
                },
            ),
            PROMPTS,
            "Unknown enrichment tool",
        ),
        (
            definition("bad", enrichment={"prompt": "enrichment.md", "blocks": []}),
            PROMPTS,
            "enrichment.blocks",
        ),
        (definition("bad"), {**PROMPTS, "analysis": "   "}, "analysis"),
        (definition("bad"), {"match": "only"}, "prompts"),
        (definition("bad"), {**PROMPTS, "match": "x" * 100_001}, "match"),
        ([], PROMPTS, "definition"),
    ],
)
def test_invalid_create_writes_nothing(store, profiles_root, payload, prompts, message):
    before = snapshot(profiles_root)

    with pytest.raises(ConsoleError) as exc:
        store.create_profile("bad", payload, prompts)

    assert exc.value.status == 400
    assert message in " ".join([exc.value.message, *exc.value.details])
    assert snapshot(profiles_root) == before


def test_update_requires_current_revision(store, profiles_root):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)

    updated = store.update_profile(
        "local-test",
        revision=created["revision"],
        definition=definition("local-test", name="Renamed"),
        prompts={**PROMPTS, "analysis": "New rubric."},
    )

    assert updated["name"] == "Renamed"
    assert updated["revision"] != created["revision"]
    assert ProfileRegistry.load(profiles_root, "tech-news").get("local-test").analysis_prompt == "New rubric."

    before = snapshot(profiles_root)
    with pytest.raises(ConsoleError) as exc:
        store.update_profile(
            "local-test",
            revision=created["revision"],
            definition=definition("local-test", name="Stale"),
            prompts=PROMPTS,
        )
    assert exc.value.status == 409
    assert snapshot(profiles_root) == before


@pytest.mark.parametrize(
    "payload",
    [
        definition("local-test", enrichment={"prompt": "enrichment.md", "blocks": []}),
        definition("renamed-id"),
        definition("local-test", content={"analysis_max_chars": 10}),
    ],
)
def test_invalid_update_leaves_prior_profile_unchanged(store, profiles_root, payload):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)
    before = snapshot(profiles_root)

    with pytest.raises(ConsoleError) as exc:
        store.update_profile(
            "local-test", revision=created["revision"], definition=payload, prompts=PROMPTS
        )

    assert exc.value.status == 400
    assert snapshot(profiles_root) == before


def test_update_preserves_file_permissions(store, profiles_root):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)
    profile_json = profiles_root / "local-test" / "profile.json"
    os.chmod(profile_json, 0o644)

    store.update_profile(
        "local-test",
        revision=store.get_profile("local-test")["revision"],
        definition=definition("local-test", name="Renamed"),
        prompts=PROMPTS,
    )

    assert stat.S_IMODE(profile_json.stat().st_mode) == 0o644
    assert created["revision"] != store.get_profile("local-test")["revision"]


def file_modes(directory):
    return {path.name: stat.S_IMODE(path.stat().st_mode) for path in directory.iterdir()}


CHANGED_PROMPTS = {name: f"Changed {name}." for name in PROMPTS}


@pytest.fixture
def live_profile(store, profiles_root):
    """A console profile with non-default modes and an extra user file."""
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)
    directory = profiles_root / "local-test"
    os.chmod(directory / "analysis.md", 0o640)
    (directory / "notes.txt").write_text("keep me\n", encoding="utf-8")
    os.chmod(directory / "notes.txt", 0o600)
    os.chmod(directory, 0o750)
    return directory, created["revision"]


def directory_state(directory):
    return snapshot(directory), file_modes(directory), stat.S_IMODE(directory.stat().st_mode)


def update_live(store, revision, name="Renamed"):
    return store.update_profile(
        "local-test",
        revision=revision,
        definition=definition("local-test", name=name),
        prompts=CHANGED_PROMPTS,
    )


def test_update_replaces_directory_and_keeps_marker_extra_files_and_modes(
    store, profiles_root, live_profile
):
    directory, revision = live_profile
    marker = (directory / USER_PROFILE_MARKER).read_bytes()

    updated = update_live(store, revision)

    assert updated["name"] == "Renamed"
    assert updated["editable"] is True
    assert (directory / "analysis.md").read_text(encoding="utf-8") == "Changed analysis.\n"
    assert (directory / USER_PROFILE_MARKER).read_bytes() == marker
    assert (directory / "notes.txt").read_text(encoding="utf-8") == "keep me\n"
    modes = file_modes(directory)
    assert modes["analysis.md"] == 0o640
    assert modes["notes.txt"] == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert leftover_work_entries(profiles_root) == []


def test_update_rejects_extra_file_changed_during_preparation(
    store, profiles_root, live_profile, monkeypatch
):
    directory, revision = live_profile
    prepare = ProfileStore._prepare_replacement

    def edit_after_copy(self, live, files):
        prepared = prepare(self, live, files)
        (live / "notes.txt").write_text("edited elsewhere\n", encoding="utf-8")
        return prepared

    monkeypatch.setattr(ProfileStore, "_prepare_replacement", edit_after_copy)

    with pytest.raises(ConsoleError) as exc:
        update_live(store, revision)

    assert exc.value.status == 409
    assert (directory / "notes.txt").read_text(encoding="utf-8") == "edited elsewhere\n"
    assert (directory / "analysis.md").read_text(encoding="utf-8") == PROMPTS["analysis"] + "\n"
    assert leftover_work_entries(profiles_root) == []


def test_failed_swap_restores_original_directory(store, profiles_root, live_profile, monkeypatch):
    directory, revision = live_profile
    before = directory_state(directory)
    real_rename = os.rename

    def rename(source, target):
        if Path(target) == directory and Path(source).parent.name.startswith(".console-staging-"):
            raise OSError("simulated swap failure")
        return real_rename(source, target)

    monkeypatch.setattr(os, "rename", rename)

    with pytest.raises(ConsoleError) as exc:
        update_live(store, revision)

    monkeypatch.undo()
    assert exc.value.status == 500
    assert "previous files were kept" in exc.value.message
    assert directory_state(directory) == before
    assert leftover_work_entries(profiles_root) == []


def test_failed_restore_keeps_backup_and_reports_it(store, profiles_root, live_profile, monkeypatch):
    directory, revision = live_profile
    before = directory_state(directory)
    real_rename = os.rename

    def rename(source, target):
        if Path(target) == directory:
            raise OSError("simulated rename failure")
        return real_rename(source, target)

    monkeypatch.setattr(os, "rename", rename)

    with pytest.raises(ConsoleError) as exc:
        update_live(store, revision)

    monkeypatch.undo()
    assert exc.value.status == 500
    assert "could not be saved or restored" in exc.value.message
    assert "previous files were kept" not in exc.value.message
    backup = profiles_root / exc.value.details[0]
    assert backup.parent.name.startswith(".console-backup-")
    assert directory_state(backup) == before
    assert not directory.exists()


def test_cleanup_failure_after_swap_keeps_successful_update(
    store, profiles_root, live_profile, monkeypatch
):
    directory, revision = live_profile
    real_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith(".console-"):
            raise OSError("simulated cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)

    updated = update_live(store, revision)

    monkeypatch.undo()
    assert updated["name"] == "Renamed"
    assert (directory / "analysis.md").read_text(encoding="utf-8") == "Changed analysis.\n"
    assert any(name.startswith(".console-backup-") for name in leftover_work_entries(profiles_root))
    registry = ProfileRegistry.load(profiles_root, "tech-news")
    assert registry.get("local-test").definition.name == "Renamed"
    assert [profile["id"] for profile in store.list_profiles()].count("local-test") == 1
    assert store.get_profile("local-test")["revision"] == updated["revision"]


@pytest.mark.parametrize("failure", ["write", "sync", "copy"])
def test_preparation_failure_leaves_live_files_untouched(
    store, profiles_root, live_profile, monkeypatch, failure
):
    directory, revision = live_profile
    before = directory_state(directory)

    def fail(*args, **kwargs):
        raise OSError(f"simulated {failure} failure")

    if failure == "write":
        real_open = open

        def failing_open(file, mode="r", *args, **kwargs):
            if mode == "xb" and Path(file).name == "enrichment.md":
                raise OSError("simulated write failure")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", failing_open)
    elif failure == "sync":
        monkeypatch.setattr(os, "fsync", fail)
    else:
        monkeypatch.setattr(shutil, "copy2", fail)

    with pytest.raises(ConsoleError) as exc:
        update_live(store, revision)

    monkeypatch.undo()
    assert exc.value.status == 500
    assert "previous files were kept" in exc.value.message
    assert directory_state(directory) == before
    assert leftover_work_entries(profiles_root) == []


def test_detail_read_during_concurrent_update_is_one_snapshot(profiles_root, monkeypatch):
    reader = ProfileStore(profiles_root, default_profile="tech-news")
    writer = ProfileStore(profiles_root, default_profile="tech-news")
    reader.create_profile("local-test", definition("local-test"), PROMPTS)
    before = reader.get_profile("local-test")
    writer_thread = None
    writer_errors = []
    # Set when the writer has finished, or is blocked behind a lock the reader holds.
    writer_progressed = threading.Event()
    real_acquire = FileLock.acquire

    def observed_acquire(lock, *args, **kwargs):
        if threading.current_thread() is writer_thread:
            try:
                return real_acquire(lock, blocking=False)
            except Timeout:
                writer_progressed.set()
        return real_acquire(lock, *args, **kwargs)

    def write():
        try:
            writer.update_profile(
                "local-test",
                revision=before["revision"],
                definition=definition("local-test", name="Writer"),
                prompts={**PROMPTS, "analysis": "Writer rubric."},
            )
        except Exception as exc:  # reported by the assertion below
            writer_errors.append(exc)
        finally:
            writer_progressed.set()

    read_directories = reader._directories

    def directories_with_concurrent_update():
        nonlocal writer_thread
        if writer_thread is None:
            writer_thread = threading.Thread(target=write)
            writer_thread.start()
            assert writer_progressed.wait(timeout=30)
        return read_directories()

    monkeypatch.setattr(FileLock, "acquire", observed_acquire)
    monkeypatch.setattr(reader, "_directories", directories_with_concurrent_update)

    detail = reader.get_profile("local-test")
    writer_thread.join(timeout=30)
    monkeypatch.undo()

    assert not writer_thread.is_alive()
    assert writer_errors == []
    after = reader.get_profile("local-test")
    assert after["prompts"]["analysis"] == "Writer rubric."
    assert (detail["prompts"], detail["revision"]) in [
        (before["prompts"], before["revision"]),
        (after["prompts"], after["revision"]),
    ]
    assert detail in (before, after)

    try:
        reader.update_profile(
            "local-test",
            revision=detail["revision"],
            definition=detail["definition"],
            prompts={**detail["prompts"], "match": "Reader edit."},
        )
    except ConsoleError as exc:
        assert exc.status == 409
    analysis = (profiles_root / "local-test" / "analysis.md").read_text(encoding="utf-8")
    assert analysis == "Writer rubric.\n"


def test_update_rejects_profile_changed_during_validation(store, profiles_root, monkeypatch):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)
    analysis = profiles_root / "local-test" / "analysis.md"
    validate = ProfileStore._validate_candidate

    def edit_during_validation(self, changes):
        registry = validate(self, changes)
        analysis.write_text("External edit.\n", encoding="utf-8")
        return registry

    monkeypatch.setattr(ProfileStore, "_validate_candidate", edit_during_validation)

    with pytest.raises(ConsoleError) as exc:
        store.update_profile(
            "local-test",
            revision=created["revision"],
            definition=definition("local-test", name="Console edit"),
            prompts={**PROMPTS, "analysis": "Console edit."},
        )

    assert exc.value.status == 409
    assert analysis.read_text(encoding="utf-8") == "External edit.\n"
    saved = json.loads((profiles_root / "local-test" / "profile.json").read_text(encoding="utf-8"))
    assert saved["name"] == "Local Test"


def test_delete_rejects_profile_changed_during_validation(store, profiles_root, monkeypatch):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)
    validate = ProfileStore._validate_candidate

    def edit_during_validation(self, changes):
        registry = validate(self, changes)
        (profiles_root / "local-test" / "match.md").write_text("External edit.\n", encoding="utf-8")
        return registry

    monkeypatch.setattr(ProfileStore, "_validate_candidate", edit_during_validation)

    with pytest.raises(ConsoleError) as exc:
        store.delete_profile("local-test", revision=created["revision"])

    assert exc.value.status == 409
    assert (profiles_root / "local-test" / "match.md").read_text(encoding="utf-8") == "External edit.\n"


def test_builtin_and_hand_made_profiles_are_read_only(store, profiles_root):
    shutil.copytree(profiles_root / "tech-blog", profiles_root / "hand-made")
    hand_made = profiles_root / "hand-made" / "profile.json"
    hand_made.write_text(
        hand_made.read_text(encoding="utf-8").replace('"tech-blog"', '"hand-made"'),
        encoding="utf-8",
    )
    before = snapshot(profiles_root)

    for profile_id in ("tech-news", "hand-made"):
        detail = store.get_profile(profile_id)
        assert detail["editable"] is False
        with pytest.raises(ConsoleError) as exc:
            store.update_profile(
                profile_id,
                revision=detail["revision"],
                definition=detail["definition"],
                prompts=detail["prompts"],
            )
        assert exc.value.status == 403
        with pytest.raises(ConsoleError) as exc:
            store.delete_profile(profile_id, revision=detail["revision"])
        assert exc.value.status == 403

    assert snapshot(profiles_root) == before


def test_symlinked_profile_directory_is_read_only(store, profiles_root, tmp_path):
    outside = tmp_path / "outside" / "linked"
    shutil.copytree(profiles_root / "tech-blog", outside)
    profile_json = outside / "profile.json"
    profile_json.write_text(
        profile_json.read_text(encoding="utf-8").replace('"tech-blog"', '"linked"'),
        encoding="utf-8",
    )
    (outside / USER_PROFILE_MARKER).write_text("", encoding="utf-8")
    (profiles_root / "linked").symlink_to(outside, target_is_directory=True)

    assert store.get_profile("linked")["editable"] is False


def test_relative_symlinked_default_profile_survives_candidate_validation(profiles_root, tmp_path):
    outside = tmp_path / "shared" / "linked"
    shutil.copytree(profiles_root / "tech-blog", outside)
    profile_json = outside / "profile.json"
    profile_json.write_text(
        profile_json.read_text(encoding="utf-8").replace('"tech-blog"', '"linked"'),
        encoding="utf-8",
    )
    (profiles_root / "linked").symlink_to(Path("..") / "shared" / "linked", target_is_directory=True)
    linked_store = ProfileStore(profiles_root, default_profile="linked")

    created = linked_store.create_profile("local-test", definition("local-test"), PROMPTS)

    assert created["editable"] is True
    assert (profiles_root / "linked").readlink() == Path("..") / "shared" / "linked"


def test_copy_builtin_creates_editable_profile(store, profiles_root):
    before = snapshot(profiles_root / "tech-blog")

    copied = store.copy_profile("tech-blog", "my-blog")

    source = store.get_profile("tech-blog")
    assert copied["editable"] is True
    assert copied["definition"]["id"] == "my-blog"
    assert copied["definition"]["enrichment"] == source["definition"]["enrichment"]
    assert copied["prompts"] == source["prompts"]
    assert snapshot(profiles_root / "tech-blog") == before
    assert ProfileRegistry.load(profiles_root, "tech-news").get("my-blog")


def test_delete_removes_user_profile(store, profiles_root):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)

    with pytest.raises(ConsoleError) as exc:
        store.delete_profile("local-test", revision="stale")
    assert exc.value.status == 409

    store.delete_profile("local-test", revision=created["revision"])

    assert not (profiles_root / "local-test").exists()
    assert "local-test" not in ProfileRegistry.load(profiles_root, "tech-news").ids
    assert leftover_work_entries(profiles_root) == []


def test_delete_runs_registry_check_before_removing(store, profiles_root):
    created = store.create_profile("local-test", definition("local-test"), PROMPTS)

    def reject(registry):
        assert "local-test" not in registry.ids
        raise ConsoleError(409, "still referenced")

    with pytest.raises(ConsoleError, match="still referenced"):
        store.delete_profile("local-test", revision=created["revision"], check=reject)

    assert (profiles_root / "local-test" / "profile.json").is_file()


def test_save_is_rejected_when_profile_set_is_invalid(store, profiles_root):
    broken = profiles_root / "broken"
    broken.mkdir()
    (broken / "profile.json").write_text("{", encoding="utf-8")
    before = snapshot(profiles_root)

    with pytest.raises(ConsoleError) as exc:
        store.create_profile("local-test", definition("local-test"), PROMPTS)

    assert exc.value.status == 400
    assert "broken/profile.json" in " ".join(exc.value.details)
    assert str(profiles_root) not in " ".join([exc.value.message, *exc.value.details])
    assert snapshot(profiles_root) == before


def test_unwritable_profile_directory_is_browsable_read_only(profiles_root, monkeypatch):
    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode, *args, **kwargs: (
            False if Path(path) == profiles_root and mode & os.W_OK
            else real_access(path, mode, *args, **kwargs)
        ),
    )
    read_only_store = ProfileStore.from_profiles_dir(
        str(profiles_root), base_dir=profiles_root.parent, default_profile="tech-news"
    )
    monkeypatch.undo()

    assert read_only_store.writable is False
    assert read_only_store.get_profile("tech-blog")["editable"] is False
    assert not (profiles_root / LOCK_FILE).exists()
    with pytest.raises(ConsoleError) as exc:
        read_only_store.copy_profile("tech-blog", "my-blog")
    assert exc.value.status == 403


def test_packaged_profiles_are_never_written(tmp_path, monkeypatch):
    packaged = tmp_path / "packaged"
    shutil.copytree(REPO_PROFILES, packaged)
    monkeypatch.setattr(profile_module, "BUILTIN_PROFILES_DIR", packaged)
    working_dir = tmp_path / "working"
    working_dir.mkdir()

    packaged_store = ProfileStore.from_profiles_dir(
        "profiles", base_dir=working_dir, default_profile="tech-news"
    )

    assert packaged_store.writable is False
    with pytest.raises(ConsoleError) as exc:
        packaged_store.copy_profile("tech-blog", "my-blog")
    assert exc.value.status == 403
    assert not (packaged / "my-blog").exists()
