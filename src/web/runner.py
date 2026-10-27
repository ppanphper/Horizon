"""Run the Horizon pipeline as a child process with bounded, redacted logs."""

import os
import re
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .errors import ConsoleError

MAX_LOG_LINES = 300
MAX_LINE_CHARS = 2000
REDACTED = "<redacted>"

_SENSITIVE_ENV_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|CREDENTIAL|AUTH|SESSION|WEBHOOK|DSN|PRIVATE",
    re.IGNORECASE,
)
_MIN_SECRET_CHARS = 6
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_USERINFO = re.compile(r"(?<=://)[^/\s@]+@")
_SENSITIVE_QUERY = re.compile(
    r"([?&][^=&\s]*(?:key|token|secret|password|signature|sig|auth)[^=&\s]*=)[^&\s#]+",
    re.IGNORECASE,
)
_SENSITIVE_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)(\s*[:=]\s*)\S.*",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+\S+")
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:^|[-_])(authorization|cookie|credential|key|password|secret|signature|token|api[-_]?key)(?:$|[-_])",
    re.IGNORECASE,
)


def config_secret_values(config: object, environ: Mapping[str, str]) -> set[str]:
    """Collect configuration values that may be secret.

    These are environment values referenced through ``${VAR}`` or ``*_env``
    settings, and plain strings stored under credential-like keys.
    """
    values: set[str] = set()

    def visit(value: object, key: str) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            names = _ENV_REFERENCE.findall(value)
            if key.endswith("_env"):
                names.append(value)
            elif _SENSITIVE_CONFIG_KEY.search(key):
                values.add(value)
            values.update(environ[name] for name in names if environ.get(name))

    visit(config, "")
    return values


def build_redactor(
    environ: Mapping[str, str], extra_secrets: Iterable[str] = ()
) -> Callable[[str], str]:
    """Return a function that masks secret values and URL credentials."""
    candidates = {
        value for name, value in environ.items() if _SENSITIVE_ENV_NAME.search(name)
    }
    candidates.update(extra_secrets)
    secrets = sorted(
        (value for value in candidates if len(value) >= _MIN_SECRET_CHARS),
        key=len,
        reverse=True,
    )

    def redact(text: str) -> str:
        text = _ANSI_ESCAPE.sub("", text)
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        text = _URL_USERINFO.sub(f"{REDACTED}@", text)
        text = _SENSITIVE_QUERY.sub(rf"\1{REDACTED}", text)
        text = _SENSITIVE_HEADER.sub(rf"\1\2{REDACTED}", text)
        return _BEARER.sub(f"Bearer {REDACTED}", text)

    return redact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PipelineRunner:
    """Start one pipeline process at a time and keep its recent output."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environ: Optional[Mapping[str, str]] = None,
        max_lines: int = MAX_LOG_LINES,
    ):
        self.command = list(command)
        self.cwd = Path(cwd)
        self._environ = dict(os.environ if environ is None else environ)
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self._state = "idle"
        self._exit_code: Optional[int] = None
        self._started_at: Optional[str] = None
        self._finished_at: Optional[str] = None

    def is_running(self) -> bool:
        with self._lock:
            return self._state == "running"

    def start(self, extra_secrets: Iterable[str] = ()) -> dict[str, Any]:
        """Start a run whose log also masks ``extra_secrets``."""
        with self._lock:
            if self._state == "running":
                raise ConsoleError(409, "A pipeline run is already in progress")
            redact = build_redactor(self._environ, extra_secrets)
            try:
                process = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    env={**self._environ, "PYTHONUNBUFFERED": "1"},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError as exc:
                raise ConsoleError(500, "The pipeline could not be started") from exc
            self._lines.clear()
            self._state = "running"
            self._exit_code = None
            self._started_at = _now()
            self._finished_at = None
        threading.Thread(target=self._collect, args=(process, redact), daemon=True).start()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "exit_code": self._exit_code,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "log": list(self._lines),
            }

    def _collect(self, process: subprocess.Popen, redact: Callable[[str], str]) -> None:
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                text = redact(line.rstrip("\r\n"))
                if len(text) > MAX_LINE_CHARS:
                    text = f"{text[:MAX_LINE_CHARS]}…"
                with self._lock:
                    self._lines.append(text)
        exit_code = process.wait()
        with self._lock:
            self._exit_code = exit_code
            self._state = "succeeded" if exit_code == 0 else "failed"
            self._finished_at = _now()
