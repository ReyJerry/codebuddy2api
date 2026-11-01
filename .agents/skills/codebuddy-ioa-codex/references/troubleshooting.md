# Error matrix and evidence collection

Every diagnostic starts by identifying **which hop** failed: Codex App → Magpie (~3425) → local bridge (~8791) → official SDK → authorized provider. An error whose URL is `127.0.0.1:3425/v1/responses` may have originated downstream; examine Magpie Routing / usage and the bridge console before attributing it.

| Signal | Likely cause | Safe action |
|---|---|---|
| `zsh: command not found: codex` | Codex App installed, CLI not installed | Use the App; CLI is optional. Do not reinstall without request. |
| 404 `Not Found`, `/v1/responses` | Magpie sent Chat Completions to a Responses-only bridge or wrong base URL | In custom Provider set **only OpenAI Responses** URL `http://127.0.0.1:8791/v1`, clear Chat URL; restart Codex session. |
| 401 `invalid_api_key` | Magpie/terminal local bridge key != running 8791 process's key | Check `IOA_BRIDGE_KEY` in the bridge process *without printing it*. Use `copy-key` from the stable local key file, restart bridge if necessary. Never replace official iOA key as a fix. |
| 404 `model_not_listed` | Model not in local `/v1/models` discovery | Check `probe`. Set `IOA_BRIDGE_MODELS` only to an ID owner has verified in their authorized SDK; local allowlist cannot grant entitlements. Restart. |
| 413 `请求体超过本地字节限制` | HTTP request bytes too large | Check local code version, then reduce unnecessary historical blobs or adjust `IOA_BRIDGE_MAX_BODY_MB` (bounded). No silent truncation. |
| 413 `工具声明超过...` | Tool schema char budget reached | Confirm new version and identify schema payload; raise `IOA_BRIDGE_MAX_TOOL_SPEC_CHARS` within supported bounded range if needed, do not discard tool schemas. |
| 422 `工具列表格式错误或过长` | **Old** bridge 64-tool cap or malformed request | Update fast-forward-only to the 2026-10-31 fix; new version has no fixed 64-tool count. If still 422, inspect returned `code` and tool types without logging secrets. |
| 422 `tools_not_enabled` | Experimental tools deliberately off | With owner approval, restart with `--experimental-tools` or `IOA_BRIDGE_EXPERIMENTAL_TOOLS=1`. Experimental, not proven native function calling. |
| 422 `unsupported_tool_type` | The `tools[N]` item has an unsupported **type**; not a size limit. Earlier bridges rejected Codex `namespace` / `tool_search` / `web_search`. | Fetch an updated bridge; log only sanitized tool type and index, not the full schemas. Modern bridge supports namespaces and *client* tool search, not hosted search. |
| 422 `hosted_tool_unavailable` | A client forced vendor-hosted `web_search`, or requires a tool while only unsupported hosted tools were advertised. | Use another explicitly authorized search tool or provider. Do not fake search results or turn a hosted tool into a shell command. |
| `ioa_unavailable_hosted_tools=web_search` | Modern bridge continued a coding request after advertising, **not implementing**, provider-hosted web search. | No live search/citations occurred. Do not claim search results; configure an actually capable backend if required. |
| 502 `tool_protocol_error` on tool discovery | Prompt adapter generated incorrect `tool_search_call` JSON/arguments, or called a deferred tool before discovery. | Inspect nonsecret event types and tool records. Modern bridge supports `execution=client` and handles `tool_search_output`, but full protocol remains experimental. |
| 422 `unsupported_history` | `previous_response_id` unsupported | Use full input history; don't silently discard state. |
| `request_user_input is unavailable in Default mode` | Codex client tool unavailable in its mode; possibly stale read-only plan instructions | Use ordinary text to request owner consent. Verify bridge uses SDK `permission_mode=default` (not `plan`) and start a new Codex session; don't bypass Codex approvals. |
| 502 `tool_protocol_error` | Model generated invalid JSON or invalid tool name/arguments | Review nonsecret model output in safe test environment; reduce tool complexity; do not auto-run fabricated commands. |
| 502 `sdk_failed` / `sdk_execution` | Official SDK error or auth/backend mismatch | Check minimal *approved* model, SDK environment `ioa`, official key and provider availability. Avoid leaking exception text or repeated paid retries. |
| Model replies “executed” but App has no tool row | Hallucinated execution, no real tool call | Fail verification. Demand actual `exec_command` / `apply_patch` call, exit code and output. |

## Minimal diagnostics

From the installed skill:

```bash
python3 scripts/bridge_setup.py doctor
python3 scripts/bridge_setup.py probe
python3 scripts/bridge_setup.py magpie
```

If installed globally, use the full script path in `~/.agents/skills/codebuddy-ioa-codex/scripts/`. Run `probe --invoke` only with owner consent and the `--accept-charges` flag.

Inspect only sanitized logs: no `Authorization` headers, `.env`, iOA keys, cookies, internal prompts, private corporate URLs or files. If a fix needs changing tool limits or model selection, record which layer's limit was hit. If a request failed **after** reaching the model backend, treat billing as possible even when the HTTP response is an error.


## Codex App screenshot / input_image (2026-11)

When a Responses request contains `input_image`, upgrade the SDK bridge to its
native image-block adapter. The bridge accepts **Base64 data-image URLs** in user
messages and tool results, and sends one ordered CodeBuddy SDK stream-json user
message with `{"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}`.
It never drops image pixels or substitutes fabricated text descriptions. Text
requests retain the existing SDK string-prompt path.

Supported: PNG, JPEG, WebP, GIF, max 12 MiB/image, 32 MiB combined, 128 images
per request. A `422 unsupported_image_reference` means the client sent a URL or
opaque file reference, not an embedded Base64 data URL. The bridge does **not**
download network URLs or read arbitrary files. `422 invalid_image` means
malformed Base64 or a mismatched image signature. For these errors, do not
instruct the model to hallucinate the image or silently strip it.

Even successful image transfer does not prove the current iOA model is
vision-capable. A non-vision model may ask the SDK to use ViewImage; SDK-native
tools are deliberately disabled, so check a live authorized image-test prompt.
Never auto-run chargeable requests without consent. After upgrading, restart
the bridge on 8791, then retry a synthetic non-sensitive image in Codex App.


## Codex App modern Responses input-item errors (2026-11-01)

- `422 ... agent_message`: Update to the adapter version exposed at authenticated `GET http://127.0.0.1:8791/v1/compatibility`. Plain MultiAgentV2 task delegation MUST be transferred as a labelled user-message turn, retaining author/recipient; dropping it makes subagents lose their task.
- `agent_reasoning` / `reasoning`: model-private content is not replayed; the response metadata counts intentional omissions. Never pass hidden reasoning as fabricated user instructions.
- `unsupported_encrypted_history`: an OpenAI/Codex-sealed `agent_message` or compaction cannot be decrypted by the CodeBuddy SDK. Never strip the ciphertext to force HTTP 200. Ask the user to start a new task using an explicit plaintext summary, or select a provider that can handle the sealed content natively.
- `unavailable_compaction_summary`: an old compressed context has no portable plaintext summary; request a plaintext handoff instead of pretending to restore context.
- `unsupported_item_reference`: item depends on a remote Responses store that the stateless local bridge does not have. Send full input or start a summarized session.
- `unsupported_visual_tool_output`: tool history contains an image form the SDK cannot faithfully receive; do not replace it with fabricated description.
- New safe text-bearing input types are labelled as history and can be handled without a one-by-one parser release; unrecognizable executable or opaque items still fail with an explicit index/type.

See [modern Responses item reference](response-items.md) and the project's [compatibility matrix](https://github.com/ReyJerry/codebuddy2api/blob/codex-skill-v1/docs/ioa-responses-compatibility.md).


### v3: Model requests `tool_search_call` without a declared discovery tool

- **Root cause:** Previous versions always included a `tool_search_call` JSON example in the model prompt even if Codex had provided only function/custom tools. Astra could then ask to discover tools it was never authorized to use.
- **Fix:** Update to adapter revision `2026-11-01-responses-compat-v3`. The prompt now only describes actually advertised tools and discovery capabilities.
- **Safe degradation:** In automatic tool-choice mode, if the model still asks for undeclared discovery, Bridge returns a visible diagnostic message with `metadata.ioa_tool_protocol_notice=unadvertised_tool_search`. **No discovery or command was executed; the coding task is not finished.** Required/specific tool-choice calls still fail closed.
- The Bridge does **not** automatically retry paid inference or make up a replacement command. Verify the exact active revision through authenticated `GET /v1/compatibility`, then test Codex Shell/Patch in a disposable workspace.
