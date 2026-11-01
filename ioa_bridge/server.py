"""Local-only OpenAI Responses facade backed by the official CodeBuddy SDK."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .catalog import ModelCatalog
from .core import (BridgeError, MAX_INPUT_CHARS, MAX_TOOL_SPEC_CHARS, MODEL_ID, active_tools_for_turn,
                   conversation_prompt, encode_sse, event, image_prompt_blocks, new_response, parse_tool_answer, response_usage,
                   tool_prompt, validate_tools)
from .sdk_backend import SDKBackend
from .sessions import SessionManager, is_single_delta


DEFAULT_MAX_BODY_MB = 128  # Large enough for typical 1M-token text histories (not a token guarantee).
MAX_CONFIG_BODY_MB = 256
MAX_CONFIG_PROMPT_CHARS = 32_000_000
MAX_CONFIG_TOOL_SPEC_CHARS = 32_000_000

# CodeBuddy CLI auto-compaction budget baseline; NOT an upstream context-tier selector.
DEFAULT_AUTOCOMPACT_TOKENS = 1_000_000
MIN_AUTOCOMPACT_TOKENS = 100_000
MAX_AUTOCOMPACT_TOKENS = 1_000_000


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} 必须是整数") from None
    if not minimum <= number <= maximum:
        raise RuntimeError(f"{name} 必须介于 {minimum} 与 {maximum} 之间")
    return number


async def _read_body_limited(request: Request, limit: int) -> bytearray:
    """Enforce byte limit before materializing the request (even for chunked bodies)."""
    size_header = request.headers.get("content-length", "")
    if size_header.isascii() and size_header.isdigit():
        if len(size_header) > 18 or int(size_header) > limit:
            raise BridgeError("input_too_large", "请求体超过本地字节限制；请压缩历史或调整 IOA_BRIDGE_MAX_BODY_MB", 413)
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(body):
            raise BridgeError("input_too_large", "请求体超过本地字节限制；请压缩历史或调整 IOA_BRIDGE_MAX_BODY_MB", 413)
        body.extend(chunk)
    return body


@dataclass(frozen=True)
class Settings:
    bridge_key: str
    api_key: str
    cwd: Path
    experimental_tools: bool = False
    max_body_bytes: int = DEFAULT_MAX_BODY_MB * 1024 * 1024
    max_prompt_chars: int = MAX_INPUT_CHARS
    max_tool_spec_chars: int = MAX_TOOL_SPEC_CHARS
    auto_compact_window_tokens: int = DEFAULT_AUTOCOMPACT_TOKENS

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.getenv("IOA_BRIDGE_KEY", "")
        if len(key) < 16:
            raise RuntimeError("请设置至少 16 字符的 IOA_BRIDGE_KEY（本地客户端密钥）")
        if os.getenv("CODEBUDDY_INTERNET_ENVIRONMENT") != "ioa":
            raise RuntimeError("请先设置 CODEBUDDY_INTERNET_ENVIRONMENT=ioa")
        api_key = os.getenv("CODEBUDDY_API_KEY", "")
        if not api_key:
            raise RuntimeError("未找到官方 CODEBUDDY_API_KEY")
        if os.getenv("CODEBUDDY_AUTH_TOKEN"):
            raise RuntimeError("检测到 CODEBUDDY_AUTH_TOKEN；请取消该环境变量以避免覆盖 API Key")
        cwd = Path(os.getenv("IOA_BRIDGE_CWD", "~/.cache/codebuddy2api/ioa-bridge")).expanduser().resolve()
        cwd.mkdir(parents=True, exist_ok=True)
        mb = _env_int("IOA_BRIDGE_MAX_BODY_MB", DEFAULT_MAX_BODY_MB, 1, MAX_CONFIG_BODY_MB)
        chars = _env_int("IOA_BRIDGE_MAX_PROMPT_CHARS", MAX_INPUT_CHARS, 100_000, MAX_CONFIG_PROMPT_CHARS)
        tool_chars = _env_int("IOA_BRIDGE_MAX_TOOL_SPEC_CHARS", MAX_TOOL_SPEC_CHARS,
                              1_000, MAX_CONFIG_TOOL_SPEC_CHARS)
        compact_tokens = _env_int("IOA_BRIDGE_AUTOCOMPACT_TOKENS",
                                  DEFAULT_AUTOCOMPACT_TOKENS,
                                  MIN_AUTOCOMPACT_TOKENS, MAX_AUTOCOMPACT_TOKENS)
        return cls(key, api_key, cwd, os.getenv("IOA_BRIDGE_EXPERIMENTAL_TOOLS") == "1",
                   mb * 1024 * 1024, chars, tool_chars, compact_tokens)


def _error(err: BridgeError) -> JSONResponse:
    return JSONResponse({"error": {"message": err.message, "type": "invalid_request_error"
                                   if err.status in (400, 413, 422) else "api_error",
                                   "code": err.code}}, status_code=err.status)


def _authorized(request: Request, key: str) -> bool:
    header = request.headers.get("authorization", "")
    received = header[7:] if header.lower().startswith("bearer ") else ""
    return secrets.compare_digest(key, received)


def _output_text(answer: str) -> dict[str, Any]:
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": answer, "annotations": []}]}


def _output_call(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    call_id = "call_" + uuid.uuid4().hex
    if kind == "tool_search_call":
        return {"id": "tsc_" + uuid.uuid4().hex, "type": kind,
                "status": "completed", "call_id": call_id, **payload}
    prefix = "fc_" if kind == "function_call" else "ctc_"
    return {"id": prefix + uuid.uuid4().hex, "type": kind, "status": "completed",
            "call_id": call_id, **payload}


def _response_metadata(response: dict[str, Any], raw_usage: dict[str, int]) -> None:
    response["usage"] = response_usage(raw_usage)
    # The SDK's dollar cost is not the company's RMB bill. Preserve the two
    # cache counters as informative vendor metadata, never as monetary amounts.
    response.setdefault("metadata", {}).update({
        "ioa_cache_creation_tokens": str(raw_usage.get("cache_creation_input_tokens", 0)),
        "ioa_cache_read_tokens": str(raw_usage.get("cache_read_input_tokens", 0)),
    })


def _text_events(item: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    idx = 0
    item_id = item["id"]
    part = item["content"][0]
    return [
        ("response.output_item.added", event("response.output_item.added", {},
                                              output_index=idx,
                                              item={**item, "status": "in_progress", "content": []})),
        ("response.content_part.added", event("response.content_part.added", {},
                                               item_id=item_id, output_index=idx,
                                               content_index=0,
                                               part={"type": "output_text", "text": "", "annotations": []})),
        ("response.output_text.delta", event("response.output_text.delta", {},
                                              item_id=item_id, output_index=idx, content_index=0,
                                              delta=part["text"])),
        ("response.output_text.done", event("response.output_text.done", {},
                                             item_id=item_id, output_index=idx,
                                             content_index=0, text=part["text"])),
        ("response.content_part.done", event("response.content_part.done", {},
                                              item_id=item_id, output_index=idx,
                                              content_index=0, part=part)),
        ("response.output_item.done", event("response.output_item.done", {},
                                             output_index=idx, item=item)),
    ]


def _tool_events(item: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    idx = 0
    if item["type"] == "tool_search_call":
        return [
            ("response.output_item.added", event("response.output_item.added", {},
                                                  output_index=idx, item={**item, "status": "in_progress"})),
            ("response.output_item.done", event("response.output_item.done", {},
                                                 output_index=idx, item=item)),
        ]
    base = {"item_id": item["id"], "output_index": idx}
    if item["type"] == "function_call":
        value, prefix = item["arguments"], "response.function_call_arguments"
        empty = {**item, "arguments": "", "status": "in_progress"}
        field = "arguments"
    else:
        value, prefix = item["input"], "response.custom_tool_call_input"
        empty = {**item, "input": "", "status": "in_progress"}
        field = "input"
    return [
        ("response.output_item.added", event("response.output_item.added", {}, output_index=idx, item=empty)),
        (prefix + ".delta", event(prefix + ".delta", {}, **base, delta=value)),
        (prefix + ".done", event(prefix + ".done", {}, **base, **{field: value})),
        ("response.output_item.done", event("response.output_item.done", {}, output_index=idx, item=item)),
    ]


async def _get_result(backend: Any, prompt: str | list[dict[str, Any]], model: str, *, stream: bool,
                      session_id: str | None) -> AsyncIterator[tuple[str, Any]]:
    async for typ, data in backend.generate(prompt, model, stream=stream, session_id=session_id):
        yield typ, data


def create_app(*, settings: Settings | None = None, backend: Any = None,
               catalog: ModelCatalog | None = None) -> FastAPI:
    """Construct a bridge without reading credentials or starting SDK processes."""
    settings = settings or Settings.from_env()
    backend = backend or SDKBackend(cwd=settings.cwd, api_key=settings.api_key,
                                    auto_compact_window_tokens=settings.auto_compact_window_tokens)
    catalog = catalog or ModelCatalog()
    sessions = SessionManager()
    app = FastAPI(title="Local CodeBuddy iOA SDK Responses bridge", version="0.1.0")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/models")
    def models(request: Request):
        if not _authorized(request, settings.bridge_key):
            return _error(BridgeError("invalid_api_key", "无效的本地桥接密钥", 401))
        names = catalog.models()
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "codebuddy-ioa"}
                                             for m in names]}

    @app.post("/v1/responses")
    async def responses(request: Request):
        if not _authorized(request, settings.bridge_key):
            return _error(BridgeError("invalid_api_key", "无效的本地桥接密钥", 401))
        try:
            raw = await _read_body_limited(request, settings.max_body_bytes)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise BridgeError("invalid_json", "请求必须是有效 JSON", 400) from None
            if not isinstance(body, dict):
                raise BridgeError("invalid_json", "请求 JSON 必须是对象", 400)
            model = body.get("model")
            if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
                raise BridgeError("invalid_model", "请指定有效的 model", 422)
            allowed = catalog.models()
            if not allowed:
                raise BridgeError("models_unavailable", "模型目录为空；请检查 SDK 二进制或 IOA_BRIDGE_MODELS", 503)
            if model not in allowed:
                raise BridgeError("model_not_listed", "模型不在本地发现的目录中", 404)
            choice = body.get("tool_choice", "auto")
            raw_tools = body.get("tools")
            # Do not spend memory checking 100K+ tool schemas when tools are
            # explicitly disabled; avoid reporting a misleading 413 here.
            if choice == "none":
                requested_tools = []
            else:
                if raw_tools and not settings.experimental_tools:
                    raise BridgeError("tools_not_enabled",
                                      "此桥接默认不支持 Codex 工具调用；实验模式需显式开启", 422)
                tools = validate_tools(raw_tools, max_chars=settings.max_tool_spec_chars)
                requested_tools = tools if settings.experimental_tools else []
            active_tools, deferred_previews = active_tools_for_turn(requested_tools, body.get("input"))
            if body.get("previous_response_id"):
                raise BridgeError("unsupported_history", "不支持服务端 previous_response_id；请发送完整 input 历史", 422)
            if body.get("background"):
                raise BridgeError("unsupported_background", "不支持后台执行", 422)
            image_sources: list[dict[str, Any]] = []
            image_marker = "\x1fioa_img_" + uuid.uuid4().hex + "_"
            prompt = conversation_prompt(body, tools=requested_tools,
                                         max_chars=settings.max_prompt_chars,
                                         image_sources=image_sources,
                                         image_marker=image_marker)
            if requested_tools:
                prompt = tool_prompt(prompt, active_tools, choice, deferred=deferred_previews)
                # Tool schema and instructions also consume context; never silently truncate.
                if len(prompt) > settings.max_prompt_chars:
                    raise BridgeError("input_too_large", "工具说明和历史合并后超过本地字符限制", 413)
            # Images stay as native SDK content blocks; do not turn them into
            # fake text descriptions or silently drop them. No URL retrieval.
            backend_prompt = image_prompt_blocks(prompt, image_sources, image_marker) if image_sources else prompt
            stream = body.get("stream", False)
            if not isinstance(stream, bool):
                raise BridgeError("invalid_stream", "stream 必须为布尔值", 422)
            session_key = request.headers.get("x-ioa-delta-session")
            session_id, rotated = sessions.choose(session_key, model,
                                                   is_delta=is_single_delta(body.get("input"))
                                                   and not requested_tools)
        except BridgeError as err:
            return _error(err)

        response = new_response(model)
        if requested_tools:
            unavailable = [t["name"] for t in requested_tools
                           if t["type"] == "unavailable_hosted"]
            if unavailable:
                # Mark advertised but unavailable vendor-hosted capabilities.
                # These do not become fake tool calls or unsourced citations.
                response["metadata"] = {"ioa_unavailable_hosted_tools": ",".join(unavailable)}
        async def generate():
            yield "response.created", event("response.created", {}, response={**response, "status": "in_progress"})
            yield "response.in_progress", event("response.in_progress", {}, response={**response, "status": "in_progress"})
            try:
                raw_usage: dict[str, int] = {}
                collected: list[str] = []
                text_item = None
                # Experimental tool mode must buffer an entire valid JSON object
                # before deciding whether to hand off a tool call.
                if not requested_tools:
                    text_item = _output_text("")
                    empty_item = {**text_item, "status": "in_progress", "content": []}
                    yield "response.output_item.added", event("response.output_item.added", {}, output_index=0, item=empty_item)
                    yield "response.content_part.added", event("response.content_part.added", {},
                                                               output_index=0, item_id=text_item["id"],
                                                               content_index=0,
                                                               part={"type": "output_text", "text": "", "annotations": []})
                async for kind, payload in _get_result(backend, backend_prompt, model, stream=stream, session_id=session_id):
                    if kind == "delta":
                        collected.append(payload)
                        if text_item is not None:
                            yield "response.output_text.delta", event("response.output_text.delta", {},
                                                                      item_id=text_item["id"], output_index=0,
                                                                      content_index=0, delta=payload)
                    elif kind == "result":
                        raw_usage = payload.get("usage") or {}
                        final_text = payload.get("text") or "".join(collected)
                sessions.record(session_key, raw_usage)
                if requested_tools:
                    mode, data = parse_tool_answer(final_text, active_tools, choice)
                    if mode == "message":
                        item = _output_text(data)
                        for name, item_event in _text_events(item):
                            yield name, item_event
                    else:
                        item = _output_call(mode, data)
                        for name, item_event in _tool_events(item):
                            yield name, item_event
                else:
                    item = _output_text(final_text)
                    item["id"] = text_item["id"]
                    yield "response.output_text.done", event("response.output_text.done", {},
                                                              item_id=item["id"], output_index=0,
                                                              content_index=0, text=final_text)
                    yield "response.content_part.done", event("response.content_part.done", {},
                                                               item_id=item["id"], output_index=0, content_index=0,
                                                               part=item["content"][0])
                    yield "response.output_item.done", event("response.output_item.done", {},
                                                              output_index=0, item=item)
                response["output"].append(item)
                _response_metadata(response, raw_usage)
                yield "response.completed", event("response.completed", {}, response=response)
            except BridgeError as exc:
                response["status"] = "failed"
                response["error"] = {"code": exc.code, "message": exc.message}
                yield "response.failed", event("response.failed", {}, response=response)
            except Exception:
                response["status"] = "failed"
                response["error"] = {"code": "bridge_failed", "message": "桥接内部执行失败"}
                yield "response.failed", event("response.failed", {}, response=response)
            finally:
                sessions.release(session_key)

        if stream:
            async def stream_sse() -> AsyncIterator[bytes]:
                async for name, item in generate():
                    yield encode_sse(name, item)
                yield b"data: [DONE]\n\n"
            return StreamingResponse(stream_sse(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        result: dict[str, Any] | None = None
        failure = None
        async for name, data in generate():
            if name == "response.completed":
                result = data["response"]
            elif name == "response.failed":
                failure = data["response"].get("error", {})
        if failure:
            return _error(BridgeError(failure.get("code", "sdk_failed"),
                                      failure.get("message", "SDK 调用失败"), 502))
        if result is None:
            return _error(BridgeError("bridge_incomplete", "桥接未产生最终响应", 502))
        return JSONResponse(result)

    return app
