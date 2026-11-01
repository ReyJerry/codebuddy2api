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
    assert captured["permission_mode"] == "default"  # no SDK plan-only restriction
    assert captured["allowed_tools"] == [] and callable(captured["can_use_tool"])
    assert captured["cwd"] == str(tmp_path)
    assert captured["setting_sources"] == []
    assert captured["env"]["CODEBUDDY_INTERNET_ENVIRONMENT"] == "ioa"
    assert captured["persist_session"] is False


def test_long_history_above_old_two_mb_and_250k_chars_passes_without_truncation():
    c, backend = client()
    # Previous versions rejected this with HTTP 413 before reaching the SDK.
    huge_history = "a" * 2_200_000
    response = c.post("/v1/responses", headers=headers(),
                      json={"model": "demo-model", "input": huge_history})
    assert response.status_code == 200, response.text[:300]
    assert len(backend.calls) == 1
    assert huge_history in backend.calls[0][0]
    assert len(backend.calls[0][0]) > 250_000


def test_body_size_limit_is_enforced_before_sdk_for_content_length_and_chunked():
    c, backend = client()
    app = create_app(settings=Settings("z" * 32, "secret", Path("/tmp"), False,
                                       max_body_bytes=2048), backend=backend, catalog=FakeCatalog())
    c = TestClient(app)
    large = {"model": "demo-model", "input": "x" * 3000}
    r = c.post("/v1/responses", headers=headers(), json=large)
    assert r.status_code == 413 and r.json()["error"]["code"] == "input_too_large"
    payload = json.dumps(large).encode()
    chunks = (payload[i:i + 400] for i in range(0, len(payload), 400))
    r2 = c.post("/v1/responses", headers={**headers(), "Content-Type": "application/json"},
                content=chunks)
    assert r2.status_code == 413 and r2.json()["error"]["code"] == "input_too_large"
    assert not backend.calls


def test_prompt_chars_limit_is_distinct_from_request_bytes():
    backend = FakeBackend()
    settings = Settings("z" * 32, "secret", Path("/tmp"), False,
                        max_body_bytes=1_000_000, max_prompt_chars=200)
    c = TestClient(create_app(settings=settings, backend=backend, catalog=FakeCatalog()))
    r = c.post("/v1/responses", headers=headers(),
               json={"model": "demo-model", "input": "Z" * 400})
    assert r.status_code == 413 and r.json()["error"]["code"] == "input_too_large"
    assert not backend.calls


def test_tool_instructions_count_against_total_prompt_ceiling():
    backend = FakeBackend()
    settings = Settings("z" * 32, "secret", Path("/tmp"), True,
                        max_body_bytes=1_000_000, max_prompt_chars=500)
    c = TestClient(create_app(settings=settings, backend=backend, catalog=FakeCatalog()))
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "x" * 200,
        "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}]})
    assert r.status_code == 413 and r.json()["error"]["code"] == "input_too_large"
    assert not backend.calls


def test_settings_long_context_defaults_and_env_overrides(monkeypatch, tmp_path):
    env = {"IOA_BRIDGE_KEY": "z" * 32, "CODEBUDDY_API_KEY": "secret",
           "CODEBUDDY_INTERNET_ENVIRONMENT": "ioa", "IOA_BRIDGE_CWD": str(tmp_path)}
    with patch.dict(os.environ, env, clear=True):
        settings = Settings.from_env()
        assert settings.max_body_bytes == 128 * 1024 * 1024
        assert settings.max_prompt_chars == 16_000_000
    with patch.dict(os.environ, {**env, "IOA_BRIDGE_MAX_BODY_MB": "256",
                                      "IOA_BRIDGE_MAX_PROMPT_CHARS": "32000000"}, clear=True):
        settings = Settings.from_env()
        assert settings.max_body_bytes == 256 * 1024 * 1024
        assert settings.max_prompt_chars == 32_000_000
    for bad in ("0", "512", "not-an-integer"):
        with patch.dict(os.environ, {**env, "IOA_BRIDGE_MAX_BODY_MB": bad}, clear=True):
            with pytest.raises(RuntimeError):
                Settings.from_env()
    with patch.dict(os.environ, {**env, "IOA_BRIDGE_MAX_PROMPT_CHARS": "36000000"}, clear=True):
        with pytest.raises(RuntimeError):
            Settings.from_env()


def _large_codex_tool_schema():
    """Representative oversized tool spec: nested parameter docs, not just top-level description."""
    return [{"type": "function", "name": "shell_command", "description": "Run an approved command",
             "parameters": {"type": "object", "properties": {
                 f"argument_{i}": {"type": "string", "description": "D" * 4_000}
                 for i in range(12)}, "required": ["argument_0"]}}]


def test_codex_tool_schema_larger_than_legacy_limit_is_not_dropped():
    schema = _large_codex_tool_schema()
    # Regression: legacy 24K tool cap should not reject real Codex tool metadata.
    assert 24_000 < len(json.dumps(schema, ensure_ascii=False)) < 512_000
    b = FakeBackend('{"kind":"message","text":"OK"}')
    c, b = client(b, tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "hello", "tools": schema, "tool_choice": "auto"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["status"] == "completed"
    assert len(b.calls) == 1
    assert 'argument_11' in b.calls[0][0]
    assert '"required": ["argument_0"]' in b.calls[0][0]


def test_explicit_tool_limit_fails_cleanly_before_any_model_request():
    b = FakeBackend()
    settings = Settings("z" * 32, "not-used", Path("/tmp"), True,
                        max_tool_spec_chars=24_000)
    c = TestClient(create_app(settings=settings, backend=b, catalog=FakeCatalog()))
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "hello", "tools": _large_codex_tool_schema()})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "tool_spec_too_large"
    assert "IOA_BRIDGE_MAX_TOOL_SPEC_CHARS" in r.json()["error"]["message"]
    assert not b.calls


def test_no_tool_choice_and_disabled_tool_mode_do_not_raise_misleading_413():
    huge_tools = _large_codex_tool_schema()
    b = FakeBackend()
    settings = Settings("z" * 32, "not-used", Path("/tmp"), True,
                        max_tool_spec_chars=1_000)
    c = TestClient(create_app(settings=settings, backend=b, catalog=FakeCatalog()))
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "hello", "tools": huge_tools,
        "tool_choice": "none"})
    assert r.status_code == 200, r.text[:300]
    assert len(b.calls) == 1 and "Available tools JSON" not in b.calls[0][0]
    b2 = FakeBackend()
    c2, b2 = client(b2)
    r2 = c2.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "hello", "tools": huge_tools})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "tools_not_enabled"
    assert not b2.calls


def test_env_tool_schema_bounds(tmp_path):
    base = {"IOA_BRIDGE_KEY": "z" * 32, "CODEBUDDY_API_KEY": "secret",
            "CODEBUDDY_INTERNET_ENVIRONMENT": "ioa", "IOA_BRIDGE_CWD": str(tmp_path)}
    with patch.dict(os.environ, base, clear=True):
        assert Settings.from_env().max_tool_spec_chars == 8_000_000
    with patch.dict(os.environ, {**base, "IOA_BRIDGE_MAX_TOOL_SPEC_CHARS": "2000000"}, clear=True):
        assert Settings.from_env().max_tool_spec_chars == 2_000_000
    for bad in ("0", "32000001", "not-a-number"):
        with patch.dict(os.environ, {**base, "IOA_BRIDGE_MAX_TOOL_SPEC_CHARS": bad}, clear=True):
            with pytest.raises(RuntimeError):
                Settings.from_env()


def _many_codex_tools(count=180):
    return [{"type": "function", "name": f"mcp__demo__tool_{i}",
             "description": f"Synthetic tool {i} description",
             "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}}}
            for i in range(count)]


def test_more_than_64_tools_are_preserved_and_accepted():
    """Previous builds wrongly rejected valid 65+ tool lists with 422."""
    tools = _many_codex_tools(180)
    assert len(tools) > 64
    b = FakeBackend('{"kind":"message","text":"OK"}')
    c, b = client(b, tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "reply", "tools": tools})
    assert r.status_code == 200, r.text[:300]
    assert len(b.calls) == 1
    assert 'mcp__demo__tool_0' in b.calls[0][0]
    assert 'mcp__demo__tool_179' in b.calls[0][0]
    assert b.calls[0][0].count('"type": "function"') == len(tools)


def test_tool_call_to_last_tool_in_180_item_list():
    tools = _many_codex_tools(180)
    response = '{"kind":"tool_call","name":"mcp__demo__tool_179","arguments":{"arg":"ok"}}'
    b = FakeBackend(response)
    c, b = client(b, tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "Call the last tool", "tools": tools})
    assert r.status_code == 200, r.text[:300]
    output = r.json()["output"][0]
    assert output["type"] == "function_call"
    assert output["name"] == "mcp__demo__tool_179"
    assert json.loads(output["arguments"])["arg"] == "ok"


def test_distinct_tool_shape_duplicate_and_type_errors():
    c, b = client(tools=True)
    for invalid, expected in [
        ({"type": "function"}, "invalid_tools_shape"),
        ("tool", "invalid_tools_shape"),
        (["not-an-object"], "invalid_tool_entry"),
        ([{"type": "file_search", "vector_store_ids": ["vs_abc"]}], "unsupported_tool_type"),
        (_many_codex_tools(1) * 2, "duplicate_tool_name"),
    ]:
        r = c.post("/v1/responses", headers=headers(), json={
            "model": "demo-model", "input": "hello", "tools": invalid})
        assert r.status_code == 422, r.text[:200]
        assert r.json()["error"]["code"] == expected
    assert not b.calls


def test_no_description_truncation_and_incremental_budget_check():
    extra_description = "d" * 1500
    tools = _many_codex_tools(75)
    tools[-1]["description"] = extra_description
    full = validate_tools(tools)
    assert full[-1]["description"] == extra_description
    exact_chars = len(json.dumps(full, ensure_ascii=False))
    assert len(json.dumps(full, ensure_ascii=False)) == exact_chars
    assert len(validate_tools(tools, max_chars=exact_chars)) == len(tools)
    with pytest.raises(BridgeError) as exc:
        validate_tools(tools, max_chars=exact_chars - 1)
    assert exc.value.code == "tool_spec_too_large" and exc.value.status == 413


def test_large_tool_list_only_hits_explicit_budget_not_arbitrary_count():
    tools = _many_codex_tools(200)
    b = FakeBackend()
    settings = Settings("z" * 32, "secret", Path("/tmp"), True,
                        max_tool_spec_chars=2_000)
    c = TestClient(create_app(settings=settings, backend=b, catalog=FakeCatalog()))
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "hello", "tools": tools})
    assert r.status_code == 413 and r.json()["error"]["code"] == "tool_spec_too_large"
    assert not b.calls


def test_large_tool_budget_env_override(tmp_path):
    env = {"IOA_BRIDGE_KEY": "z" * 32, "CODEBUDDY_API_KEY": "secret",
           "CODEBUDDY_INTERNET_ENVIRONMENT": "ioa", "IOA_BRIDGE_CWD": str(tmp_path)}
    with patch.dict(os.environ, env, clear=True):
        assert Settings.from_env().max_tool_spec_chars == 8_000_000
    with patch.dict(os.environ, {**env, "IOA_BRIDGE_MAX_TOOL_SPEC_CHARS": "32000000"}, clear=True):
        assert Settings.from_env().max_tool_spec_chars == 32_000_000
    with patch.dict(os.environ, {**env, "IOA_BRIDGE_MAX_TOOL_SPEC_CHARS": "32000001"}, clear=True):
        with pytest.raises(RuntimeError):
            Settings.from_env()


def _codex_new_tools():
    """Modern Codex request: function/custom + namespace + web + discovery."""
    return [
        {"type": "function", "name": "exec_command", "description": "run command",
         "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
        {"type": "namespace", "name": "mcp__demo__", "description": "test deferred tools", "tools": [
            {"type": "function", "name": "list_events", "defer_loading": True,
             "description": "list calendar events", "parameters": {"type": "object", "properties": {}}},
            {"type": "custom", "name": "freeform", "format": {"type": "text"}},
        ]},
        {"type": "web_search", "external_web_access": False},
        {"type": "tool_search", "execution": "client", "description": "Search deferred tools",
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string"}, "limit": {"type": "number"}}, "required": ["query"]}},
    ]


def test_codex_mixed_tools_client_tool_search_json_and_metadata():
    tools = _codex_new_tools()
    answer = '{"kind":"tool_search_call","arguments":{"query":"calendar events","limit":8}}'
    c, b = client(FakeBackend(answer), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "find calendar tools", "tools": tools})
    assert r.status_code == 200, r.text
    value = r.json()
    item = value["output"][0]
    assert item["type"] == "tool_search_call"
    assert item["execution"] == "client" and item["call_id"].startswith("call_")
    assert item["arguments"] == {"query": "calendar events", "limit": 8}
    assert value["metadata"]["ioa_unavailable_hosted_tools"] == "web_search"
    assert "web_search" in b.calls[0][0]
    assert "NOT available" in b.calls[0][0]
    assert '"namespace": "mcp__demo__"' in b.calls[0][0]
    assert '"type": "tool_search"' in b.calls[0][0]


def test_codex_client_tool_search_sse_item_and_followup_handoff():
    tools = _codex_new_tools()
    answer = '{"kind":"tool_search_call","arguments":{"query":"calendar events"}}'
    c, _ = client(FakeBackend(answer), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "discover", "tools": tools, "stream": True})
    assert r.status_code == 200, r.text
    data = [json.loads(x[6:]) for x in r.text.splitlines() if x.startswith("data: {")]
    tool = next(x["item"] for x in data if x.get("type") == "response.output_item.done")
    assert tool["type"] == "tool_search_call" and tool["execution"] == "client"
    assert tool["arguments"]["query"] == "calendar events"
    assert data[-1]["response"]["metadata"]["ioa_unavailable_hosted_tools"] == "web_search"
    assert r.text.endswith("data: [DONE]\n\n")

    followup = [
        {"role": "user", "content": "find calendar tools"},
        tool,
        {"type": "tool_search_output", "execution": "client", "call_id": tool["call_id"],
         "status": "completed", "tools": [tools[2]]},
    ]
    answer2 = '{"kind":"tool_call","namespace":"mcp__demo__","name":"list_events","arguments":{}}'
    c2, b2 = client(FakeBackend(answer2), tools=True)
    r2 = c2.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": followup, "tools": tools})
    assert r2.status_code == 200, r2.text
    tool2 = r2.json()["output"][0]
    assert tool2["type"] == "function_call"
    assert tool2["namespace"] == "mcp__demo__" and tool2["name"] == "list_events"
    assert tool2["arguments"] == "{}"
    assert "tool_search_result" in b2.calls[0][0]
    assert "previous_tool_search" in b2.calls[0][0]

    # The Codex client can send tool execution results in a later request.
    followup.extend([tool2, {"type": "function_call_output", "call_id": tool2["call_id"],
                             "output": '{"events":[]}'}])
    c3, b3 = client(FakeBackend('{"kind":"message","text":"No events"}'), tools=True)
    r3 = c3.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": followup, "tools": tools})
    assert r3.status_code == 200, r3.text
    assert r3.json()["output"][0]["content"][0]["text"] == "No events"
    assert "tool_result" in b3.calls[0][0]


def test_web_search_is_explicitly_unavailable_not_fabricated_or_ignored():
    c, b = client(FakeBackend('{"kind":"message","text":"Live web search is unavailable"}'), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "What's the latest?",
        "tools": [{"type": "web_search"}]})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["ioa_unavailable_hosted_tools"] == "web_search"
    assert "NOT available" in b.calls[0][0]
    assert r.json()["output"][0]["content"][0]["text"] == "Live web search is unavailable"
    # Forced hosted tools are explicitly rejected before SDK call.
    r2 = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "Search now", "tool_choice": {"type": "web_search"},
        "tools": [{"type": "web_search"}]})
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "hosted_tool_unavailable"
    assert len(b.calls) == 1


def test_discovery_rejects_server_execution_and_malformed_args():
    tools = _codex_new_tools()
    tools[-1] = {**tools[-1], "execution": "server"}
    c, b = client(tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "discover", "tools": tools})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unsupported_hosted_tool"
    assert not b.calls

    for answer in [
        '{"kind":"tool_search_call","arguments":{"query":""}}',
        '{"kind":"tool_search_call","arguments":{"query":"q","limit":0}}',
        '{"kind":"tool_search_call","arguments":{"query":"q","limit":true}}',
        '{"kind":"tool_search_call","arguments":{"query":"q","limit":200}}',
    ]:
        c2, _ = client(FakeBackend(answer), tools=True)
        r2 = c2.post("/v1/responses", headers=headers(), json={
            "model": "demo-model", "input": "discover", "tools": _codex_new_tools()})
        assert r2.status_code == 502 and r2.json()["error"]["code"] == "tool_protocol_error"


def test_named_tool_collisions_and_namespaced_custom_calls():
    tools = [
        {"type": "namespace", "name": "alpha", "tools": [
            {"type": "custom", "name": "run", "format": {"type": "text"}}]},
        {"type": "namespace", "name": "beta", "tools": [
            {"type": "custom", "name": "run", "format": {"type": "text"}}]},
    ]
    assert len(validate_tools(tools)) == 2
    c, _ = client(FakeBackend('{"kind":"tool_call","namespace":"beta","name":"run","input":"hello"}'), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "run", "tools": tools})
    assert r.status_code == 200, r.text
    item = r.json()["output"][0]
    assert item["type"] == "custom_tool_call" and item["namespace"] == "beta"
    c2, _ = client(FakeBackend('{"kind":"tool_call","name":"run","input":"hello"}'), tools=True)
    r2 = c2.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "run", "tools": tools})
    assert r2.status_code == 502 and r2.json()["error"]["code"] == "tool_protocol_error"


def test_tool_search_history_accepts_stringified_args_and_rejects_malformed():
    t = _codex_new_tools()
    c, b = client(FakeBackend('{"kind":"message","text":"OK"}'), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "tools": t,
        "input": [
            {"type": "tool_search_call", "call_id": "call_abc", "execution": "client",
             "arguments": '{"query":"list tools"}'},
            {"type": "tool_search_output", "call_id": "call_abc", "execution": "client",
             "tools": [t[2]]},
            {"role": "user", "content": "continue"},
        ]})
    assert r.status_code == 200, r.text
    assert "list tools" in b.calls[0][0]
    assert "tool_search_result" in b.calls[0][0]
    r2 = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "tools": t,
        "input": [{"type": "tool_search_call", "call_id": "call_abc", "arguments": "not json"}]})
    assert r2.status_code == 422


def test_deferred_namespace_tool_cannot_be_called_before_discovery():
    tools = _codex_new_tools()
    shortcut = '{"kind":"tool_call","namespace":"mcp__demo__","name":"list_events","arguments":{}}'
    c, b = client(FakeBackend(shortcut), tools=True)
    result = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "read events", "tools": tools})
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "tool_protocol_error"
    first_prompt = b.calls[0][0]
    assert "Deferred tools (must use tool_search before calling)" in first_prompt
    assert "list_events" in first_prompt
    assert '"type": "tool_search"' in first_prompt
    # The deferred tool is previewed without its full parameter schema.
    assert '"name": "list_events", "description": "list calendar events"' not in first_prompt

    loaded = [{"role": "user", "content": "read events"},
              {"type": "tool_search_output", "call_id": "call_find", "execution": "client",
               "tools": [tools[2]]}]
    c2, _ = client(FakeBackend(shortcut), tools=True)
    r2 = c2.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": loaded, "tools": tools})
    assert r2.status_code == 200
    assert r2.json()["output"][0]["namespace"] == "mcp__demo__"


def test_tool_choice_required_hosted_only_rejects_before_paid_backend():
    c, b = client(FakeBackend('{"kind":"message","text":"OK"}'), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "search", "tool_choice": "required",
        "tools": [{"type": "web_search"}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "hosted_tool_unavailable"
    assert not b.calls


def test_exact_fifth_tool_web_search_does_not_break_coding_request():
    base = [{"type": "function", "name": f"tool_{i}",
             "parameters": {"type": "object", "properties": {}}} for i in range(4)]
    base.append({"type": "web_search", "external_web_access": False})
    c, b = client(FakeBackend('{"kind":"tool_call","name":"tool_1","arguments":{}}'), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": "run a coding tool", "tools": base})
    assert r.status_code == 200, r.text
    assert r.json()["output"][0]["type"] == "function_call"
    assert r.json()["metadata"]["ioa_unavailable_hosted_tools"] == "web_search"
    assert "NOT available" in b.calls[0][0]

# Regression: Codex App sends OpenAI Responses input_image blocks, which must
# actually reach the CodeBuddy SDK as native stream-json image blocks.
_TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)
_TEST_PNG = "data:image/png;base64," + _TEST_PNG_B64


def _image_message(*parts):
    return {"type": "message", "role": "user", "content": list(parts)}


def test_codex_image_reaches_sdk_as_multimodal_blocks_without_leaking_into_text():
    c, backend = client()
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": [
            {"role": "system", "content": "Treat this as a photo analysis."},
            _image_message({"type": "input_text", "text": "What is this?"},
                           {"type": "input_image", "image_url": _TEST_PNG},
                           {"type": "input_text", "text": "Please describe it."}),
        ]})
    assert r.status_code == 200, r.text[:200]
    sdk_prompt = backend.calls[0][0]
    assert isinstance(sdk_prompt, list)
    assert [p["type"] for p in sdk_prompt] == ["text", "image", "text"]
    assert "[system]" in sdk_prompt[0]["text"]
    assert "What is this?" in sdk_prompt[0]["text"]
    assert "Please describe it." in sdk_prompt[2]["text"]
    assert sdk_prompt[1]["source"] == {"type": "base64", "media_type": "image/png", "data": _TEST_PNG_B64}
    assert _TEST_PNG_B64 not in "".join(p.get("text", "") for p in sdk_prompt)
    assert "data:image" not in str(r.json())


def test_codex_multiple_images_preserve_order_and_context():
    c, backend = client()
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": [
            _image_message({"type": "input_image", "image_url": _TEST_PNG},
                           {"type": "input_text", "text": "first photo"}),
            {"role": "assistant", "content": "I see a tiny image."},
            _image_message({"type": "input_text", "text": "Compare to this"},
                           {"type": "input_image", "image_url": {"url": _TEST_PNG, "detail": "low"}}),
        ]})
    assert r.status_code == 200
    prompt = backend.calls[0][0]
    assert [p["type"] for p in prompt].count("image") == 2
    assert prompt[0]["text"] == "[user]\n"
    assert "first photo" in prompt[2]["text"]
    assert "[assistant]" in prompt[2]["text"]
    assert "Compare to this" in prompt[2]["text"]
    assert prompt[3]["source"]["data"] == _TEST_PNG_B64


def test_codex_image_with_experimental_tool_and_sse():
    answer = '{"kind":"tool_call","name":"exec_command","arguments":{"cmd":"pwd"}}'
    c, backend = client(FakeBackend(answer), tools=True)
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "stream": True,
        "input": [_image_message({"type": "input_text", "text": "Inspect screenshot"},
                                  {"type": "input_image", "image_url": _TEST_PNG})],
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
    })
    assert r.status_code == 200
    assert "response.function_call_arguments.delta" in r.text
    prompt = backend.calls[0][0]
    assert any(p["type"] == "image" for p in prompt)
    assert "Available tools" in prompt[0]["text"] or '"name": "exec_command"' in prompt[0]["text"]


def test_codex_image_tool_result_blocks_are_preserved():
    c, backend = client()
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": [
            {"type": "function_call_output", "call_id": "call_abc",
             "output": [{"type": "input_text", "text": "Screenshot result:"},
                        {"type": "input_image", "image_url": _TEST_PNG}]},
            _image_message({"type": "input_text", "text": "Check tool screenshot"}),
        ]})
    assert r.status_code == 200
    prompt = backend.calls[0][0]
    assert any(p["type"] == "image" for p in prompt)
    assert "[tool_result call_id=" in prompt[0]["text"]


@pytest.mark.parametrize("url,code", [
    ("https://example.org/image.png", "unsupported_image_reference"),
    ("file:///tmp/sensitive.png", "unsupported_image_reference"),
    ("data:image/png;base64,%%%%", "invalid_image"),
    ("data:image/png;base64," + __import__("base64").b64encode(b"not an image").decode(), "invalid_image"),
    ("data:application/pdf;base64," + _TEST_PNG_B64, "unsupported_image_type"),
])
def test_codex_unhandled_image_references_rejected_before_sdk(url, code):
    c, backend = client()
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": [_image_message({"type": "input_image", "image_url": url})]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code
    assert not backend.calls


def test_codex_image_count_limit_without_upstream_call():
    from ioa_bridge.core import MAX_IMAGE_COUNT
    c, backend = client()
    r = c.post("/v1/responses", headers=headers(), json={
        "model": "demo-model", "input": [_image_message(*[
            {"type": "input_image", "image_url": _TEST_PNG} for _ in range(MAX_IMAGE_COUNT + 1)
        ])]})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_many_images"
    assert not backend.calls


def test_codex_sdk_real_image_stream_json_shape(monkeypatch, tmp_path):
    module = ModuleType("codebuddy_agent_sdk")
    class AssistantMessage:
        def __init__(self, content): self.content = content
    class TextBlock:
        def __init__(self, text): self.text = text
    class StreamEvent:
        def __init__(self, event): self.event = event
    class ResultMessage:
        is_error = False
        usage = {"input_tokens": 65, "output_tokens": 3}
        num_turns = 1
        duration_api_ms = 100
        result = "IMAGE-OK"
    captured = {}
    class CodeBuddyAgentOptions:
        def __init__(self, **kwargs): captured.update(kwargs)
    async def query(*, prompt, options):
        assert hasattr(prompt, "__aiter__")
        messages = [p async for p in prompt]
        captured["messages"] = messages
        yield AssistantMessage([TextBlock("IMAGE-OK")])
        yield ResultMessage()
    module.AssistantMessage = AssistantMessage
    module.TextBlock = TextBlock
    module.StreamEvent = StreamEvent
    module.ResultMessage = ResultMessage
    module.CodeBuddyAgentOptions = CodeBuddyAgentOptions
    module.query = query
    monkeypatch.setitem(sys.modules, "codebuddy_agent_sdk", module)
    blocks = [{"type": "text", "text": "Analyze:"},
              {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _TEST_PNG_B64}}]
    async def collect():
        return [e async for e in SDKBackend(cwd=tmp_path, api_key="secret").generate(
            blocks, "demo-model", stream=False)]
    result = asyncio.run(collect())
    assert result[-1][1]["text"] == "IMAGE-OK"
    assert captured["messages"] == [{"type": "user", "message": {"role": "user", "content": blocks}}]
    assert captured["tools"] == [] and callable(captured["can_use_tool"])
    assert captured["persist_session"] is False


def test_codex_text_only_keeps_previous_sdk_string_path():
    c, backend = client()
    r = c.post("/v1/responses", headers=headers(), json={"model":"demo-model", "input":"hello"})
    assert r.status_code == 200
    assert backend.calls[0][0] == "[user]\nhello"
