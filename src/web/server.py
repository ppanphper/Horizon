"""Local-only configuration console. Run with ``python -m src.web.server``."""

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit, unquote

from dotenv import dotenv_values
from pydantic import ValidationError

from .._file_utils import _atomic_write_text
from ..models import Config
from ..processing.profiles import ProfileRegistry
from .reader import render_digest
from .errors import ConsoleError
from .integration import LocalProfileService


STATIC = Path(__file__).parent / "static"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SUMMARY_NAME = re.compile(r"horizon-[0-9]{4}-[0-9]{2}-[0-9]{2}-[A-Za-z0-9_-]+\.md")


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ConsoleState:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config_path = self.root / "data/config.json"
        self.env_path = self.root / ".env"
        self.summaries = self.root / "data/summaries"
        self.summaries.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.process = None
        self.logs = deque(maxlen=600)
        self.run = {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None}
        self.profiles_service = LocalProfileService(self)

    def environment(self):
        env = dict(os.environ)
        env.update({k: v for k, v in dotenv_values(self.env_path).items() if v is not None})
        return env

    def revision(self):
        return hashlib.sha256(self.config_path.read_bytes() + b"\0" +
                              (self.env_path.read_bytes() if self.env_path.exists() else b"")).hexdigest()

    @staticmethod
    def expand(value, env):
        if isinstance(value, str):
            return ENV_REF.sub(lambda m: env.get(m[1], m[0]), value)
        if isinstance(value, dict):
            return {k: ConsoleState.expand(v, env) for k, v in value.items()}
        if isinstance(value, list):
            return [ConsoleState.expand(v, env) for v in value]
        return value

    def validate(self, raw, env):
        try:
            config = Config.model_validate(self.expand(raw, env))
            profiles = ProfileRegistry.load(Path(config.processing.profiles_dir),
                                           config.processing.default_profile, base_dir=self.root)
            profiles.validate_config_references(config)
            for name in set(config.processing.profile_settings) | set(config.digest.profile_order):
                profiles.get(name)
            if not config.ai.languages:
                raise ValueError("至少选择一种日报语言")
            if not 1 <= config.collection.time_window_hours <= 720:
                raise ValueError("抓取时间范围应为 1–720 小时")
            if not 1 <= config.ai.max_tokens <= 131072:
                raise ValueError("max_tokens 应为 1–131072")
            if not 1 <= config.ai.analysis_concurrency <= 16 or not 1 <= config.ai.enrichment_concurrency <= 16:
                raise ValueError("AI 并发数应为 1–16")
            for source in config.sources.rss:
                if source.enabled and not source.name.strip():
                    raise ValueError("启用的 RSS 订阅必须填写名称")
            for source in config.sources.github:
                if not source.enabled:
                    continue
                if source.type == "repo_releases" and (not source.owner or not source.repo):
                    raise ValueError("GitHub Release 来源必须填写仓库所有者和仓库名称")
                if source.type == "user_events" and not source.username:
                    raise ValueError("GitHub 用户动态必须填写用户名")
            hn = config.sources.hackernews
            if hn.enabled and (not 1 <= hn.fetch_top_stories <= 500 or hn.min_score < 0):
                raise ValueError("Hacker News 条数应为 1–500，最低分数不能小于 0")
            if config.ai.api_key_env and not ENV_NAME.fullmatch(config.ai.api_key_env):
                raise ValueError("api_key_env 必须是环境变量名，密钥请填入专用输入框")
            if config.ai.base_url and not ENV_REF.search(config.ai.base_url):
                url = urlsplit(config.ai.base_url)
                if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                    raise ValueError("API 地址必须是 HTTP(S) 地址，不能在地址中嵌入凭证")
            return config, profiles
        except ValidationError as exc:
            # Do not echo input values (they can contain a key or private URL).
            fields = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            raise RequestError("配置字段格式不正确：" + "、".join(fields)) from exc
        except (ValueError, OSError) as exc:
            raise RequestError(str(exc)) from exc

    def read(self):
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError) as exc:
            raise RequestError("无法读取 data/config.json，请检查文件是否存在且为有效 JSON", 422) from exc

    def readiness(self, config, env):
        missing = []
        if not config.ai.model.strip() or ENV_REF.search(config.ai.model):
            missing.append("模型名称")
        if config.ai.base_url and ENV_REF.search(config.ai.base_url):
            missing.append("API 地址")
        if config.ai.provider.value == "openai-chatgpt":
            from ..ai.chatgpt_auth import ChatGPTAuth
            from ..ai.errors import AIProviderUnavailableError

            directory = env.get("HORIZON_CHATGPT_AUTH_DIR")
            try:
                session = ChatGPTAuth(Path(directory).expanduser() if directory else None).status(
                    config.ai.chatgpt_account
                )
                connected = session.get("connected", False) and session.get("plan_usage", False)
            except (AIProviderUnavailableError, OSError):
                connected = False
            if not connected:
                missing.append("ChatGPT 登录（horizon-chatgpt login）")
        elif config.ai.provider.value != "ollama" and not env.get(config.ai.api_key_env, "").strip():
            missing.append("API 密钥")
        if config.ai.provider.value == "azure" and not env.get(config.ai.azure_endpoint_env or "AZURE_OPENAI_ENDPOINT"):
            missing.append("Azure endpoint 环境变量")
        return missing

    def snapshot(self):
        with self.lock:
            raw = self.read()
            env = self.environment()
            config, profiles = self.validate(raw, env)
            return {
                "config": raw, "revision": self.revision(), "csrf_token": self.token,
                "ai": {"model": config.ai.model, "base_url": config.ai.base_url or "",
                       "key_configured": bool(env.get(config.ai.api_key_env, ""))},
                "profiles": [{"id": p.id, "name": p.definition.display_names.get("zh", p.definition.name)}
                             for p in profiles.profiles],
                "missing": self.readiness(config, env),
                "run": self.run_snapshot(), "summaries": self.list_summaries(),
            }

    def save(self, payload):
        with self.lock:
            if self.run["status"] in {"running", "stopping"}:
                raise RequestError("正在生成日报，请等待结束后保存配置", 409)
            if payload.get("revision") != self.revision():
                raise RequestError("配置已被其他页面或本地文件修改。请重新加载后再保存。", 409)
            raw = payload.get("config")
            env = self.environment()
            config, _ = self.validate(raw, env)
            key = payload.get("api_key", "")
            if not isinstance(key, str) or len(key) > 8192 or "\n" in key or "\r" in key:
                raise RequestError("API 密钥格式不正确")
            key = key.strip()
            # Only a dedicated API-key variable can be edited through this field.
            previous = None
            if key or payload.get("clear_key"):
                name = config.ai.api_key_env
                if not name or not name.endswith(("API_KEY", "TOKEN")):
                    raise RequestError("保存密钥时，api_key_env 必须以 API_KEY 或 TOKEN 结尾")
                previous = self.env_path.read_text() if self.env_path.exists() else ""
                quoted = "'" + key.replace("\\", "\\\\").replace("'", "\\'") + "'"
                replacement = name + "=" + quoted
                pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=.*$", re.M)
                updated = pattern.sub(lambda _: replacement, previous) if pattern.search(previous) else previous.rstrip() + "\n" + replacement + "\n"
            else:
                updated = None
            # Serialize and back up before writing. Bind-mounted .env must retain its inode.
            serialized = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
            _atomic_write_text(self.config_path.with_suffix(".json.web-backup"), self.config_path.read_text())
            if updated is not None:
                with self.env_path.open("w", encoding="utf-8") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.env_path.chmod(0o600)
            try:
                _atomic_write_text(self.config_path, serialized)
            except OSError:
                if previous is not None:
                    with self.env_path.open("w", encoding="utf-8") as stream:
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
                raise
            return self.snapshot()

    def list_summaries(self):
        if not self.summaries.exists():
            return []
        return [{"name": p.name, "size": p.stat().st_size}
                for p in sorted(self.summaries.glob("horizon-*.md"), reverse=True)
                if p.is_file() and not p.is_symlink() and SUMMARY_NAME.fullmatch(p.name)][:100]

    def read_summary(self, name, *, render=True):
        if not SUMMARY_NAME.fullmatch(name):
            raise RequestError("日报文件名无效", 404)
        path = self.summaries / name
        if not path.is_file() or path.is_symlink():
            raise RequestError("日报不存在", 404)
        if path.stat().st_size > 2_000_000:
            raise RequestError("文件较大，请在本地编辑器打开", 413)
        try:
            content = path.read_bytes().decode("utf-8")
        except UnicodeError as exc:
            raise RequestError("日报文件不是有效的 UTF-8 文本", 422) from exc
        result = {"name": name, "markdown": content}
        if render:
            result.update(render_digest(content))
        return result

    def run_snapshot(self):
        with self.lock:
            return {**self.run, "logs": list(self.logs)}

    def start(self):
        with self.lock:
            if self.run["status"] in {"running", "stopping"}:
                raise RequestError("已有一个日报任务正在运行", 409)
            env = self.environment()
            config, _ = self.validate(self.read(), env)
            missing = self.readiness(config, env)
            if missing:
                raise RequestError("请先配置并保存：" + "、".join(missing), 422)
            env.update(PYTHONUNBUFFERED="1", NO_COLOR="1", TERM="dumb")
            process = subprocess.Popen(
                [sys.executable, "-m", "src.main", "--hours", str(config.collection.time_window_hours)],
                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
            self.process = process
            self.logs.clear()
            self.run = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": None, "exit_code": None}
            redactions = sorted({v for k, v in env.items() if v and len(v) >= 4 and
                                 (any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")) or k == config.ai.api_key_env)}, key=len, reverse=True)
            threading.Thread(target=self._collect, args=(process, redactions), daemon=True).start()
            return self.run_snapshot()

    def _collect(self, process, redactions):
        try:
            for line in process.stdout:
                line = ANSI.sub("", line).rstrip()
                for secret in redactions:
                    line = line.replace(secret, "[已隐藏]")
                with self.lock:
                    self.logs.append(line[-16000:])
        finally:
            code = process.wait()
            with self.lock:
                stopped = self.run["status"] == "stopping"
                self.run.update(status="stopped" if stopped else ("success" if code == 0 else "failed"),
                                exit_code=code, finished_at=datetime.now(timezone.utc).isoformat())
            if process.stdout:
                process.stdout.close()

    def stop(self):
        with self.lock:
            process = self.process
            if process and process.poll() is None:
                self.run["status"] = "stopping"
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                def kill_later():
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                threading.Thread(target=kill_later, daemon=True).start()
            return self.run_snapshot()


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, state):
        self.state = state
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "HorizonLocal"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass

    def respond(self, code, data, content_type="application/json; charset=utf-8", headers=None):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def check_origin(self, write=False):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            valid = parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port == self.server.server_address[1]
            # Docker publishes a different host port from the container's port.
            public_port = int(os.environ.get("HORIZON_WEB_PUBLIC_PORT", self.server.server_address[1]))
            valid = valid or (parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port == public_port)
        except ValueError:
            valid = False
        origin = self.headers.get("Origin")
        if not valid or (origin and origin != "http://" + host) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError("仅接受本机同源请求", 403)
        if write and not secrets.compare_digest(self.headers.get("X-Horizon-Token", ""), self.server.state.token):
            raise RequestError("页面已过期，请刷新后重试", 403)

    def handle_error(self, exc):
        if isinstance(exc, ConsoleError):
            self.respond(exc.status, exc.to_dict())
        elif isinstance(exc, RequestError):
            self.respond(exc.status, {"error": str(exc)})
        else:
            self.respond(500, {"error": "操作失败，请检查配置文件格式、目录权限或重启服务"})

    def do_GET(self):
        try:
            self.check_origin()
            path = unquote(urlsplit(self.path).path)
            state = self.server.state
            if path == "/api/state":
                self.respond(200, state.snapshot())
            elif path == "/api/profiles":
                self.respond(200, state.profiles_service.list_profiles())
            elif re.fullmatch(r"/api/profiles/[^/]+", path):
                self.respond(200, state.profiles_service.get_profile(path.rsplit("/", 1)[1]))
            elif path == "/api/profile-routing":
                self.respond(200, state.profiles_service.get_config())
            elif path == "/api/run":
                self.respond(200, state.run_snapshot())
            elif path == "/api/summaries":
                self.respond(200, state.list_summaries())
            elif path.startswith("/api/summaries/") and path.endswith("/download"):
                name = path[len("/api/summaries/"):-len("/download")]
                summary = state.read_summary(name, render=False)
                self.respond(200, summary["markdown"].encode("utf-8"), "text/markdown; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{name}"'})
            elif path.startswith("/api/summaries/"):
                self.respond(200, state.read_summary(path[len("/api/summaries/"):]))
            elif path == "/api/health":
                self.respond(200, {"status": "ok"})
            elif path in ("/", "/app.js", "/style.css"):
                file, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                              "/style.css": ("style.css", "text/css")}[path]
                self.respond(200, (STATIC / file).read_bytes(), mime + "; charset=utf-8")
            else:
                raise RequestError("页面不存在", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.handle_error(exc)

    def do_PUT(self):
        self.do_POST()

    def do_DELETE(self):
        self.do_POST()

    def do_POST(self):
        try:
            self.check_origin(write=True)
            if self.headers.get_content_type() != "application/json":
                raise RequestError("请求必须使用 JSON", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_000_000:
                    raise RequestError("请求体大小无效", 413)
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise RequestError("无效 JSON") from exc
            path = urlsplit(self.path).path
            state = self.server.state
            service = state.profiles_service
            profile = re.fullmatch(r"/api/profiles/([^/]+)", path)
            copy = re.fullmatch(r"/api/profiles/([^/]+)/copy", path)
            if self.command == "POST" and path == "/api/profiles":
                self.respond(201, service.create_profile(payload))
            elif self.command == "POST" and copy:
                self.respond(201, service.copy_profile(copy[1], payload))
            elif self.command == "PUT" and profile:
                self.respond(200, service.update_profile(profile[1], payload))
            elif self.command == "DELETE" and profile:
                self.respond(200, service.delete_profile(profile[1], payload))
            elif self.command == "PUT" and path == "/api/profile-routing":
                self.respond(200, service.update_config(payload))
            elif self.command != "POST":
                raise RequestError("接口不存在", 404)
            elif path == "/api/validate":
                env = state.environment()
                config, _ = state.validate(payload.get("config"), env)
                self.respond(200, {"model": config.ai.model, "base_url": config.ai.base_url or "",
                                   "key_configured": bool(env.get(config.ai.api_key_env, ""))})
            elif path == "/api/config":
                self.respond(200, state.save(payload))
            elif path == "/api/run":
                self.respond(202, state.start())
            elif path == "/api/run/stop":
                self.respond(200, state.stop())
            else:
                raise RequestError("接口不存在", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.handle_error(exc)


def main():
    parser = argparse.ArgumentParser(description="Horizon local Web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    state = ConsoleState(args.root)
    server = ConsoleServer((args.host, args.port), state)
    print(f"Horizon local console listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        server.server_close()


if __name__ == "__main__":
    main()
