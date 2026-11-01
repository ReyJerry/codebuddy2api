# Codex App ↔ Magpie ↔ iOA Bridge: Responses compatibility matrix (2026-11-01)

The independent bridge uses the **official CodeBuddy Agent SDK** and a text-based
Codex tool-call adapter. It does **not** implement a native OpenAI Responses
backend or decrypt upstream private state. Codex App and Magpie evolve; this
matrix distinguishes what is converted from what remains unavailable.

## Input items and handling

| Codex `input[]` item | Handling | Data loss and caveats |
|---|---|---|
| `message` (user/system/developer/assistant/tool) | Preserve text and source role in the ordered SDK transcript; embedded `input_image` uses native SDK image blocks | No cross-provider role-level guarantees or universal media support |
| `agent_message` (MultiAgentV2) | **Treat plaintext task/reply as a user turn**, retaining the `author` / `recipient` label | Encrypted message payloads cannot be decrypted and are rejected before SDK calls |
| `agent_reasoning`, `reasoning` | Do not replay private chain-of-thought; report omission count in `metadata.ioa_compat_omitted_private_reasoning` | Opaque model reasoning cannot be migrated between providers |
| `function_call`, `custom_tool_call`, their `_output` | Preserve named calls, args, `call_id`, textual results and SDK-native images (when supported) | No duplicate tool execution |
| `tool_search_call`, `tool_search_output`, `additional_tools` | Preserve client-side discovery and its tool descriptions; deferred tools still require discovery | Historical tool descriptions are **not** independent authorization to execute |
| Shell/Patch/MCP/computer/code-interpreter/web-search/file-search/image-generation history, e.g. `apply_patch_call_output`, `mcp_call`, `local_shell_call`, `computer_call_output` | Store as explicitly labelled historical events. Recognized screenshot formats remain image blocks | These items are **not executed** by the bridge; unportable encryption or visual payloads reject |
| `compaction`, `context_compaction` | Convert **plaintext** summary (`summary` or `content`) | Encrypted-only or summary-less compactions fail with a precise error; remote state is not recreated |
| `compaction_trigger` | Control-only, recorded as a count | No substitute summary is invented |
| `configuration_update` | Retained as historical information | Does not change SDK model context/effort settings |
| `item_reference` | Explicit 422 `unsupported_item_reference` | Needs a server-side Responses store; this bridge has none |
| `refusal` | Retain readable refusal text | No invented answers |
| New **text-bearing** item types | Keep their text with original type label; account for them in `metadata.ioa_compat_generic_text_items` | This is history data, **not** an executable tool declaration |
| New opaque/binary/unparseable input items | Descriptive 422 with input index/type | No dropping data to force HTTP 200 |

Hosted *tool declarations* (`web_search`, `file_search`, `image_generation`,
`code_interpreter`, etc.) are marked **unavailable** in the prompt/response
metadata, not invoked. Actual client-hosted `function`, `custom`, and
`tool_search` continue in **experimental mode** only. Do not mistake
`response.completed` for proof of live hosted search.

## Security / compatibility guardrails

- An encrypted `agent_message` or sealed compaction is not portable via the public
  SDK. The bridge returns `unsupported_encrypted_history` **before model
  invocation**. Never decode by guessing or feed ciphertext as if it were the task.
- Existing model-private `reasoning` is not prompt-injected. The response metadata
  counts these omissions, so clients can detect that **full reasoning replay is not
  supported**. Public plaintext agent messages **are** preserved.
- Image URLs must be approved `data:image/...;base64` values. No automatic
  HTTP/local file access. Unsupported screenshots are errors, not made-up pixels.
- Unknown client tool declarations are **not** auto-executable; unknown **history**
  ending in `_call`/`_call_output` is represented as an event only. Failed calls
  are never automatically retried, as usage charges and side effects are possible.
- Request-body, prompt, image and tool-Schema budgets remain bounded. The 1M
  auto-compaction baseline is **not** proof of a 1M iOA context tier.
- A green HTTP status proves protocol conversion, **not** that SDK-native tool
  calling, native encrypted MultiAgentV2 or hosted search work. Always verify a
  real `exec_command` + `apply_patch` in a disposable project.

## Regression tests and validation

Run locally without any iOA credentials or upstream billing:

```bash
uv run --locked python -B -m pytest -q tests/test_ioa_bridge.py
```

The test suite contains synthetic MultiAgentV2 `agent_message` payloads,
plaintext vs encrypted cases, historic tools, multimodal tool output, compaction
and explicit failures for unportable state. Real Codex App + Magpie + iOA
subagent runs still require **authorized, on-device verification**.

Relevant public upstream references:

- Codex MultiAgentV2 `agent_message` and encryption compatibility:
  https://github.com/openai/codex/issues/33551
- Magpie's task-loss diagnosis and plaintext `agent_message` conversion:
  https://github.com/yetone/magpie/issues/4108
- Codex's evolving input-item catalog:
  https://github.com/openai/codex/issues/27005

### Read-only compatibility probe (no paid model call)

With the same local `IOA_BRIDGE_KEY` that the 8791 process uses:

```bash
curl -sS http://127.0.0.1:8791/v1/compatibility \
  -H "Authorization: Bearer $IOA_BRIDGE_KEY" | python3 -m json.tool
```

This returns the adapter revision, recognized item families, bounded local
limits, and `verified_upstream_context_tokens: null`. The latter is deliberate:
an SDK auto-compact budget of 1M does not authenticate a real 1M backend tier.
The probe is read-only and does not invoke the paid iOA model.


## Tool-search guard (2026-11-01 v3)

Codex sometimes supplies only `function`/`custom` tools, but Astra previously
saw an **unconditional** example of `tool_search_call` in the generated system
prompt and could emit an unauthorized discovery request. The bridge rejected
that request after model inference, causing a `response.failed` SSE event and
Magpie's misleading “stream disconnected before completion” error.

The bridge now builds model instructions from **only the callable and discovery
kinds actually advertised in the current `tools[]`**. In particular, absent
client `tool_search` means no discovery JSON example or deferred-discovery
instruction is placed in the prompt; historical discovery items confer no
permission. No `tool_search` is synthesized or executed.

If the model nevertheless requests an unadvertised search with `tool_choice=auto`,
the bridge returns an explicit, truthful assistant notice and sets
`metadata.ioa_tool_protocol_notice=unadvertised_tool_search`. No tool is executed,
no missing tool is invented, and **the model request is not automatically
retried**. With `tool_choice=required` or a specific tool choice, such an
invalid model output continues to fail closed with `tool_protocol_error`.
The notice completes the SSE turn, but it is **not proof that the requested
coding task was completed**. The client/user may start a subsequent turn using
an advertised tool. SDK-native MCP calling remains unimplemented.
