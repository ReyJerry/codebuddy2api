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


# These are optional OpenAI-hosted capabilities, not executable Codex tools.
# Accept their declarations only as *unavailable* so ordinary coding requests can
# proceed, while explicitly warning the model and client. No simulated web results.
_UNAVAILABLE_HOSTED = frozenset({"web_search", "web_search_preview"})


def validate_tools(value: Any, *, max_chars: int = MAX_TOOL_SPEC_CHARS) -> list[dict[str, Any]]:
    """Validate Codex callable, namespace, discovery and optional hosted tools.

    Client-executed tool_search is a distinct Responses item, not a function.
    Web search is *not* performed by this SDK bridge. Record it as unavailable
    instead of pretending it is executable or silently removing it.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeError("invalid_tools_shape", "tools 必须是 JSON 数组；不能是对象或字符串", 422)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    serialized_chars = 2

    def append_spec(spec: dict[str, Any], where: str) -> None:
        nonlocal serialized_chars
        key = (spec.get("namespace") or "functions", spec["name"])
        if key in seen:
            raise BridgeError("duplicate_tool_name", f"{where} 的工具名称重复：{key[1][:80]}", 422)
        seen.add(key)
        size = len(json.dumps(spec, ensure_ascii=False)) + (2 if out else 0)
        if serialized_chars + size > max_chars:
            raise BridgeError("tool_spec_too_large",
                              f"工具声明总长度超过本地 {max_chars} 字符限制；"
                              "可调整 IOA_BRIDGE_MAX_TOOL_SPEC_CHARS", 413)
        serialized_chars += size
        out.append(spec)

    def callable_spec(item: dict[str, Any], where: str,
                      namespace: str | None = None) -> dict[str, Any]:
        kind = item.get("type")
        if kind not in ("function", "custom"):
            raise BridgeError("unsupported_tool_type",
                              f"{where} 的 type={str(kind)[:64]!r} 暂不支持", 422)
        name = item.get("name")
        if not isinstance(name, str) or not MODEL_ID.fullmatch(name):
            raise BridgeError("invalid_tool_name", f"{where} 的 name 无效", 422)
        description = item.get("description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise BridgeError("invalid_tool_description", f"{where} 的 description 必须是字符串", 422)
        spec: dict[str, Any] = {"type": kind, "name": name, "description": description}
        if namespace:
            spec["namespace"] = namespace
        if item.get("defer_loading") is True:
            spec["defer_loading"] = True
        if kind == "function":
            parameters = item.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(parameters, dict):
                raise BridgeError("invalid_tool_schema", f"{where} 的 parameters 必须是对象", 422)
            spec["parameters"] = parameters
        else:
            fmt = item.get("format", {"type": "text"})
            if not isinstance(fmt, dict):
                raise BridgeError("invalid_tool_schema", f"{where} 的 format 必须是对象", 422)
            spec["format"] = fmt
        return spec

    for index, item in enumerate(value):
        where = f"tools[{index}]"
        if not isinstance(item, dict):
            raise BridgeError("invalid_tool_entry", f"{where} 必须是 JSON 对象", 422)
        kind = item.get("type")
        if kind in ("function", "custom"):
            append_spec(callable_spec(item, where), where)
        elif kind == "namespace":
            namespace = item.get("name")
            children = item.get("tools")
            if not isinstance(namespace, str) or not MODEL_ID.fullmatch(namespace):
                raise BridgeError("invalid_tool_name", f"{where} 的命名空间名称无效", 422)
            if not isinstance(children, list):
                raise BridgeError("invalid_tool_schema", f"{where}.tools 必须是数组", 422)
            for j, child in enumerate(children):
                child_where = f"{where}.tools[{j}]"
                if not isinstance(child, dict):
                    raise BridgeError("invalid_tool_entry", f"{child_where} 必须是 JSON 对象", 422)
                append_spec(callable_spec(child, child_where, namespace), child_where)
        elif kind == "tool_search":
            if item.get("execution") != "client":
                raise BridgeError("unsupported_hosted_tool",
                                  f"{where} 的 tool_search 仅支持 execution=client；"
                                  "CodeBuddy SDK 不具备服务端托管工具搜索", 422)
            parameters = item.get("parameters")
            if not isinstance(parameters, dict):
                raise BridgeError("invalid_tool_schema", f"{where} 的 parameters 必须是对象", 422)
            description = item.get("description") or "Client tool discovery"
            if not isinstance(description, str):
                raise BridgeError("invalid_tool_description", f"{where} 的 description 必须是字符串", 422)
            append_spec({"type": "tool_search", "name": "tool_search",
                         "execution": "client", "description": description,
                         "parameters": parameters}, where)
        elif kind in _UNAVAILABLE_HOSTED:
            # Keep an explicit marker in the response and prompt; never claim
            # to have searched the internet or supplied fresh citations.
            append_spec({"type": "unavailable_hosted", "name": kind}, where)
        else:
            raise BridgeError("unsupported_tool_type",
                              f"{where} 的 type={str(kind)[:64]!r} 不受此桥接支持；"
                              "请检查是否为托管工具或新 Codex 工具类型", 422)
    return out



def active_tools_for_turn(tools: list[dict[str, Any]],
                          history: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Only expose deferred tools after Codex has returned their discovery.

    The tool-search output is a *description* of client-side discoveries. It
    cannot grant new execution rights: a tool must still be declared in the
    current request. A model cannot call a deferred tool before discovery.
    """
    if not any(t["type"] == "tool_search" for t in tools):
        return tools, []  # Existing clients without tool search are unchanged.
    discovered: set[tuple[str, str]] = set()

    def collect(items: Any) -> None:
        if not isinstance(items, list):
            return
        for entry in items:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "namespace" and isinstance(entry.get("tools"), list):
                namespace = entry.get("name")
                if not isinstance(namespace, str):
                    continue
                for child in entry["tools"]:
                    if isinstance(child, dict) and child.get("type") in ("function", "custom"):
                        name = child.get("name")
                        if isinstance(name, str):
                            discovered.add((namespace, name))
            elif entry.get("type") in ("function", "custom"):
                name, ns = entry.get("name"), entry.get("namespace")
                if isinstance(name, str):
                    discovered.add((ns if isinstance(ns, str) else "functions", name))

    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") in ("tool_search_output", "additional_tools"):
                collect(entry.get("tools"))

    active: list[dict[str, Any]] = []
    deferred: list[dict[str, str]] = []
    for tool in tools:
        key = (tool.get("namespace") or "functions", tool["name"])
        if tool.get("defer_loading") and key not in discovered:
            deferred.append({"name": tool["name"], "namespace": key[0],
                             "description": tool["description"]})
        else:
            active.append(tool)
    return active, deferred


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
                parts.append(f"[previous_tool_call {json.dumps({'call_id':call_id,'name':name,'namespace':item.get('namespace'),'input':val},ensure_ascii=False)}]")
            elif typ == "tool_search_call":
                call_id = item.get("call_id")
                arguments = item.get("arguments")
                if not isinstance(call_id, str) or not call_id:
                    raise BridgeError("unsupported_input", "工具搜索调用缺少 call_id", 422)
                if isinstance(arguments, str):
                    # Some Codex versions replay arguments as a JSON string.
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        raise BridgeError("unsupported_input", "工具搜索调用参数不是有效 JSON", 422) from None
                if not isinstance(arguments, dict):
                    raise BridgeError("unsupported_input", "工具搜索调用参数必须是对象", 422)
                parts.append(f"[previous_tool_search call_id={json.dumps(call_id)}]\n"
                             + json.dumps(arguments, ensure_ascii=False))
            elif typ == "tool_search_output":
                call_id, discovered = item.get("call_id"), item.get("tools")
                if not isinstance(call_id, str) or not isinstance(discovered, list):
                    raise BridgeError("unsupported_input", "工具搜索结果缺少 call_id 或 tools 数组", 422)
                parts.append(f"[tool_search_result call_id={json.dumps(call_id)}]\n"
                             + json.dumps(discovered, ensure_ascii=False))
            elif typ == "additional_tools":
                if item.get("role") != "developer" or not isinstance(item.get("tools"), list):
                    raise BridgeError("unsupported_input", "additional_tools 必须包含 developer 角色及工具数组", 422)
                parts.append("[additional_tool_declarations]\n" + json.dumps(item["tools"], ensure_ascii=False))
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


def tool_prompt(prompt: str, tools: list[dict[str, Any]], choice: Any,
                deferred: list[dict[str, str]] | None = None) -> str:
    """Experimental text protocol, including Codex *client* tool discovery.

    Not native OpenAI tool calling. Hosted web_search remains unavailable;
    it is explicitly reported rather than invented or silently hidden.
    """
    usable = [t for t in tools if t["type"] != "unavailable_hosted"]
    unavailable = [t["name"] for t in tools if t["type"] == "unavailable_hosted"]
    rule = (
        "You are the reasoning backend for an external coding agent. "
        "Only the external Codex client executes tools under its own approvals. "
        "Return EXACTLY one JSON object, no Markdown. "
        "To answer: {\"kind\":\"message\",\"text\":\"...\"}. "
        "To call one function: {\"kind\":\"tool_call\",\"name\":\"tool_name\","
        "\"arguments\":{...}}. "
        "To call one custom tool: {\"kind\":\"tool_call\","
        "\"name\":\"tool_name\",\"input\":\"...\"}. "
        "For a namespaced tool also include \"namespace\":\"namespace_name\". "
        "For client-executed tool discovery: {\"kind\":\"tool_search_call\","
        "\"arguments\":{\"query\":\"search terms\",\"limit\":8}}. "
        "Do not request tool_search_call unless a client tool_search is declared. "
        "For tools with defer_loading=true, request tool_search_call first. "
        "Never invent tool names or tool results. If a previous tool result exists, use it."
    )
    if unavailable:
        rule += (" IMPORTANT: The following OpenAI-hosted tools were advertised "
                 "by the client but are NOT available through this CodeBuddy bridge: "
                 + ", ".join(unavailable)
                 + ". Do not fabricate web results, file search, citations or current facts. "
                 "If the user needs them and no other authorized external tool can do so, "
                 "plainly state that this capability is unavailable.")
    if choice == "required":
        if not usable:
            raise BridgeError("hosted_tool_unavailable", "请求必须使用工具，但仅提供未实现的托管工具", 422)
        rule += " You MUST request an available client tool, not a final answer."
    elif isinstance(choice, dict):
        target = choice.get("name") or (choice.get("function") or {}).get("name")
        kind = choice.get("type")
        namespace = choice.get("namespace")
        if kind == "tool_search" and any(t["type"] == "tool_search" for t in usable):
            rule += " You MUST request a client tool_search_call, not a final answer."
        else:
            if (not isinstance(target, str)
                    or not any(t["name"] == target and (namespace is None or t.get("namespace") == namespace)
                               for t in usable)):
                raise BridgeError("hosted_tool_unavailable", "指定工具不可由客户端执行；此桥接不支持托管工具", 422)
            rule += f" You MUST call the available client tool named {json.dumps(target)} in this turn."
    elif choice not in (None, "auto", "none"):
        raise BridgeError("unsupported_tool_choice", "不支持当前 tool_choice", 422)
    preview = ("\nDeferred tools (must use tool_search before calling):\n"
               + json.dumps(deferred, ensure_ascii=False)) if deferred else ""
    return (rule + "\nAvailable Codex client tools JSON:\n" + json.dumps(usable, ensure_ascii=False)
            + preview + "\n\nConversation:\n" + prompt)


def parse_tool_answer(answer: str, tools: list[dict[str, Any]], choice: Any) -> tuple[str, Any]:
    """Validate model-generated JSON before handing anything to Codex."""
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
    if payload.get("kind") == "tool_search_call":
        if not any(t["type"] == "tool_search" and t["execution"] == "client" for t in tools):
            raise BridgeError("tool_protocol_error", "未声明客户端 tool_search，禁止请求工具搜索", 502)
        if isinstance(choice, dict) and choice.get("type") != "tool_search":
            raise BridgeError("tool_protocol_error", "工具搜索不符合 tool_choice", 502)
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise BridgeError("tool_protocol_error", "tool_search 参数必须是对象", 502)
        query, limit = arguments.get("query"), arguments.get("limit")
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise BridgeError("tool_protocol_error", "tool_search query 无效", 502)
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise BridgeError("tool_protocol_error", "tool_search limit 必须是 1–100 的整数", 502)
        return "tool_search_call", {"execution": "client", "arguments": arguments}
    if payload.get("kind") != "tool_call":
        raise BridgeError("tool_protocol_error", "模型返回未知工具协议类型", 502)
    name = payload.get("name")
    namespace = payload.get("namespace")
    if not isinstance(name, str) or (namespace is not None and not isinstance(namespace, str)):
        raise BridgeError("tool_protocol_error", "工具名称或命名空间无效", 502)
    matches = [t for t in tools if t["name"] == name and t["type"] in ("function", "custom")
               and (namespace is None or t.get("namespace") == namespace)]
    if len(matches) != 1 or choice == "none":
        raise BridgeError("tool_protocol_error", "模型请求了未授权、未声明或命名空间不明确的工具", 502)
    tool = matches[0]
    if isinstance(choice, dict):
        wanted = choice.get("name") or (choice.get("function") or {}).get("name")
        if wanted != name or (choice.get("namespace") is not None
                              and choice["namespace"] != tool.get("namespace")):
            raise BridgeError("tool_protocol_error", "模型未调用指定工具", 502)
    extra = {"namespace": tool["namespace"]} if tool.get("namespace") else {}
    if tool["type"] == "function":
        args = payload.get("arguments")
        if not isinstance(args, dict):
            raise BridgeError("tool_protocol_error", "函数参数必须是 JSON 对象", 502)
        return "function_call", {"name": name, "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":")), **extra}
    val = payload.get("input")
    if not isinstance(val, str):
        raise BridgeError("tool_protocol_error", "自定义工具输入必须是字符串", 502)
    return "custom_tool_call", {"name": name, "input": val, **extra}
