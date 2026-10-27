import pytest

from src.ai.chatgpt_auth import ChatGPTAuth
from src.models import Config
from src.web.server import ConsoleState, STATIC


@pytest.mark.parametrize("connected", [True, False])
def test_chatgpt_readiness_uses_account_not_api_key(tmp_path, monkeypatch, connected):
    monkeypatch.setattr(ChatGPTAuth, "status", lambda self, account=None: {"connected": connected})
    config = Config.model_validate({"ai": {"provider": "openai-chatgpt", "model": "gpt-5"}, "sources": {}})
    missing = ConsoleState(tmp_path).readiness(config, {})
    assert "API 密钥" not in missing
    assert bool(missing) is not connected
    if not connected:
        assert "horizon-chatgpt login" in missing[0]


def test_chatgpt_provider_is_selectable():
    assert '<option value="openai-chatgpt">' in (STATIC / "index.html").read_text()
