"""Exercise Responses through the real SDK with an in-memory HTTP transport."""

import asyncio
import json

import httpx
from openai import AsyncOpenAI, BadRequestError
from pydantic import ValidationError
import pytest

from src.ai import client as clients
from src.ai.tokens import get_usage_snapshot, reset_usage
from src.models import AIConfig, AIProvider


def config(**overrides):
    fields = dict(provider="openai", api_protocol="responses", model="custom-model",
                  api_key_env="TEST_API_KEY", base_url="https://gateway.example/v1",
                  max_tokens=2048, temperature=.3)
    fields.update(overrides)
    return AIConfig(**fields)


def response_payload(**overrides):
    payload = {
        "id": "resp_test", "object": "response", "created_at": 1,
        "status": "completed", "model": "custom-model", "error": None,
        "output": [{"type": "message", "id": "msg_test", "status": "completed",
                    "role": "assistant", "content": [
                        {"type": "output_text", "text": '{"score":', "annotations": []},
                        {"type": "output_text", "text": '8}', "annotations": []}]}],
        "usage": {"input_tokens": 19, "output_tokens": 7, "total_tokens": 26},
    }
    payload.update(overrides)
    return payload


def install_transport(monkeypatch, handler):
    monkeypatch.setenv("TEST_API_KEY", "fictional-test-key")
    def factory(**kwargs):
        return AsyncOpenAI(**kwargs, max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(clients, "AsyncOpenAI", factory)


def test_responses_request_schema_endpoint_output_and_usage(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response_payload())
    install_transport(monkeypatch, handler)
    reset_usage()
    async def run():
        client = clients.create_ai_client(config())
        try:
            return await client.complete("Return JSON.", "Score this news.", temperature=.7, max_tokens=256)
        finally:
            await client.client.close()
    assert asyncio.run(run()) == '{"score":8}'
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://gateway.example/v1/responses"
    body = json.loads(seen[0].content)
    assert body == {
        "model": "custom-model",
        "input": [{"role": "system", "content": "Return JSON."},
                  {"role": "user", "content": "Score this news."}],
        "max_output_tokens": 256, "temperature": .7,
        "text": {"format": {"type": "json_object"}}, "store": False,
    }
    usage = get_usage_snapshot()
    assert usage.total_input_tokens == 19 and usage.total_output_tokens == 7
    reset_usage()


def test_temperature_rejection_retries_same_protocol_and_is_remembered(monkeypatch):
    bodies = []
    def handler(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: temperature", "type": "invalid_request_error"}})
        return httpx.Response(200, json=response_payload())
    install_transport(monkeypatch, handler)
    async def run():
        client = clients.create_ai_client(config())
        try:
            for _ in range(2):
                assert await client.complete("JSON", "news") == '{"score":8}'
        finally:
            await client.client.close()
    asyncio.run(run())
    assert len(bodies) == 3
    assert "temperature" in bodies[0]
    assert all("temperature" not in body for body in bodies[1:])


@pytest.mark.parametrize("payload, message", [
    (response_payload(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), "max_output_tokens"),
    (response_payload(status="failed", error={"code": "server_error", "message": "failed"}), "failed"),
    (response_payload(output=[]), "Empty response"),
    (response_payload(output=[{"type": "message", "id": "msg_refused", "role": "assistant", "status": "completed",
                              "content": [{"type": "refusal", "refusal": "No output"}]}]), "Empty response"),
])
def test_invalid_outputs_are_not_silently_used_as_news(monkeypatch, payload, message):
    install_transport(monkeypatch, lambda _: httpx.Response(200, json=payload))
    async def run():
        client = clients.create_ai_client(config())
        try:
            with pytest.raises(ValueError, match=message):
                await client.complete("JSON", "news")
        finally:
            await client.client.close()
    asyncio.run(run())


def test_other_request_error_does_not_switch_protocol_or_drop_fields(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": {"message": "model does not exist", "type": "invalid_request_error"}})
    install_transport(monkeypatch, handler)
    async def run():
        client = clients.create_ai_client(config())
        try:
            with pytest.raises(BadRequestError):
                await client.complete("JSON", "news")
        finally:
            await client.client.close()
    asyncio.run(run())
    assert calls == ["/v1/responses"]


def test_old_config_keeps_chat_completions_endpoint(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "chat_test", "object": "chat.completion", "created": 1,
                                       "model": "custom-model", "choices": [{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": "{}"}}]})
    install_transport(monkeypatch, handler)
    old = config().model_dump()
    del old["api_protocol"]
    parsed = AIConfig.model_validate(old)
    assert parsed.api_protocol == "chat_completions"
    async def run():
        client = clients.create_ai_client(parsed)
        try:
            assert await client.complete("JSON", "news") == "{}"
        finally:
            await client.client.close()
    asyncio.run(run())
    assert calls[0].url.path == "/v1/chat/completions"
    assert "messages" in json.loads(calls[0].content)
    assert "input" not in json.loads(calls[0].content)


def test_responses_protocol_is_kept_only_for_primary_chain_provider():
    chain = clients._create_chained_client(config(provider_chain="openai,ollama"))
    assert chain.configs[0].api_protocol == "responses"
    assert chain.configs[1].api_protocol == "chat_completions"


@pytest.mark.parametrize("overrides", [
    {"provider": AIProvider.ANTHROPIC}, {"provider": AIProvider.AZURE},
    {"api_protocol": "unknown"}, {"max_tokens": 15},
])
def test_invalid_protocol_combination_rejected(overrides):
    with pytest.raises(ValidationError):
        config(**overrides)
