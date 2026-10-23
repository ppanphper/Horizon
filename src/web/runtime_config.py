"""Read and update the profile-related parts of the runtime configuration."""

import copy
import hashlib
import json
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .._file_utils import _atomic_write_text
from ..models import Config, ProfileRoute, ProfileSettingsConfig
from ..storage.manager import _expand_env_vars
from .errors import ConsoleError, describe_validation_error
from .profiles import ProfileStore

# Source configuration objects that carry a ``profile`` route, as
# (dotted location under ``sources``, whether the location holds a list).
ROUTE_LOCATIONS = (
    ("github", True),
    ("hackernews", False),
    ("rss", True),
    ("reddit.subreddits", True),
    ("reddit.users", True),
    ("telegram.channels", True),
    ("twitter", False),
    ("openbb.watchlists", True),
    ("ossinsight", False),
    ("gdelt", False),
    ("google_news", False),
)

_LABEL_FIELDS = {
    "rss": "name",
    "reddit.subreddits": "subreddit",
    "reddit.users": "username",
    "telegram.channels": "channel",
    "openbb.watchlists": "name",
}
_LABEL_PREFIXES = {"reddit.subreddits": "r/", "reddit.users": "u/"}
_MAX_LABEL_CHARS = 120


@dataclass(frozen=True)
class ConfigDocument:
    """The raw JSON file, its revision, and its validated expansion."""

    raw: dict[str, Any]
    revision: str
    config: Config


def iter_route_targets(sources: object) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield each routable source object keyed by its location."""
    if not isinstance(sources, dict):
        return
    for location, is_list in ROUTE_LOCATIONS:
        node: object = sources
        for part in location.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if is_list and isinstance(node, list):
            for index, entry in enumerate(node):
                if isinstance(entry, dict):
                    yield f"{location}.{index}", entry
        elif not is_list and isinstance(node, dict):
            yield location, node


def _label(key: str, entry: dict[str, Any]) -> str:
    location = key.rsplit(".", 1)[0] if key[-1].isdigit() else key
    if location == "github":
        target = "/".join(str(entry[name]) for name in ("owner", "repo") if entry.get(name))
        label = f"{entry.get('type') or ''} {target or entry.get('username') or ''}"
    elif location in _LABEL_FIELDS:
        label = f"{_LABEL_PREFIXES.get(location, '')}{entry.get(_LABEL_FIELDS[location]) or ''}"
    else:
        label = ""
    return label.strip()[:_MAX_LABEL_CHARS]


def _parse_route(value: object) -> ProfileRoute:
    """Normalize a route so that automatic routing is always ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return None if value in ("", "auto") else value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value]
    raise ConsoleError(400, "Routes must be null, a profile ID, or a list of profile IDs")


class RuntimeConfigStore:
    """Change default profile, profile settings, and source routes in place."""

    def __init__(self, config_path: Path, *, base_dir: Path):
        self.config_path = Path(config_path)
        self.base_dir = Path(base_dir)

    def read(self) -> ConfigDocument:
        try:
            content = self.config_path.read_bytes()
        except FileNotFoundError as exc:
            raise ConsoleError(404, "Configuration file not found") from exc
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConsoleError(400, "Configuration file is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ConsoleError(400, "Configuration file must contain a JSON object")
        return ConfigDocument(raw, hashlib.sha256(content).hexdigest(), self._validate(raw))

    def profile_store(self, document: ConfigDocument) -> ProfileStore:
        processing = document.config.processing
        return ProfileStore.from_profiles_dir(
            processing.profiles_dir,
            base_dir=self.base_dir,
            default_profile=processing.default_profile,
        )

    def snapshot(self) -> dict[str, Any]:
        document = self.read()
        registry = self.profile_store(document).load_registry()
        effective = dict(iter_route_targets(document.config.sources.model_dump(mode="json")))
        sources = []
        for key, entry in iter_route_targets(document.raw.get("sources")):
            resolved = effective.get(key, entry)
            sources.append(
                {
                    "key": key,
                    "source": key.split(".", 1)[0],
                    "label": _label(key, entry),
                    "enabled": resolved.get("enabled", True) is not False,
                    "route": _parse_route(resolved.get("profile")),
                }
            )
        processing = document.config.processing
        return {
            "revision": document.revision,
            "default_profile": processing.default_profile,
            "profile_settings": {
                profile_id: settings.model_dump()
                for profile_id, settings in processing.profile_settings.items()
            },
            "profiles": [profile.id for profile in registry.profiles],
            "sources": sources,
        }

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        document = self.read()
        if payload.get("revision") != document.revision:
            raise ConsoleError(
                409, "The configuration changed since it was loaded; reload it and try again"
            )
        raw = copy.deepcopy(document.raw)
        if "default_profile" in payload:
            self._apply_default_profile(raw, document, payload["default_profile"])
        if "profile_settings" in payload:
            self._apply_profile_settings(raw, document, payload["profile_settings"])
        if "routes" in payload:
            self._apply_routes(raw, document, payload["routes"])
        if raw != document.raw:
            config = self._validate(raw)
            registry = ProfileStore.from_profiles_dir(
                config.processing.profiles_dir,
                base_dir=self.base_dir,
                default_profile=config.processing.default_profile,
            ).load_registry()
            try:
                registry.validate_config_references(config)
            except ValueError as exc:
                raise ConsoleError(400, "Configuration references are invalid", [str(exc)]) from exc
            self._write(raw, document.revision)
        return self.snapshot()

    def references(self, document: ConfigDocument, profile_id: str) -> list[str]:
        """List configuration locations that name ``profile_id``."""
        config = document.config
        found = []
        if config.processing.default_profile == profile_id:
            found.append("processing.default_profile")
        if profile_id in config.processing.profile_settings:
            found.append(f"processing.profile_settings.{profile_id}")
        if profile_id in config.digest.profile_order:
            found.append("digest.profile_order")
        if config.digest.group_by == "profile":
            found.extend(
                f"digest.category_groups.{name}"
                for name, group in config.digest.category_groups.items()
                if profile_id in group.categories
            )
        labels = {
            key: _label(key, entry) for key, entry in iter_route_targets(document.raw.get("sources"))
        }
        for key, entry in iter_route_targets(config.sources.model_dump(mode="json")):
            route = _parse_route(entry.get("profile"))
            if profile_id == route or (isinstance(route, list) and profile_id in route):
                label = labels.get(key)
                found.append(f"sources.{key} ({label})" if label else f"sources.{key}")
        return found

    @staticmethod
    def _validate(raw: dict[str, Any]) -> Config:
        try:
            return Config.model_validate(_expand_env_vars(raw))
        except ValidationError as exc:
            raise ConsoleError(
                400, "Configuration is invalid", describe_validation_error(exc)
            ) from exc

    @staticmethod
    def _apply_default_profile(
        raw: dict[str, Any], document: ConfigDocument, value: object
    ) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ConsoleError(400, "default_profile must be a profile ID")
        if value != document.config.processing.default_profile:
            raw.setdefault("processing", {})["default_profile"] = value

    @staticmethod
    def _apply_profile_settings(
        raw: dict[str, Any], document: ConfigDocument, desired: object
    ) -> None:
        if not isinstance(desired, dict):
            raise ConsoleError(400, "profile_settings must be an object keyed by profile ID")
        current = document.config.processing.profile_settings
        current_raw = (raw.get("processing") or {}).get("profile_settings") or {}
        updated = dict(current_raw)
        for profile_id, value in desired.items():
            try:
                settings = ProfileSettingsConfig.model_validate(value)
            except ValidationError as exc:
                raise ConsoleError(
                    400,
                    f"Invalid settings for profile {profile_id}",
                    describe_validation_error(exc),
                ) from exc
            if settings == ProfileSettingsConfig():
                # Defaults need no entry, and a stale entry still references the profile.
                updated.pop(profile_id, None)
            elif current.get(profile_id) != settings:
                updated[profile_id] = settings.model_dump()
        if updated != current_raw:
            raw.setdefault("processing", {})["profile_settings"] = updated

    @staticmethod
    def _apply_routes(raw: dict[str, Any], document: ConfigDocument, routes: object) -> None:
        if not isinstance(routes, dict):
            raise ConsoleError(400, "routes must be an object keyed by source location")
        targets = dict(iter_route_targets(raw.get("sources")))
        effective = dict(iter_route_targets(document.config.sources.model_dump(mode="json")))
        for key, value in routes.items():
            if key not in targets:
                raise ConsoleError(400, f"Unknown source location: {key}")
            route = _parse_route(value)
            if route == _parse_route(effective.get(key, targets[key]).get("profile")):
                continue
            if route is None:
                targets[key].pop("profile", None)
            else:
                targets[key]["profile"] = route

    def _write(self, raw: dict[str, Any], revision: str) -> None:
        path = self.config_path.resolve()
        if hashlib.sha256(path.read_bytes()).hexdigest() != revision:
            raise ConsoleError(
                409, "The configuration changed since it was loaded; reload it and try again"
            )
        _atomic_write_text(
            path,
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
            mode=stat.S_IMODE(path.stat().st_mode),
        )
