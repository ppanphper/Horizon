"""Coordinate console reads and writes with the pipeline runner."""

import json
import os
import re
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from ..processing import ProfileRegistry
from ..storage.manager import safe_output_path
from .errors import ConsoleError
from .profiles import is_regular_file
from .runner import config_secret_values
from .runtime_config import RuntimeConfigStore

MAX_SUMMARIES = 20
MAX_SUMMARY_BYTES = 2_000_000
_SUMMARY_NAME = re.compile(
    r"horizon-\d{4}-\d{2}-\d{2}-[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*\.md"
)


class Runner(Protocol):
    def is_running(self) -> bool: ...

    def start(self, extra_secrets: Iterable[str] = ()) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


class ConsoleService:
    """Apply console operations against fresh files on every request."""

    def __init__(
        self,
        config_path: Path,
        *,
        base_dir: Path,
        summaries_dir: Path,
        runner: Runner,
    ):
        self.config = RuntimeConfigStore(config_path, base_dir=base_dir)
        self.summaries_dir = Path(summaries_dir)
        self.runner = runner
        self._lock = threading.Lock()

    @contextmanager
    def _saving(self) -> Iterator[None]:
        with self._lock:
            if self.runner.is_running():
                raise ConsoleError(
                    409, "A pipeline run is in progress; save after it finishes"
                )
            yield

    def list_profiles(self) -> dict[str, Any]:
        with self._lock:
            document = self.config.read()
            return {"profiles": self.config.profile_store(document).list_profiles()}

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            document = self.config.read()
            detail = self.config.profile_store(document).get_profile(profile_id)
            return {**detail, "references": self.config.references(document, detail["id"])}

    def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._saving():
            store = self.config.profile_store(self.config.read())
            return store.create_profile(
                payload.get("id"), payload.get("definition"), payload.get("prompts")
            )

    def copy_profile(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._saving():
            store = self.config.profile_store(self.config.read())
            return store.copy_profile(source_id, payload.get("id"), payload.get("name"))

    def update_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._saving():
            store = self.config.profile_store(self.config.read())
            return store.update_profile(
                profile_id,
                revision=payload.get("revision"),
                definition=payload.get("definition"),
                prompts=payload.get("prompts"),
            )

    def delete_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._saving():
            document = self.config.read()

            def check(registry: ProfileRegistry) -> None:
                references = self.config.references(document, profile_id)
                if references:
                    raise ConsoleError(
                        409, "The profile is still used by the runtime configuration", references
                    )
                try:
                    registry.validate_config_references(document.config)
                except ValueError as exc:
                    raise ConsoleError(
                        409, "The profile is still used by the runtime configuration", [str(exc)]
                    ) from exc

            self.config.profile_store(document).delete_profile(
                profile_id, revision=payload.get("revision"), check=check
            )
            return {"deleted": profile_id}

    def get_config(self) -> dict[str, Any]:
        # Validation loads the default profile; do not observe a writer's directory swap.
        with self._lock:
            return self.config.snapshot()

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._saving():
            return self.config.update(payload)

    def start_run(self) -> dict[str, Any]:
        # Read without validation: an invalid config is when the pipeline prints values.
        try:
            raw = json.loads(self.config.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        secrets = config_secret_values(raw, os.environ)
        with self._lock:
            return self.runner.start(extra_secrets=secrets)

    def run_status(self) -> dict[str, Any]:
        return self.runner.status()

    def list_summaries(self) -> dict[str, Any]:
        if not self.summaries_dir.is_dir():
            return {"summaries": []}
        names = sorted(
            (
                path.name
                for path in self.summaries_dir.iterdir()
                if _SUMMARY_NAME.fullmatch(path.name) and is_regular_file(path)
            ),
            reverse=True,
        )
        return {"summaries": names[:MAX_SUMMARIES]}

    def get_summary(self, name: str) -> dict[str, Any]:
        if not _SUMMARY_NAME.fullmatch(name):
            raise ConsoleError(400, "Invalid summary name")
        if not is_regular_file(self.summaries_dir / name):
            raise ConsoleError(404, "Summary not found")
        path = safe_output_path(self.summaries_dir, name)
        if path.stat().st_size > MAX_SUMMARY_BYTES:
            raise ConsoleError(413, "The summary is too large to display")
        return {"name": name, "markdown": path.read_text(encoding="utf-8", errors="replace")}
