# Codex modern Responses wire items

Use the project's [full compatibility matrix](https://github.com/ReyJerry/codebuddy2api/blob/codex-skill-v1/docs/ioa-responses-compatibility.md)
to diagnose unknown `input[]` item types. The bridge uses explicitly
loss-aware conversion, **not** a JSON passthrough to a genuine OpenAI Responses
server.

- Plain `agent_message` in Codex MultiAgentV2 carries delegated instructions;
  it is now converted and MUST NOT be ignored. OpenAI Codex issue:
  https://github.com/openai/codex/issues/33551 .
- `agent_reasoning`/`reasoning` is model-private. The bridge records the
  omitted count instead of inventing reasoning text.
- A nonempty `encrypted_content` in a task, tool replay or compaction is
  **not decryptable** by the official CodeBuddy Python SDK. Never bypass
  this error by deleting encrypted items or reinterpreting the ciphertext.
- `item_reference` needs provider-side input-item storage; the bridge has none.
- Historic Shell/Patch/MCP and tools are **recorded history**, not permission
  to execute. Client tools run only in explicitly enabled experimental mode.
- For further 422 errors, collect the **type name and field names only**.
  Do not ask for private source, API keys, ciphertext or entire HTTP payloads.
- If an old Codex session contains unportable encrypted state, have the owner
  explicitly agree to a **new session with a plaintext handoff summary**;
  do not claim exact history/state restoration.
- Run `uv run --locked python -B -m pytest -q tests/test_ioa_bridge.py`
  (offline) before any authorized real Codex App test. Real upstream access
  may charge AI TOKEN; do not auto-retry failures.

The latest Bridge exposes an authenticated, read-only compatibility endpoint:

```bash
curl -sS http://127.0.0.1:8791/v1/compatibility \
  -H "Authorization: Bearer $IOA_BRIDGE_KEY" | python3 -m json.tool
```

Probe it before diagnosing Codex `agent_message` failures; an older running
process may still be using the previous strict input union even after `git pull`.
No model call/AI TOKEN is spent by this endpoint.
