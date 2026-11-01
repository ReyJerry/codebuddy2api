# CodeBuddy iOA 1M context: what is and is not configured

The default bridge configuration sets `IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000`. This is passed as `CODEBUDDY_AUTO_COMPACT_WINDOW=1000000` in `CodeBuddyAgentOptions.env` for each SDK request. No change is needed for this default.

**Not the same as changing the model context tier.** CodeBuddy Code official ACP protocol implements real session context tiers with `getConfigOptions` / `setSessionConfigOption('context_window', value)`, only when the model reports multiple supported lengths. The public Python SDK `CodeBuddyAgentOptions` used in this bridge does **not** expose that selector. The bridge must not assert that iOA `gpt-6-astra` is actually in 1M mode just because the auto-compaction baseline is 1M. The CLI takes the minimum with the effective model budget.

The owner may customize the auto-compaction baseline (range 100000–1000000) before starting the bridge:

```bash
export IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

Codex has an **independent** `model_context_window` token budget in `~/.codex/config.toml`, for example `model_context_window = 1000000`. Only recommend applying this value when the exact provider/model supports 1M; never write it to another user's existing config automatically. Codex-side budgeting does not change the upstream API's model tier or maximum accepted tokens.

The bridge's `IOA_BRIDGE_MAX_BODY_MB`, `IOA_BRIDGE_MAX_PROMPT_CHARS`, `IOA_BRIDGE_MAX_TOOL_SPEC_CHARS` are local size ceilings in bytes/characters, not context tokens. A 1M preference does not remove these guardrails, guarantee caching, prevent charges, or excuse unverified claims.

Official sources:
- https://www.codebuddy.cn/docs/cli/acp
- https://www.codebuddy.ai/docs/cli/sdk-python
- https://www.codebuddy.ai/docs/cli/settings
- https://learn.chatgpt.com/docs/config-file/config-reference
