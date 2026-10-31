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
image/audio/file input, hosted tools, previous-response storage, WebSockets or
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
