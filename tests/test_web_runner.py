import sys
import time

import pytest

from src.web.errors import ConsoleError
from src.web.runner import PipelineRunner, build_redactor, config_secret_values

FAKE_SECRET = "fake-api-key-for-tests-789"


def wait_until_finished(runner, timeout=20):
    deadline = time.monotonic() + timeout
    while runner.is_running():
        if time.monotonic() > deadline:
            raise AssertionError("runner did not finish")
        time.sleep(0.05)
    return runner.status()


def python_command(source):
    return [sys.executable, "-c", source]


def test_run_logs_are_bounded_and_redacted(tmp_path):
    source = "\n".join(
        [
            "import os",
            "for index in range(30):",
            "    print('line', index)",
            "print('key', os.environ['HORIZON_TEST_API_KEY'])",
            "print('feed https://user:pass@example.com/rss?token=abc123&page=2')",
            "print('Authorization: Bearer abc.def')",
            "print('x' * 5000)",
        ]
    )
    runner = PipelineRunner(
        python_command(source),
        cwd=tmp_path,
        environ={"HORIZON_TEST_API_KEY": FAKE_SECRET, "PATH": ""},
        max_lines=10,
    )

    runner.start()
    status = wait_until_finished(runner)

    log = "\n".join(status["log"])
    assert status["state"] == "succeeded"
    assert status["exit_code"] == 0
    assert len(status["log"]) == 10
    assert FAKE_SECRET not in log
    assert "pass@" not in log and "abc123" not in log and "abc.def" not in log
    assert "page=2" in log
    assert max(len(line) for line in status["log"]) <= 2001


def test_run_reports_failure(tmp_path):
    runner = PipelineRunner(python_command("raise SystemExit(3)"), cwd=tmp_path, environ={})

    runner.start()
    status = wait_until_finished(runner)

    assert status["state"] == "failed"
    assert status["exit_code"] == 3


def test_cannot_start_a_second_run(tmp_path):
    runner = PipelineRunner(
        python_command("import time; time.sleep(0.5)"), cwd=tmp_path, environ={}
    )
    assert runner.status()["state"] == "idle"

    runner.start()
    try:
        with pytest.raises(ConsoleError) as exc:
            runner.start()
        assert exc.value.status == 409
    finally:
        wait_until_finished(runner)


def test_extra_secrets_are_redacted_for_that_run(tmp_path):
    runner = PipelineRunner(
        python_command("print('value plain-config-secret')"), cwd=tmp_path, environ={}
    )

    runner.start(extra_secrets={"plain-config-secret"})
    status = wait_until_finished(runner)

    assert status["log"] == ["value <redacted>"]


def test_config_secret_values_follow_references_and_sensitive_keys():
    raw = {
        "ai": {"api_key_env": "MY_PROVIDER_KEY", "max_tokens": 4096},
        "sources": {"rss": [{"url": "https://example.com/${FEED_PATH}/rss"}]},
        "email": {"password": "plain-password-value"},
        "twitter": {"keywords": ["public keyword"]},
    }
    environ = {"MY_PROVIDER_KEY": "provider-key-value", "FEED_PATH": "private-path-value"}

    assert config_secret_values(raw, environ) == {
        "provider-key-value",
        "private-path-value",
        "plain-password-value",
    }


def test_redactor_ignores_short_or_unrelated_environment_values():
    redact = build_redactor({"HOME": "/home/horizon", "API_KEY": "abc", "TOKEN": FAKE_SECRET})

    assert redact(f"/home/horizon abc {FAKE_SECRET}") == "/home/horizon abc <redacted>"
