"""Protocol translation and safety checks for the optional iOA SDK bridge."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

MAX_INPUT_CHARS = 16_000_000  # Local character ceiling, NOT a model token limit.
# No fixed tool COUNT cap. Large Codex/MCP registries are bounded by the
# serialized schema budget instead; model context limits still apply.
# Do not silently drop descriptions or parameters from the tool declarations.
MAX_TOOL_SPEC_CHARS = 8_000_000
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class BridgeError(Exception):
    """Safe, never-secret-bearing HTTP error."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def number_usage(value: Any) -> dict[str, int]:
    """SDK returns Usage dataclasses in some versions and dictionaries in others."""
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    out = {}
    for field in fields:
        candidate = value.get(field) if isinstance(value, dict) else getattr(value, field, None)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            out[field] = candidate
    return out


def response_usage(value: dict[str, int]) -> dict[str, Any]:
    i, o = value.get("input_tokens", 0), value.get("output_tokens", 0)
    return {
        "input_tokens": i, "output_tokens": o, "total_tokens": i + o,
        "input_tokens_details": {"cached_tokens": value.get("cache_read_input_tokens", 0)},
    }


def new_response(model: str) -> dict[str, Any]:
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response", "created_at": int(time.time()),
        "status": "completed", "model": model, "output": [],
        "usage": response_usage({}), "store": False,
    }


def event(name: str, _placeholder: Any = None, **extra: Any) -> dict[str, Any]:
    return {"type": name, **extra}


def encode_sse(name: str, obj: dict[str, Any]) -> bytes:
    return (f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False, separators=(',', ':'))}\n\n").encode("utf-8")


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise BridgeError("unsupported_input", "只支持文本输入；未能识别消息内容", 422)
    pieces = []
    for part in content:
        if not isinstance(part, dict):
            raise BridgeError("unsupported_input", "消息内容必须为文本片段", 422)
        t = part.get("type")
        if t not in ("input_text", "output_text", "text"):
            # No silent drops of images, audio, files or hosted tool results.
            raise BridgeError("unsupported_input", f"暂不支持内容类型：{str(t)[:48]}", 422)
        if not isinstance(part.get("text"), str):
            raise BridgeError("unsupported_input", "文本片段缺少 text 字段", 422)
        pieces.append(part["text"])
    return "\n".join(pieces)


def validate_tools(value: Any, *, max_chars: int = MAX_TOOL_SPEC_CHARS) -> list[dict[str, Any]]:
    """Validate all Codex tool declarations without an arbitrary 64-tool cutoff.

    Enforce a cumulative serialized-character budget incrementally so a huge
    incoming tool list is rejected before creating an additional huge prompt.
    Never silently drop tools, their descriptions or parameter schemas.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeError("invalid_tools_shape", "tools 必须是 JSON 数组；不能是对象或字符串", 422)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # json.dumps(..., ensure_ascii=False) joins list items with ', '.
    serialized_chars = 2  # opening and closing brackets
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise BridgeError("invalid_tool_entry", f"tools[{index}] 必须是 JSON 对象", 422)
        kind = item.get("type")
        if kind not in ("function", "custom"):
            raise BridgeError("unsupported_tool_type",
                              f"tools[{index}] 类型不受支持；仅允许 function/custom，不支持托管工具", 422)
        name = item.get("name")
        if not isinstance(name, str) or not MODEL_ID.fullmatch(name):
            raise BridgeError("invalid_tool_name", f"tools[{index}] 的 name 无效", 422)
        if name in seen:
            raise BridgeError("duplicate_tool_name", f"tools[{index}] 的工具名称重复", 422)
        seen.add(name)

        description = item.get("description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise BridgeError("invalid_tool_description", f"tools[{index}] 的 description 必须是字符串", 422)
        spec = {"type": kind, "name": name, "description": description}
        if kind == "function":
            parameters = item.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(parameters, dict):
                raise BridgeError("invalid_tool_schema", f"tools[{index}] 的 parameters 必须是对象", 422)
            spec["parameters"] = parameters
        else:
            spec["format"] = item.get("format", {"type": "text"})

        if out:
            serialized_chars += 2  # ', '
        serialized_chars += len(json.dumps(spec, ensure_ascii=False))
        if serialized_chars > max_chars:
            raise BridgeError("tool_spec_too_large",
                              f"工具声明总长度超过本地 {max_chars} 字符限制；"
                              "可调整 IOA_BRIDGE_MAX_TOOL_SPEC_CHARS", 413)
        out.append(spec)
    return out


def conversation_prompt(body: dict[str, Any], *, tools: list[dict[str, Any]],
                        max_chars: int = MAX_INPUT_CHARS) -> str:
    """Flatten OpenAI Responses history without executing any tools in the bridge."""
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise BridgeError("unsupported_input", "instructions 必须为字符串", 422)
    request_input = body.get("input")
    if isinstance(request_input, str):
        parts: list[str] = ["[user]\n" + request_input]
    elif isinstance(request_input, list):
        parts = []
        for item in request_input:
            if not isinstance(item, dict):
                raise BridgeError("unsupported_input", "输入历史必须包含消息对象", 422)
            typ = item.get("type")
            if typ in ("function_call_output", "custom_tool_call_output"):
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise BridgeError("unsupported_input", "工具结果缺少 call_id", 422)
                output = item.get("output")
                if not isinstance(output, str):
                    output = _text_content(output)
                parts.append(f"[tool_result call_id={json.dumps(call_id)}]\n{output}")
            elif typ in ("function_call", "custom_tool_call"):
                call_id, name = item.get("call_id"), item.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise BridgeError("unsupported_input", "工具调用历史字段不完整", 422)
                val = item.get("arguments") if typ == "function_call" else item.get("input")
                parts.append(f"[previous_tool_call {json.dumps({'call_id':call_id,'name':name,'input':val},ensure_ascii=False)}]")
            elif typ in (None, "message"):
                role = item.get("role")
                if role not in ("system", "developer", "user", "assistant", "tool"):
                    raise BridgeError("unsupported_input", "消息角色无效", 422)
                parts.append(f"[{role}]\n{_text_content(item.get('content'))}")
            elif typ in ("reasoning", "refusal"):
                # Never reinterpret hidden reasoning or provider-private blobs as input text.
                continue
            else:
                raise BridgeError("unsupported_input", f"暂不支持输入项目类型：{str(typ)[:48]}", 422)
    else:
        raise BridgeError("unsupported_input", "input 必须为字符串或消息列表", 422)
    if instructions:
        parts.insert(0, "[instructions]\n" + instructions)
    if not parts:
        raise BridgeError("unsupported_input", "缺少可发送的文本消息", 422)
    prompt = "\n\n".join(parts)
    if len(prompt) > max_chars:
        raise BridgeError("input_too_large", "转换后提示词超过本地字符限制；请压缩历史或调整 IOA_BRIDGE_MAX_PROMPT_CHARS", 413)
    return prompt


def tool_prompt(prompt: str, tools: list[dict[str, Any]], choice: Any) -> str:
    """EXPERIMENTAL: express Codex tools as data in the Agent prompt.

    Not equivalent to a model-native function calling API. The implementation
    validates tool name, arguments and turn handoff before emitting a call.
    """
    rule = (
        "You are the reasoning backend for an external coding agent. "
        "The external agent alone executes tools, obeying its own user approvals. "
        "Never claim that you ran a tool. Return EXACTLY one JSON object and no Markdown. "
        "To answer: {\"kind\":\"message\",\"text\":\"...\"}. "
        "To request exactly one tool: {\"kind\":\"tool_call\",\"name\":\"tool_name\","
        "\"arguments\":{...}} for a function tool, or {\"kind\":\"tool_call\","
        "\"name\":\"tool_name\",\"input\":\"...\"} for a custom tool. "
        "Never invent tool names or report fabricated tool results. "
        "If a prior tool result exists, use it to continue."
    )
    if choice == "required":
        rule += " You MUST request a tool and not answer in this turn."
    elif isinstance(choice, dict):
        target = choice.get("name") or (choice.get("function") or {}).get("name")
        if not isinstance(target, str) or target not in {t["name"] for t in tools}:
            raise BridgeError("unsupported_tool_choice", "请求了未声明的工具", 422)
        rule += f" You MUST call the tool named {json.dumps(target)} in this turn."
    elif choice not in (None, "auto", "none"):
        raise BridgeError("unsupported_tool_choice", "不支持当前 tool_choice", 422)
    return rule + "\nAvailable tools JSON:\n" + json.dumps(tools, ensure_ascii=False) + "\n\nConversation:\n" + prompt


def parse_tool_answer(answer: str, tools: list[dict[str, Any]], choice: Any) -> tuple[str, Any]:
    """Strict output validation: malformed tool output is an error, never executable."""
    try:
        payload = json.loads(answer.strip())
    except (ValueError, TypeError):
        raise BridgeError("tool_protocol_error", "模型未按实验性工具协议返回 JSON", 502) from None
    if not isinstance(payload, dict):
        raise BridgeError("tool_protocol_error", "工具协议响应必须是对象", 502)
    if payload.get("kind") == "message":
        if choice == "required" or isinstance(choice, dict):
            raise BridgeError("tool_protocol_error", "模型未按指定策略调用工具", 502)
        text = payload.get("text")
        if not isinstance(text, str):
            raise BridgeError("tool_protocol_error", "模型消息缺少字符串 text", 502)
        return "message", text
    if payload.get("kind") != "tool_call":
        raise BridgeError("tool_protocol_error", "模型返回未知工具协议类型", 502)
    name = payload.get("name")
    tool = next((t for t in tools if t["name"] == name), None)
    if tool is None or choice == "none":
        raise BridgeError("tool_protocol_error", "模型请求了未授权或未声明的工具", 502)
    if isinstance(choice, dict):
        wanted = choice.get("name") or (choice.get("function") or {}).get("name")
        if wanted != name:
            raise BridgeError("tool_protocol_error", "模型未调用指定工具", 502)
    if tool["type"] == "function":
        args = payload.get("arguments")
        if not isinstance(args, dict):
            raise BridgeError("tool_protocol_error", "函数参数必须是 JSON 对象", 502)
        return "function_call", {"name": name, "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":"))}
    val = payload.get("input")
    if not isinstance(val, str):
        raise BridgeError("tool_protocol_error", "自定义工具输入必须是字符串", 502)
    return "custom_tool_call", {"name": name, "input": val}
