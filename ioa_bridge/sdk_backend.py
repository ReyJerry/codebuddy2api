"""Official CodeBuddy Agent SDK backend; never handles credential files or raw RPC."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator

from .core import BridgeError, number_usage


async def _deny_all_tools(_name: str, _input: dict[str, Any], _options: Any) -> Any:
    from codebuddy_agent_sdk import PermissionResultDeny
    return PermissionResultDeny(message="外部客户端自行执行工具，本地桥接不允许 SDK 工具执行", interrupt=True)


def _chunk_delta(message: Any) -> str:
    """Extract only actual SDK stream text deltas, never tool/thinking bytes."""
    evt = getattr(message, "event", None)
    if not isinstance(evt, dict) or evt.get("type") != "content_block_delta":
        return ""
    delta = evt.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


class SDKBackend:
    def __init__(self, *, cwd: Path, api_key: str, timeout_seconds: int = 150):
        self.cwd = cwd
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def generate(self, prompt: str, model: str, *, stream: bool,
                       session_id: str | None = None) -> AsyncIterator[tuple[str, Any]]:
        """Yield ('delta', text), then ('result', metadata). SDK subprocess only."""
        try:
            from codebuddy_agent_sdk import (
                query, CodeBuddyAgentOptions, StreamEvent, AssistantMessage,
                ResultMessage, TextBlock,
            )
        except ImportError:
            raise BridgeError("sdk_missing", "未安装 codebuddy-agent-sdk", 503) from None

        options = CodeBuddyAgentOptions(
            model=model,
            tools=[],              # Deliberately omit built-in tool *definitions*.
            allowed_tools=[],
            can_use_tool=_deny_all_tools,
            # Codex owns file-edit approval. SDK must not inject plan-only instructions.
            permission_mode="default",
            max_turns=1,
            cwd=str(self.cwd),     # A stable, isolated directory; no project configs.
            setting_sources=[],
            include_partial_messages=stream,
            persist_session=bool(session_id),
            session_id=session_id,
            env={"CODEBUDDY_API_KEY": self.api_key,
                 "CODEBUDDY_INTERNET_ENVIRONMENT": "ioa",
                 "CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS": "1"},
        )
        partial = ""
        final = ""
        result = None
        try:
            # A streaming SDK query is kept within the same task until closed.
            # Do not raise inside the async-for message-processing body.
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, StreamEvent):
                    if stream:
                        text = _chunk_delta(message)
                        if text:
                            partial += text
                            yield "delta", text
                elif isinstance(message, AssistantMessage):
                    chunks = [b.text for b in message.content if isinstance(b, TextBlock)]
                    final = "".join(chunks) or final
                elif isinstance(message, ResultMessage):
                    result = message
        except GeneratorExit:
            raise
        except Exception as exc:
            # Never return an SDK exception's text, which may contain credentials,
            # internal hostnames, paths or prompts.
            name = type(exc).__name__
            category = "auth" if "Auth" in name else "sdk_execution"
            raise BridgeError(category, f"SDK 调用未完成（{name[:48]}）", 502) from None

        if result is None:
            raise BridgeError("sdk_incomplete", "SDK 未返回完成状态", 502)
        if getattr(result, "is_error", True):
            raise BridgeError("sdk_failed", "SDK 返回执行失败；请在本机安全查看日志", 502)
        if not final and isinstance(getattr(result, "result", None), str):
            final = result.result
        if partial and final and final.startswith(partial):
            final_suffix = final[len(partial):]
        elif not partial:
            final_suffix = final
        else:
            # Avoid emitting duplicate or out-of-order streamed assistant text.
            final_suffix = ""
        if final_suffix:
            yield "delta", final_suffix
            partial += final_suffix
        if not partial:
            raise BridgeError("sdk_empty", "模型未返回文本输出", 502)
        yield "result", {
            "text": partial,
            "usage": number_usage(getattr(result, "usage", None)),
            "num_turns": getattr(result, "num_turns", None),
            "api_duration_ms": getattr(result, "duration_api_ms", None),
        }
