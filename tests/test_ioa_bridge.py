"""Offline iOA bridge contract tests; no SDK, iOA credentials or network calls."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ioa_bridge.catalog import ModelCatalog, parse_help_models
from ioa_bridge.core import (BridgeError, conversation_prompt, number_usage,
                             parse_tool_answer, response_usage, tool_prompt, validate_tools)
from ioa_bridge.sdk_backend import SDKBackend
from ioa_bridge.server import Settings, create_app
from ioa_bridge.sessions import SessionManager, is_single_delta


class FakeCatalog:
    def models(self):
        return ["demo-model", "demo-model-v2"]


class FakeBackend:
    def __init__(self, output="OK", *, usage=None):
        self.output = output
        self.usage = usage or {"input_tokens": 4000, "output_tokens": 2,
                               "cache_read_input_tokens": 3000,
                               "cache_creation_input_tokens": 1000}
        self.calls = []

    async def generate(self, prompt, model, *, stream, session_id=None):
        self.calls.append((prompt, model, stream, session_id))
        for char in self.output:
            yield "delta", char
        yield "result", {"text": self.output, "usage": self.usage}


def client(backend=None, *, tools=False):
    backend = backend or FakeBackend()
    settings = Settings("z" * 32, "placeholder-not-used", Path("/tmp/codebuddy-ioa-unit-tests"), tools)
    return TestClient(create_app(settings=settings, backend=backend, catalog=FakeCatalog())), backend


def headers():
    return {"Authorization": "Bearer " + "z" * 32}


def test_health_and_key_protection():
    c, _ = client()
    assert c.get("/health").json() == {"status": "ok"}
    assert c.get("/v1/models").status_code == 401
    assert c.post("/v1/responses", json={"model": "demo-model", "input": "hello"}).status_code == 401
    assert c.get("/v1/models", headers=headers()).json()["data"][0]["id"] == "demo-model"


def test_non_stream_text_usage_and_no_billing_cost():
    c, b = client()
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "Hello"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "completed"
    assert data["output"][0]["content"][0]["text"] == "OK"
    assert data["usage"]["input_tokens_details"]["cached_tokens"] == 3000
    assert data["metadata"]["ioa_cache_creation_tokens"] == "1000"
    assert "total_cost_usd" not in str(data)
    assert b.calls[0][3] is None


def test_stream_events_conform_and_close():
    c, _ = client()
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "Hi", "stream": True})
    assert r.status_code == 200
    events = [line for line in r.text.splitlines() if line.startswith("event: ")]
    names = [x.partition(": ")[2] for x in events]
    for name in ["response.created", "response.in_progress", "response.output_item.added",
                 "response.content_part.added", "response.output_text.delta",
                 "response.output_text.done", "response.content_part.done",
                 "response.output_item.done", "response.completed"]:
        assert name in names
    assert r.text.endswith("data: [DONE]\n\n")
    objs = [json.loads(line[6:]) for line in r.text.splitlines()
            if line.startswith("data: {")]
    assert objs[-1]["response"]["usage"]["total_tokens"] == 4002
    assert "OK" == "".join(obj.get("delta", "") for obj in objs if obj.get("type") == "response.output_text.delta")


def test_model_not_in_catalog_and_unsupported_input():
    c, b = client()
    unknown = c.post("/v1/responses", headers=headers(), json={"model": "fake", "input": "a"})
    assert unknown.status_code == 404
    img = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": [
        {"role": "user", "content": [{"type": "input_image", "image_url": "data:xyz"}]}
    ]})
    assert img.status_code == 422
    assert not b.calls


def test_tool_mode_fail_closed_by_default():
    c, b = client()
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "do", "tools": [
        {"type": "function", "name": "shell_command", "parameters": {"type": "object", "properties": {}}}
    ]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "tools_not_enabled"
    assert not b.calls


def test_experimental_function_call_and_handoff():
    answer = '{"kind":"tool_call","name":"shell_command","arguments":{"command":"pwd"}}'
    c, b = client(FakeBackend(answer), tools=True)
    body = {"model": "demo-model", "input": "find cwd", "tools": [
        {"type": "function", "name": "shell_command", "description": "Execute command",
         "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}
    ]}
    r = c.post("/v1/responses", headers=headers(), json=body)
    assert r.status_code == 200, r.text
    output = r.json()["output"][0]
    assert output["type"] == "function_call" and output["call_id"].startswith("call_")
    assert json.loads(output["arguments"])["command"] == "pwd"
    input2 = [
        {"type": "message", "role": "user", "content": "find cwd"},
        output,
        {"type": "function_call_output", "call_id": output["call_id"], "output": "/tmp"},
    ]
    next_c, next_b = client(FakeBackend('{"kind":"message","text":"/tmp"}'), tools=True)
    body["input"] = input2
    next_r = next_c.post("/v1/responses", headers=headers(), json=body)
    assert next_r.status_code == 200, next_r.text
    assert next_r.json()["output"][0]["content"][0]["text"] == "/tmp"
    assert "tool_result" in next_b.calls[0][0]
    assert output["call_id"] in next_b.calls[0][0]


def test_experimental_custom_tool_streamed():
    c, _ = client(FakeBackend('{"kind":"tool_call","name":"apply_patch","input":"*** Begin Patch"}'), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "patch", "stream": True,
        "tools": [{"type": "custom", "name": "apply_patch", "format": {"type": "text"}}]})
    assert r.status_code == 200
    assert "response.custom_tool_call_input.delta" in r.text
    assert '"type":"custom_tool_call"' in r.text


def test_reject_hosted_tools_or_unsafe_payload():
    c, b = client()
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "search",
                                                        "tools": [{"type": "web_search"}]})
    assert r.status_code == 422
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "hello",
                                                        "previous_response_id": "resp_secret"})
    assert r.status_code == 422 and not b.calls


def test_usage_dataclass_and_newer_dict_version():
    @dataclass
    class Usage:
        input_tokens: int = 4378
        output_tokens: int = 4
        cache_read_input_tokens: int | None = 3334
        cache_creation_input_tokens: int | None = 1039
    got = number_usage(Usage())
    assert got["cache_read_input_tokens"] == 3334
    assert response_usage(got)["total_tokens"] == 4382
    assert number_usage({"input_tokens": 20, "output_tokens": 10, "other": "private"}) == {
        "input_tokens": 20, "output_tokens": 10}
    assert number_usage({"input_tokens": True, "output_tokens": "3"}) == {}


def test_model_help_discovery_and_no_unverified_fallback():
    example = """Usage: headless [options]\n  -m, --model {model-alpha,model-beta,echo}  Select model\n  --other FLAG\n"""
    assert parse_help_models(example) == ["model-alpha", "model-beta", "echo"]
    assert parse_help_models("--model TEXT\n--tool FLAG") == []
    with patch.dict(os.environ, {"IOA_BRIDGE_MODELS": "alpha,beta,alpha,INVALID VALUE"}):
        cat = ModelCatalog()
        assert cat.models() == ["alpha", "beta"]


def test_delta_session_rotation_and_full_history_safety():
    m = SessionManager(max_turns=2, max_input_tokens=100, ttl=900)
    a, rotated = m.choose("conversation_001", "demo-model", is_delta=True)
    assert rotated
    m.record("conversation_001", {"input_tokens": 80})
    m.release("conversation_001")
    b, rotated = m.choose("conversation_001", "demo-model", is_delta=True)
    assert a == b and not rotated
    m.record("conversation_001", {"input_tokens": 101})
    m.release("conversation_001")
    c, rotated = m.choose("conversation_001", "demo-model", is_delta=True)
    assert rotated and c != a
    with pytest.raises(BridgeError):
        m.choose("conversation_001", "demo-model", is_delta=True)
    m.release("conversation_001")
    with pytest.raises(BridgeError):
        m.choose("conversation_001", "demo-model", is_delta=False)
    assert not is_single_delta([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])


def test_bridge_errors_redact_sdk_exception():
    class Broken:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("SECRET-TOKEN example.internal")
            yield "delta", "never"  # pragma: no cover
    c, _ = client(Broken())
    r = c.post("/v1/responses", headers=headers(), json={"model": "demo-model", "input": "test"})
    assert r.status_code == 502
    assert "SECRET-TOKEN" not in r.text and "example.internal" not in r.text


def test_settings_reject_wrong_environment_and_conflicting_auth():
    env = {"IOA_BRIDGE_KEY": "x" * 20, "CODEBUDDY_API_KEY": "secret", "CODEBUDDY_INTERNET_ENVIRONMENT": "ioa"}
    with patch.dict(os.environ, env, clear=True):
        assert Settings.from_env().api_key == "secret"
    with patch.dict(os.environ, {**env, "CODEBUDDY_AUTH_TOKEN": "other"}, clear=True):
        with pytest.raises(RuntimeError):
            Settings.from_env()
    with patch.dict(os.environ, {**env, "CODEBUDDY_INTERNET_ENVIRONMENT": "internal"}, clear=True):
        with pytest.raises(RuntimeError):
            Settings.from_env()


def test_sdk_options_disable_tools_and_keep_stable_cwd(monkeypatch, tmp_path):
    module = ModuleType("codebuddy_agent_sdk")
    class AssistantMessage:
        def __init__(self, content): self.content = content
    class TextBlock:
        def __init__(self, text): self.text = text
    class StreamEvent:
        def __init__(self, event): self.event = event
    class ResultMessage:
        is_error = False
        usage = {"input_tokens": 4461, "output_tokens": 4, "cache_read_input_tokens": 0}
        num_turns = 2
        duration_api_ms = 3140
        result = "PING-OK"
    captured = {}
    class CodeBuddyAgentOptions:
        def __init__(self, **kwargs): captured.update(kwargs)
    async def query(*, prompt, options):
        yield StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "PING-"}})
        yield StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}})
        yield AssistantMessage([TextBlock("PING-OK")])
        yield ResultMessage()
    module.AssistantMessage = AssistantMessage
    module.TextBlock = TextBlock
    module.StreamEvent = StreamEvent
    module.ResultMessage = ResultMessage
    module.CodeBuddyAgentOptions = CodeBuddyAgentOptions
    module.query = query
    monkeypatch.setitem(sys.modules, "codebuddy_agent_sdk", module)
    async def collect():
        return [entry async for entry in SDKBackend(cwd=tmp_path, api_key="secret").generate(
            "probe", "demo-model", stream=True)]
    rows = asyncio.run(collect())
    assert rows == [("delta", "PING-"), ("delta", "OK"),
                    ("result", {"text": "PING-OK", "usage": {"input_tokens": 4461,
                    "output_tokens": 4, "cache_read_input_tokens": 0},
                    "num_turns": 2, "api_duration_ms": 3140})]
    assert captured["tools"] == [] and captured["max_turns"] == 1
    assert captured["cwd"] == str(tmp_path)
    assert captured["setting_sources"] == []
    assert captured["env"]["CODEBUDDY_INTERNET_ENVIRONMENT"] == "ioa"
    assert captured["persist_session"] is False
