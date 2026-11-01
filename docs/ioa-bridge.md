# Local CodeBuddy iOA Agent SDK → Magpie / Codex Bridge (experimental)

This opt-in service lives inside `ReyJerry/codebuddy2api` but runs independently
on **127.0.0.1:8791**. The original web dashboard, `.info` credentials, billing
ledger and standard `converter.py` routes are not modified or read.

## Capabilities

- Official iOA SDK Key path (`CODEBUDDY_API_KEY` plus `CODEBUDDY_INTERNET_ENVIRONMENT=ioa`).
- `tools=[]`, isolated stable working directory, no CodeBuddy built-in tool execution.
- `/v1/models` discovered from the SDK-bundled headless CLI `--help`, or from an
  explicitly configured, locally verified model allowlist (`IOA_BRIDGE_MODELS`).
- `/v1/responses` JSON and OpenAI-style SSE events, including SDK `StreamEvent` text deltas.
- Numeric SDK token usage, including cache read/creation counts as metadata.
- Bound, opt-in delta Sessions, rotation and concurrency rejection.
- Strict, disabled-by-default **experimental** prompt-JSON mapping for Responses
  `function_call` / `custom_tool_call`. The bridge never executes client tools.

It **does not** implement native model function calling, full Codex tool fidelity,
audio/file input, remote image URL retrieval, hosted tools, previous-response storage, WebSockets or
strongly consistent multi-process session recovery. Unsupported features fail
explicitly instead of being silently discarded.

The user's verified SDK call for one permitted model was billed in their AI TOKEN
account; this does not establish the billing source of other models. SDK
`total_cost_usd=0` does not imply free inference. Pricing multipliers remain unverified.

## Installation (macOS, no Docker)

From the repository root:

```bash
uv sync --locked --no-build --python 3.12
export CODEBUDDY_INTERNET_ENVIRONMENT=ioa
export CODEBUDDY_API_KEY='YOUR_OFFICIAL_IOA_KEY'
export IOA_BRIDGE_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
# Optional if the CLI help cannot be parsed:
export IOA_BRIDGE_MODELS='YOUR_VERIFIED_MODEL_ID'
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

The SDK is brought into a temporary uv dependency layer; it is deliberately
**not** added to the base project's lockfile, Docker image, or runtime. Do not
commit the API key or place it in a tracked configuration file. The launcher
refuses to start when `CODEBUDDY_AUTH_TOKEN` is also set, because it would take
precedence over the official API Key in the SDK.

Local API examples (from a second terminal with `IOA_BRIDGE_KEY` set):

```bash
curl -H "Authorization: Bearer $IOA_BRIDGE_KEY" \
  http://127.0.0.1:8791/v1/models
curl -H "Authorization: Bearer $IOA_BRIDGE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"YOUR_VERIFIED_MODEL_ID","input":"Reply OK","stream":true}' \
  http://127.0.0.1:8791/v1/responses
```

## Long context: 1M-token models versus local HTTP limits

To support resumed Codex sessions, the bridge now defaults to a **128 MiB request-body**
limit and a **16,000,000-character converted-prompt** limit (previously about 2 MB
and 250,000 characters). These are *local capacity ceilings*, not a guarantee that
an upstream model will accept 1M tokens. Token counts differ from UTF-8 bytes and
characters, and the SDK inserts additional system context. No truncation occurs.

If needed, set the following in the **bridge process environment**, then restart:

```bash
export IOA_BRIDGE_MAX_BODY_MB=256
export IOA_BRIDGE_MAX_PROMPT_CHARS=32000000
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

Allowed values: `IOA_BRIDGE_MAX_BODY_MB` 1–256 (MiB); `IOA_BRIDGE_MAX_PROMPT_CHARS`
100,000–32,000,000. Unset values use the defaults. Raising these values does not
bypass Magpie's own limits or the model's actual context window. Extremely long
histories may incur significant upstream usage. Request bytes are read incrementally
with early rejection once the configured limit is reached.


### Codex tool schema budget (independent of HTTP body budget)

Codex App with MCP can send more than 64 tools. The old `MAX_TOOLS=64`
incorrectly reported a generic 422 for this case. There is now **no fixed tool
count limit**; all `function`/`custom` declarations are validated, with their
serialized sizes counted incrementally against a bounded overall budget.
No tool schemas, descriptions or parameters are silently dropped. Invalid
list shapes, duplicate names and unsupported types have distinct 422 codes.

The combined tool schema budget is **8,000,000 characters** by default, with a
configurable range of 1,000–32,000,000. Set a higher value before restarting:

```bash
export IOA_BRIDGE_MAX_TOOL_SPEC_CHARS=16000000
export IOA_BRIDGE_EXPERIMENTAL_TOOLS=1
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

HTTP body limit remains 128 MiB by default (max configured 256 MiB); the
converted combined prompt limit remains 16M chars by default (max 32M).
Tool schemas count toward the combined prompt. These limits protect the local
process against unbounded memory use and are **not equivalent to unlimited
model context**. Oversized requests fail with explicit 413 codes; `/compact`
does not shrink tool schemas. Large schemas consume real upstream tokens and
potential AI TOKEN credits. The JSON prompt adapter is **not native function
calling** and must be used only in authorized non-sensitive test projects.

## Magpie / Codex

Add a Custom OpenAI Responses provider in Magpie:

- Base URL: `http://127.0.0.1:8791/v1`
- API Key: the local `IOA_BRIDGE_KEY` value, **not** the corporate SDK Key
- Model ID: a model returned by `/v1/models`, confirmed authorized separately

Or configure Codex directly:

```toml
[model_providers.ioa_sdk_local]
name = "iOA Agent SDK local (experimental)"
base_url = "http://127.0.0.1:8791/v1"
wire_api = "responses"
env_key = "IOA_BRIDGE_KEY"
supports_websockets = false
requires_openai_auth = false

[profiles.ioa_sdk_text]
model = "YOUR_VERIFIED_MODEL_ID"
model_provider = "ioa_sdk_local"
```

With default settings, a tool-bearing Codex request fails **422**
`tools_not_enabled`. To opt in for **testing on synthetic/non-sensitive
repositories only**, set `IOA_BRIDGE_EXPERIMENTAL_TOOLS=1` and restart. This
uses a strict JSON prompt protocol, not SDK/MCP native tool calling; real Codex
compatibility is not yet established.

## Caching and bounded Sessions

Full-history Responses callers (including typical Codex requests with
`store=false`) run stateless to avoid duplicating transcript context in SDK
history. For **delta-only** clients, add header
`X-IOA-Delta-Session: conversation_001`; only a single user message is
accepted. Sessions rotate after 4 turns, 24K reported input tokens, 15 minutes
idle, or a model change. Concurrent use of the same key is rejected (409).
Persistent SDK sessions are enabled only for this explicit mode. There is no
automatic full-history deduplication, and the server does not infer financial
cost from reported token counts.

## Future native tool integration design

Use SDK MCP custom tool handlers with strict `tool_use_id ↔ call_id` mapping,
retain an SDK Client + pending Future across two HTTP Responses requests, let
Codex execute the approved tools, and resume the SDK after validating the
returned call ID and output. The design must implement cancel, expiry,
reconnect, concurrency and exactly-once tool effects before it can be called
complete. This is **not implemented** in this first step.

## Offline tests

```bash
uv run --locked python -B -m pytest -q tests/test_ioa_bridge.py
```

The tests use synthetic model responses; no network, corporate credentials,
or AI TOKEN consumption. Operate the bridge only where corporate policy allows
such a client and data flow. Keep localhost binding, never share keys/logs, and
never place private endpoint configuration in a public repository.

### Codex App default mode versus SDK plan mode

The SDK now uses `permission_mode="default"` to avoid injecting read-only plan-mode instructions into Codex tool generation. SDK native tools remain disabled (`tools=[]`, `allowed_tools=[]`, deny-all callback); **Codex App remains responsible for execution and approvals**. `request_user_input` can be unavailable in Codex Default mode; use a plain assistant message to request clarification. Test edits only in a disposable non-sensitive workspace.


## Modern Codex Responses tool declarations (experimental, Nov 2026)

Codex may now send `function`, `custom`, `namespace`, client-executed
`tool_search`, and `web_search` in the same `tools` array. These are not interchangeable.

- `function`/`custom`: existing experimental prompt-JSON to Responses tool call adapter.
- `namespace`: preserve the `namespace` field in function/custom tool calls; permit
  same names across different namespaces.
- `tool_search` with `execution=client`: emit a real Responses `tool_search_call`
  containing an **object** `arguments` field, so Codex performs discovery.
  Accept `tool_search_output` in subsequent history. Deferred tools cannot be called
  before their discovery has been returned. This is still a prompt-based adapter,
  not native hosted OpenAI tool search.
- `web_search`/`web_search_preview`: **not implemented** by this CodeBuddy bridge.
  They are explicitly marked unavailable in response metadata
  (`ioa_unavailable_hosted_tools`) and in the model prompt; live results/citations
  must never be fabricated. Forced hosted search fails as `422 hosted_tool_unavailable`.
  Use another authorized tool or a provider with native hosted web search when needed.
- Other unknown/hosted types fail with `422 unsupported_tool_type`, identifying
  their JSON index and type. Tool declarations are never secretly discarded.

The reported `tools[4]` denotes an array index, **not** a known tool kind.
Re-verify tool discovery, namespace calls, and client Shell/Patch execution with
real Codex App versions before use with important data.


## Codex App image input (`input_image`)

The bridge now converts OpenAI Responses `input_image` **Base64 data URLs** into
CodeBuddy SDK stream-json `image` blocks. Text and image ordering are preserved.
PNG, JPEG, WebP, and GIF are accepted; the local resource guard is 12 MiB per
image, 32 MiB aggregate per request, and 128 images. Image bytes are neither
inserted into the text prompt nor stored in project files.

Remote URLs (`https://`, `file://`) and opaque `file_id` references fail explicitly
with `422 unsupported_image_reference`; no server-side URL fetch/SSRF or arbitrary
local file read. Invalid/corrupt declarations fail before a paid SDK call.

**The upstream model still needs actual vision capability**. Some CodeBuddy
versions turn user images for non-vision models into local ViewImage hints; this
bridge deliberately disables SDK-native tools, so such hints do not guarantee
image analysis. The SDK stream-json message shape is documented, but the
bridge's end-to-end image route still requires an authorized live smoke test.
Restart port 8791 after upgrading.


## 1M context preference versus an actual model context tier

The bridge defaults to `IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000`, passed to the
official CLI as `CODEBUDDY_AUTO_COMPACT_WINDOW`. This configures the
**auto-compaction baseline only**. It is **not** a model-context-window override
and does not prove that a particular iOA model or account has a 1M-token tier.
The CodeBuddy runtime clamps auto-compaction to the selected model's actual
effective context budget. The bridge never silently truncates the Codex history.

To change the baseline (range 100000–1000000), restart the local bridge:

```bash
export IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

The upstream 200K/1M **context tier** is currently exposed through
CodeBuddy's ACP `context_window` session config option, which validates the
model's `contextWindow.supportedLengths`. The public Python SDK
`CodeBuddyAgentOptions` does not expose a corresponding `context_window`
parameter. This SDK-backed bridge therefore cannot currently select or verify
the actual 1M tier. Do not equate `--autocompact 1m` with model-tier selection.

Codex offers its own `model_context_window = 1000000` config. Only set that
when the selected provider/model's true 1M context support has been verified;
it changes the client's budgeting, not the upstream model's capabilities.

References: [Official SDK](https://www.codebuddy.ai/docs/cli/sdk-python),
[ACP context tiers](https://www.codebuddy.ai/docs/cli/acp),
[CodeBuddy auto-compact settings](https://www.codebuddy.ai/docs/cli/settings).

## Modern Codex Responses item compatibility (2026-11-01)

The adapter now normalizes **plaintext** `agent_message` task delegation and
replies (with `author`/`recipient`), as well as supported history events,
client tool search, plain compaction summaries and text-bearing new item types.
Private `reasoning` items are not replayed and their count is reported in
`metadata.ioa_compat_omitted_private_reasoning`.

It **cannot** decrypt Codex/OpenAI `encrypted_content`, reconstruct a remote
`item_reference`, or invent a plaintext summary for a sealed compaction. These
cases fail with explicit errors before upstream inference. Hosted web/file
search is still unavailable through this bridge, even if its tool definition
appears in a Codex request. Do not treat a successful request as proof of
native SDK tools or live search. See [the full compatibility matrix](ioa-responses-compatibility.md).

Read-only compatibility inspection (no paid model call):

```bash
curl -sS http://127.0.0.1:8791/v1/compatibility \
  -H "Authorization: Bearer $IOA_BRIDGE_KEY" | python3 -m json.tool
```


### Unexpected tool search when Codex did not advertise it (2026-11-01 v3)

Before v3, the experimental prompt always included the `tool_search_call` JSON
example, even when Codex provided only `exec_command`/`apply_patch`. The model
could then request an unadvertised tool and produce a 502 after model billing.
New prompts only describe tool kinds declared by the current client request.
For unsolicited discovery in auto mode, the bridge returns a **visible notice**
(`metadata.ioa_tool_protocol_notice=unadvertised_tool_search`) rather than
pretending a tool was executed or retrying a paid call. This notice is not a
successful coding result. Required/specific tool choice still fails closed.
