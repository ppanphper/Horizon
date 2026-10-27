"""Local web console for Horizon profiles and runtime routing.

Run with ``python -m src.web.server``. The console binds to a loopback address
only and has no authentication, so it must not be exposed to other hosts.
"""

import argparse
import hmac
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv
from rich.console import Console

from .._cli import add_data_dir_arguments, add_log_level_argument
from ..logging_config import configure_logging
from .errors import ConsoleError
from .runner import PipelineRunner
from .service import ConsoleService

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 1_048_576
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
JSON_TYPE = "application/json; charset=utf-8"
STATIC_DIR = Path(__file__).resolve().parent / "profile_static"
STATIC_FILES = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

_PROFILE_PATH = re.compile(r"/api/profiles/([^/]+)")
_PROFILE_COPY_PATH = re.compile(r"/api/profiles/([^/]+)/copy")
_SUMMARY_PATH = re.compile(r"/api/summaries/([^/]+)")


def _json_body(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def allowed_hosts(bound_host: str, port: int) -> set[str]:
    """Host header values that address this console."""
    bound = f"[{bound_host}]" if ":" in bound_host else bound_host
    return {f"{name}:{port}" for name in ("127.0.0.1", "localhost", "[::1]", bound)}


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ConsoleServer(ThreadingHTTPServer):
    """HTTP server that only answers requests addressed to its loopback origin."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: ConsoleService,
        *,
        csrf_token: Optional[str] = None,
    ):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, ConsoleRequestHandler)
        self.service = service
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        host, port = self.server_address[:2]
        self.allowed_hosts = allowed_hosts(host, port)


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleServer
    server_version = "HorizonConsole"
    sys_version = ""
    # Close connections that stop sending a declared request body.
    timeout = 30

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def _dispatch(self, method: str) -> None:
        try:
            self._check_host()
            path = urlsplit(self.path).path
            if method == "GET":
                status, body, content_type = self._handle_get(path)
            else:
                self._check_write_origin()
                status, payload = self._handle_write(method, path, self._read_json())
                body, content_type = _json_body(payload), JSON_TYPE
        except ConsoleError as exc:
            status, body, content_type = exc.status, _json_body(exc.to_dict()), JSON_TYPE
        except Exception:
            logger.exception("Console request failed")
            status = 500
            body, content_type = _json_body({"error": "Internal server error"}), JSON_TYPE
        self._send(status, body, content_type)

    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.allowed_hosts:
            raise ConsoleError(403, "Requests must use the console's local address")

    def _check_write_origin(self) -> None:
        expected_origin = f"http://{(self.headers.get('Host') or '').lower()}"
        if (self.headers.get("Origin") or "").lower() != expected_origin:
            raise ConsoleError(403, "Cross-origin requests are not allowed")
        token = (self.headers.get("X-CSRF-Token") or "").encode("utf-8")
        if not hmac.compare_digest(token, self.server.csrf_token.encode("utf-8")):
            raise ConsoleError(403, "Missing or invalid CSRF token; reload the console")

    def _read_json(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            raise ConsoleError(415, "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError as exc:
            raise ConsoleError(411, "Content-Length is required") from exc
        if length < 0:
            raise ConsoleError(400, "Invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ConsoleError(413, "Request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ConsoleError(400, "Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ConsoleError(400, "Request body must be a JSON object")
        return payload

    def _handle_get(self, path: str) -> tuple[int, bytes, str]:
        service = self.server.service
        if path == "/":
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            page = page.replace("{{CSRF_TOKEN}}", html.escape(self.server.csrf_token))
            return 200, page.encode("utf-8"), "text/html; charset=utf-8"
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            return 200, (STATIC_DIR / name).read_bytes(), content_type

        if path == "/api/profiles":
            payload = service.list_profiles()
        elif match := _PROFILE_PATH.fullmatch(path):
            payload = service.get_profile(match[1])
        elif path == "/api/config":
            payload = service.get_config()
        elif path == "/api/run":
            payload = service.run_status()
        elif path == "/api/summaries":
            payload = service.list_summaries()
        elif match := _SUMMARY_PATH.fullmatch(path):
            payload = service.get_summary(match[1])
        else:
            raise ConsoleError(404, "Not found")
        return 200, _json_body(payload), JSON_TYPE

    def _handle_write(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        service = self.server.service
        if method == "POST" and path == "/api/profiles":
            return 201, service.create_profile(payload)
        if method == "POST" and (match := _PROFILE_COPY_PATH.fullmatch(path)):
            return 201, service.copy_profile(match[1], payload)
        if method == "PUT" and (match := _PROFILE_PATH.fullmatch(path)):
            return 200, service.update_profile(match[1], payload)
        if method == "DELETE" and (match := _PROFILE_PATH.fullmatch(path)):
            return 200, service.delete_profile(match[1], payload)
        if method == "PUT" and path == "/api/config":
            return 200, service.update_config(payload)
        if method == "POST" and path == "/api/run":
            return 200, service.start_run()
        raise ConsoleError(404, "Not found")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Horizon local console for profiles and runtime routing"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback address to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind (default: {DEFAULT_PORT})",
    )
    add_data_dir_arguments(parser)
    add_log_level_argument(parser)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not is_loopback_host(args.host):
        parser.error("--host must be a loopback address; the console has no authentication")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    data_dir = Path(args.data_dir)
    config_path = Path(args.config) if args.config else data_dir / "config.json"
    if not config_path.is_file():
        print(f"Configuration file not found: {config_path}", file=sys.stderr)
        return 1
    configure_logging(Console(stderr=True), level=args.log_level)
    load_dotenv()

    base_dir = Path.cwd()
    runner = PipelineRunner(
        [sys.executable, "-m", "src.main", "--data-dir", str(data_dir), "--config", str(config_path)],
        cwd=base_dir,
    )
    service = ConsoleService(
        config_path,
        base_dir=base_dir,
        summaries_dir=data_dir / "summaries",
        runner=runner,
    )
    server = ConsoleServer((args.host, args.port), service)
    host, port = server.server_address[:2]
    display_host = f"[{host}]" if ":" in host else host
    print(f"Horizon console: http://{display_host}:{port}/ (press Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
