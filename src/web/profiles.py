"""Validated, atomic management of processing profile directories."""

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from filelock import FileLock, Timeout
from pydantic import ValidationError

from ..processing import ProfileRegistry
from ..processing import profiles as profile_definitions
from ..processing.profiles import ProfileDefinition
from ..processing.tools import ToolRegistry
from .errors import ConsoleError, describe_validation_error

logger = logging.getLogger(__name__)

USER_PROFILE_MARKER = ".user-profile"
PROFILE_FILE = "profile.json"
PROMPT_FILES = {
    "match": "match.md",
    "analysis": "analysis.md",
    "enrichment": "enrichment.md",
}
MAX_PROMPT_CHARS = 100_000
RESERVED_PROFILE_IDS = frozenset(
    {"auto", "con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

_PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
# Console work files share this prefix so fresh-registry checks skip them.
_WORK_PREFIX = ".console-"
LOCK_FILE = f"{_WORK_PREFIX}lock"
_LOCK_TIMEOUT_SECONDS = 10
_EDITABLE_FILES = (PROFILE_FILE, *PROMPT_FILES.values(), USER_PROFILE_MARKER)
_CHANGED_MESSAGE = "The profile changed since it was loaded; reload it and try again"
_NOT_SAVED_MESSAGE = "The profile could not be saved; its previous files were kept"
_CHANGED_WHILE_READING_MESSAGE = "The profile changed while it was being read; try again"
_MARKER_TEXT = (
    "Created in the Horizon console. Delete this file to make the profile "
    "read-only there.\n"
)


class ProfileStore:
    """Read profiles and change only the ones created in the console."""

    def __init__(self, root: Path, *, default_profile: str, writable: bool = True):
        self.root = Path(root).resolve()
        self.default_profile = default_profile
        self.writable = writable
        self._file_lock: Optional[FileLock] = None

    @classmethod
    def from_profiles_dir(
        cls, profiles_dir: str, *, base_dir: Path, default_profile: str
    ) -> "ProfileStore":
        root = ProfileRegistry.resolve_root(Path(profiles_dir), base_dir=base_dir)
        packaged = profile_definitions.BUILTIN_PROFILES_DIR.resolve()
        return cls(
            root,
            default_profile=default_profile,
            writable=root.resolve() != packaged and os.access(root, os.W_OK),
        )

    def load_registry(self) -> ProfileRegistry:
        return self._load_registry(self.root)

    def list_profiles(self) -> list[dict[str, Any]]:
        registry = self.load_registry()
        directories = self._directories()
        return [
            self._summary(profile.id, profile.definition, directories.get(profile.id))
            for profile in registry.profiles
        ]

    def get_profile(self, profile_id: object) -> dict[str, Any]:
        """Return one consistent snapshot of a profile and its revision.

        Console writers replace profiles under the same lock, so they cannot
        land mid-read. The revision is computed from exactly the bytes returned,
        so saving a snapshot fails if the files no longer match it, whoever
        changed them.
        """
        if not self.writable:
            return self._read_profile(profile_id)
        with self._locked():
            return self._read_profile(profile_id)

    def _read_profile(self, profile_id: object) -> dict[str, Any]:
        registry = self.load_registry()
        if not isinstance(profile_id, str) or profile_id not in registry.ids:
            raise ConsoleError(404, "Profile not found")
        directory = self._directories().get(profile_id)
        if directory is None:
            raise ConsoleError(409, _CHANGED_WHILE_READING_MESSAGE)
        document, definition, prompts, revision = _read_snapshot(directory)
        if definition.id != profile_id:
            raise ConsoleError(409, _CHANGED_WHILE_READING_MESSAGE)
        return {
            **self._summary(profile_id, definition, directory),
            "revision": revision,
            "definition": document,
            "prompts": prompts,
        }

    def create_profile(
        self, profile_id: object, definition: object, prompts: object
    ) -> dict[str, Any]:
        self._require_writable()
        new_id = self._validate_new_id(profile_id)
        return self._create(new_id, definition, prompts)

    def copy_profile(
        self, source_id: object, profile_id: object, name: Optional[str] = None
    ) -> dict[str, Any]:
        self._require_writable()
        source = self.get_profile(source_id)
        new_id = self._validate_new_id(profile_id)
        definition = {
            **source["definition"],
            "id": new_id,
            "name": name or f"{source['name']} (copy)",
            "match": PROMPT_FILES["match"],
            "analysis": PROMPT_FILES["analysis"],
        }
        enrichment = definition.get("enrichment")
        if isinstance(enrichment, dict):
            definition["enrichment"] = {**enrichment, "prompt": PROMPT_FILES["enrichment"]}
        return self._create(new_id, definition, source["prompts"])

    def update_profile(
        self,
        profile_id: object,
        *,
        revision: object,
        definition: object,
        prompts: object,
    ) -> dict[str, Any]:
        directory = self._editable_directory(profile_id, revision)
        files = self._prepare_files(directory.name, definition, prompts)
        self._validate_candidate({directory.name: files})
        with self._locked():
            work, fingerprint = self._prepare_replacement(directory, files)
            try:
                self._require_unchanged(directory, revision)
                # Files outside the revision were copied; refuse to drop later changes.
                try:
                    unchanged = _tree_digest(directory) == fingerprint
                except OSError:
                    unchanged = False
                if not unchanged:
                    raise ConsoleError(409, _CHANGED_MESSAGE)
                self._swap_into_place(work / directory.name, directory)
            finally:
                _discard(work)
        return self.get_profile(directory.name)

    def delete_profile(
        self,
        profile_id: object,
        *,
        revision: object,
        check: Optional[Callable[[ProfileRegistry], None]] = None,
    ) -> None:
        """Delete a console profile after ``check`` accepts the remaining set."""
        directory = self._editable_directory(profile_id, revision)
        if directory.name == self.default_profile:
            raise ConsoleError(
                409, "The default profile cannot be deleted", ["processing.default_profile"]
            )
        registry = self._validate_candidate({directory.name: None})
        if check is not None:
            check(registry)
        with self._locked():
            self._require_unchanged(directory, revision)
            try:
                trash = self._work_directory("trash")
            except OSError as exc:
                raise ConsoleError(500, "The profile could not be deleted") from exc
            try:
                os.rename(directory, trash / directory.name)
            except OSError as exc:
                raise ConsoleError(500, "The profile could not be deleted") from exc
            finally:
                _discard(trash)

    def _create(self, profile_id: str, definition: object, prompts: object) -> dict[str, Any]:
        files = self._prepare_files(profile_id, definition, prompts)
        self._validate_candidate({profile_id: files})
        try:
            staging = self._work_directory("staging")
        except OSError as exc:
            raise ConsoleError(500, "The profile could not be created") from exc
        try:
            directory = staging / profile_id
            directory.mkdir()
            for name, text in files.items():
                (directory / name).write_text(text, encoding="utf-8")
            (directory / USER_PROFILE_MARKER).write_text(_MARKER_TEXT, encoding="utf-8")
            with self._locked():
                if os.path.lexists(self.root / profile_id):
                    raise ConsoleError(409, f"Profile already exists: {profile_id}")
                os.rename(directory, self.root / profile_id)
        except OSError as exc:
            raise ConsoleError(500, "The profile could not be created") from exc
        finally:
            _discard(staging)
        return self.get_profile(profile_id)

    def _work_directory(self, purpose: str) -> Path:
        """Create a hidden work directory in the profile root.

        Profiles are placed one level below it, where the ``*/profile.json``
        pattern used for discovery never finds them.
        """
        return Path(tempfile.mkdtemp(prefix=f"{_WORK_PREFIX}{purpose}-", dir=self.root))

    def _prepare_replacement(self, live: Path, files: dict[str, str]) -> tuple[Path, str]:
        """Build the complete replacement for ``live`` without touching it.

        New profile and prompt files keep the permission bits of the files they
        replace. The marker and any other entries are copied with their contents
        and permission bits. Returns the work directory and a fingerprint of
        ``live`` taken before copying.
        """
        try:
            work = self._work_directory("staging")
        except OSError as exc:
            raise ConsoleError(500, _NOT_SAVED_MESSAGE) from exc
        prepared = work / live.name
        try:
            fingerprint = _tree_digest(live)
            prepared.mkdir()
            for entry in live.iterdir():
                target = prepared / entry.name
                if entry.name in files:
                    continue
                if entry.is_symlink():
                    os.symlink(os.readlink(entry), target)
                elif entry.is_dir():
                    shutil.copytree(entry, target, symlinks=True)
                else:
                    shutil.copy2(entry, target)
            for name, text in files.items():
                mode = stat.S_IMODE((live / name).stat().st_mode)
                _write_new_file(prepared / name, text.encode("utf-8"), mode)
            os.chmod(prepared, stat.S_IMODE(live.stat().st_mode))
        except OSError as exc:
            _discard(work)
            raise ConsoleError(500, _NOT_SAVED_MESSAGE) from exc
        return work, fingerprint

    def _swap_into_place(self, prepared: Path, live: Path) -> None:
        """Move ``live`` to a backup, then move ``prepared`` to its name.

        Rollback only renames the backup back. The backup is deleted only after
        the replacement is in place, and kept if it cannot be restored.
        """
        try:
            backups = self._work_directory("backup")
        except OSError as exc:
            raise ConsoleError(500, _NOT_SAVED_MESSAGE) from exc
        backup = backups / live.name
        try:
            os.rename(live, backup)
        except OSError as exc:
            _discard(backups)
            raise ConsoleError(500, _NOT_SAVED_MESSAGE) from exc
        try:
            os.rename(prepared, live)
        except OSError as exc:
            try:
                os.rename(backup, live)
            except OSError as restore_error:
                raise ConsoleError(
                    500,
                    "The profile could not be saved or restored; its previous files "
                    "remain in the backup directory",
                    [backup.relative_to(self.root).as_posix()],
                ) from restore_error
            _discard(backups)
            raise ConsoleError(500, _NOT_SAVED_MESSAGE) from exc
        _discard(backups)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the profile directory lock shared by console processes.

        Writers hold it from their final check through the swap, and readers
        while taking a snapshot. Each store reuses one lock object, which
        filelock makes reentrant within a thread, so nested use cannot
        deadlock; separate objects in the same process exclude each other.
        """
        if self._file_lock is None:
            self._file_lock = FileLock(self.root / LOCK_FILE, timeout=_LOCK_TIMEOUT_SECONDS)
        lock = self._file_lock
        try:
            lock.acquire()
        except Timeout as exc:
            raise ConsoleError(
                409, "Another console process is saving profiles; try again"
            ) from exc
        except OSError as exc:
            raise ConsoleError(500, f"Could not create {LOCK_FILE} in the profile directory") from exc
        try:
            yield
        finally:
            try:
                lock.release()
            except OSError:
                # The guarded work has finished; do not report it as failed.
                logger.warning("Could not release %s", LOCK_FILE)

    @staticmethod
    def _require_unchanged(directory: Path, revision: object) -> None:
        if _revision(directory, _EDITABLE_FILES) != revision:
            raise ConsoleError(409, _CHANGED_MESSAGE)

    def _require_writable(self) -> None:
        if not self.writable:
            raise ConsoleError(
                403,
                "This profile directory is packaged or not writable; set "
                "processing.profiles_dir to a writable directory to manage profiles",
            )

    def _validate_new_id(self, profile_id: object) -> str:
        if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
            raise ConsoleError(
                400,
                "Profile IDs must start with a lowercase letter and use at most 64 "
                "lowercase letters, digits, '_' or '-'",
            )
        if profile_id in RESERVED_PROFILE_IDS:
            raise ConsoleError(400, f"Profile ID is reserved: {profile_id}")
        registry = self.load_registry()
        if profile_id in registry.ids or os.path.lexists(self.root / profile_id):
            raise ConsoleError(409, f"Profile already exists: {profile_id}")
        return profile_id

    def _editable_directory(self, profile_id: object, revision: object) -> Path:
        self._require_writable()
        detail = self.get_profile(profile_id)
        if not detail["editable"]:
            raise ConsoleError(
                403, "This profile is read-only in the console; copy it to make changes"
            )
        if not isinstance(revision, str) or revision != detail["revision"]:
            raise ConsoleError(409, _CHANGED_MESSAGE)
        return self.root / detail["id"]

    def _prepare_files(
        self, profile_id: str, definition: object, prompts: object
    ) -> dict[str, str]:
        if not isinstance(definition, dict):
            raise ConsoleError(400, "Profile definition must be a JSON object")
        definition = copy.deepcopy(definition)
        if definition.setdefault("id", profile_id) != profile_id:
            raise ConsoleError(
                400, "Profile IDs cannot be changed; copy the profile to use a new ID"
            )
        fixed_paths = [(definition, "match", "match"), (definition, "analysis", "analysis")]
        enrichment = definition.get("enrichment")
        if isinstance(enrichment, dict):
            fixed_paths.append((enrichment, "prompt", "enrichment"))
        for container, field, prompt in fixed_paths:
            if container.setdefault(field, PROMPT_FILES[prompt]) != PROMPT_FILES[prompt]:
                raise ConsoleError(
                    400, "Prompt files are fixed: match.md, analysis.md and enrichment.md"
                )
        try:
            parsed = ProfileDefinition.model_validate(definition)
        except ValidationError as exc:
            raise ConsoleError(
                400, "Invalid profile definition", describe_validation_error(exc)
            ) from exc
        requested_tools = {tool for block in parsed.enrichment.blocks for tool in block.tools}
        unknown_tools = sorted(requested_tools - ToolRegistry().names)
        if unknown_tools:
            raise ConsoleError(400, f"Unknown enrichment tool: {', '.join(unknown_tools)}")

        files = {PROFILE_FILE: json.dumps(definition, indent=2, ensure_ascii=False) + "\n"}
        for name, text in self._prepare_prompts(prompts).items():
            files[PROMPT_FILES[name]] = text
        return files

    @staticmethod
    def _prepare_prompts(prompts: object) -> dict[str, str]:
        if not isinstance(prompts, dict) or set(prompts) != set(PROMPT_FILES):
            raise ConsoleError(400, "prompts must contain match, analysis and enrichment text")
        prepared = {}
        for name in PROMPT_FILES:
            text = prompts[name]
            if not isinstance(text, str) or not text.strip():
                raise ConsoleError(400, f"The {name} prompt cannot be empty")
            if len(text) > MAX_PROMPT_CHARS or "\x00" in text:
                raise ConsoleError(
                    400,
                    f"The {name} prompt must be text of at most {MAX_PROMPT_CHARS} characters",
                )
            prepared[name] = text.strip() + "\n"
        return prepared

    def _validate_candidate(
        self, changes: dict[str, Optional[dict[str, str]]]
    ) -> ProfileRegistry:
        """Load a fresh registry from a copy of the profile set with ``changes``."""
        with tempfile.TemporaryDirectory(prefix="horizon-profiles-") as temp_dir:
            candidate = Path(temp_dir).resolve() / "profiles"
            candidate.mkdir()
            for entry in self.root.iterdir():
                if entry.name.startswith(_WORK_PREFIX):
                    continue
                target = candidate / entry.name
                if entry.is_symlink():
                    # Point at the resolved target so relative links stay valid.
                    try:
                        resolved = entry.resolve()
                    except (OSError, RuntimeError):
                        continue
                    target.symlink_to(resolved, target_is_directory=resolved.is_dir())
                elif entry.is_dir():
                    shutil.copytree(entry, target, symlinks=True)
            for directory_name, files in changes.items():
                target = candidate / directory_name
                if target.is_symlink():
                    raise ConsoleError(403, "Symlinked profiles cannot be changed here")
                if files is None:
                    shutil.rmtree(target)
                    continue
                target.mkdir(exist_ok=True)
                for name, text in files.items():
                    (target / name).write_text(text, encoding="utf-8")
            return self._load_registry(candidate)

    def _load_registry(self, root: Path) -> ProfileRegistry:
        try:
            return ProfileRegistry.load(root, self.default_profile)
        except (OSError, ValueError) as exc:
            raise ConsoleError(
                400, "Profiles could not be loaded", self._diagnose(root, exc)
            ) from exc

    @staticmethod
    def _diagnose(root: Path, exc: Exception) -> list[str]:
        """Name the failing profile file without exposing absolute paths."""
        for config_path in sorted(root.glob(f"*/{PROFILE_FILE}")):
            location = f"{config_path.parent.name}/{PROFILE_FILE}"
            try:
                ProfileDefinition.model_validate_json(config_path.read_text(encoding="utf-8"))
            except ValidationError as error:
                return [f"{location}: {detail}" for detail in describe_validation_error(error)]
            except (OSError, ValueError):
                return [f"{location}: file could not be read as UTF-8 text"]
        if isinstance(exc, ValidationError):
            return describe_validation_error(exc)
        message = str(exc)
        for path in {str(root), str(root.resolve())}:
            message = message.replace(path, "profiles")
        return [message]

    def _directories(self) -> dict[str, Path]:
        directories: dict[str, Path] = {}
        for config_path in sorted(self.root.glob(f"*/{PROFILE_FILE}")):
            try:
                profile_id = json.loads(config_path.read_text(encoding="utf-8")).get("id")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(profile_id, str):
                directories.setdefault(profile_id, config_path.parent)
        return directories

    def _summary(
        self, profile_id: str, definition: ProfileDefinition, directory: Optional[Path]
    ) -> dict[str, Any]:
        return {
            "id": profile_id,
            "name": definition.name,
            "editable": directory is not None
            and self._is_editable(profile_id, definition, directory),
            "default": profile_id == self.default_profile,
        }

    def _is_editable(
        self, profile_id: str, definition: ProfileDefinition, directory: Path
    ) -> bool:
        if not self.writable or directory.name != profile_id or directory.is_symlink():
            return False
        if directory.resolve().parent != self.root:
            return False
        prompt_paths = (definition.match, definition.analysis, definition.enrichment.prompt)
        if prompt_paths != tuple(PROMPT_FILES.values()):
            return False
        return all(is_regular_file(directory / name) for name in _EDITABLE_FILES)


def _read_optional(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _digest(contents: Iterable[tuple[str, Optional[bytes]]]) -> str:
    digest = hashlib.sha256()
    for name, data in contents:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(b"\0missing" if data is None else data)
        digest.update(b"\0")
    return digest.hexdigest()


def _revision(directory: Path, names: tuple[str, ...]) -> str:
    return _digest((name, _read_optional(directory / name)) for name in names)


def _read_snapshot(
    directory: Path,
) -> tuple[dict[str, Any], ProfileDefinition, dict[str, str], str]:
    """Read a profile once and derive its content and revision from those bytes."""
    try:
        raw_definition = (directory / PROFILE_FILE).read_bytes()
        definition = ProfileDefinition.model_validate_json(raw_definition)
        document = json.loads(raw_definition)
        contents: list[tuple[str, Optional[bytes]]] = [(PROFILE_FILE, raw_definition)]
        prompts = {}
        prompt_paths = {
            "match": definition.match,
            "analysis": definition.analysis,
            "enrichment": definition.enrichment.prompt,
        }
        resolved_directory = directory.resolve()
        for key, name in prompt_paths.items():
            path = (resolved_directory / name).resolve()
            if not path.is_relative_to(resolved_directory):
                raise ValueError(f"Prompt path escapes profile directory: {name}")
            data = path.read_bytes()
            contents.append((name, data))
            prompts[key] = data.decode("utf-8").strip()
        contents.append((USER_PROFILE_MARKER, _read_optional(directory / USER_PROFILE_MARKER)))
    except (OSError, ValueError) as exc:
        raise ConsoleError(409, _CHANGED_WHILE_READING_MESSAGE) from exc
    return document, definition, prompts, _digest(contents)


def _tree_digest(directory: Path) -> str:
    """Fingerprint names, types, modes, file bytes, and link targets below ``directory``."""
    digest = hashlib.sha256()

    def visit(path: Path, prefix: str) -> None:
        with os.scandir(path) as scanner:
            entries = sorted(scanner, key=lambda entry: entry.name)
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            name = f"{prefix}{entry.name}"
            digest.update(f"{name}\0{info.st_mode}\0".encode("utf-8", "surrogateescape"))
            if stat.S_ISLNK(info.st_mode):
                digest.update(os.fsencode(os.readlink(entry.path)))
            elif stat.S_ISDIR(info.st_mode):
                visit(Path(entry.path), f"{name}/")
            elif stat.S_ISREG(info.st_mode):
                digest.update(Path(entry.path).read_bytes())
            digest.update(b"\0")

    visit(directory, "")
    return digest.hexdigest()


def _write_new_file(path: Path, content: bytes, mode: int) -> None:
    with open(path, "xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)


def _discard(path: Path) -> None:
    """Remove a console work directory without failing the operation."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove %s; it is safe to delete", path.name)


def is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False
