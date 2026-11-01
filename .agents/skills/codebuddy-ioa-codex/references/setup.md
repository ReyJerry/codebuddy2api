# Installation and connection details

> **Scope:** authorized CodeBuddy iOA API Key only. The bridge lives in a feature branch of `ReyJerry/codebuddy2api`, separate from the original SaaS/WebUI gateway. Mac (Codex App) is the verified path; other OSes need local checks.

## Requirements

- A legitimately issued `CODEBUDDY_API_KEY` for CodeBuddy iOA, and permission to send the selected code/data to the internal provider through Magpie / Codex.
- `git`, `python3` (for the helper; Python >=3.12 for the bridge), and `uv`. The helper *checks* for missing tools but never installs programs behind the user's back. If `uv` is missing, follow the official uv installation guide: https://docs.astral.sh/uv/getting-started/installation/ .
- Magpie (GUI preferred), Codex App or CLI. Installing the CLI is optional; `zsh: command not found: codex` does not mean the App is missing.
- Network access permitted to GitHub, PyPI/uv package registries, and the authorized iOA backend. Do not work around enterprise security controls.

## Skill installation for any owner

From Codex, ask `$skill-installer` to install the skill at:

`https://github.com/ReyJerry/codebuddy2api/tree/codex-skill-v1/.agents/skills/codebuddy-ioa-codex`

Alternatively install the **skill folder** into `~/.agents/skills/codebuddy-ioa-codex/` (global) or `<repo>/.agents/skills/codebuddy-ioa-codex/` (repo-scoped). The folder must directly contain `SKILL.md`. Restart Codex if needed, then request `$codebuddy-ioa-codex`.

A downloaded ZIP containing `codebuddy-ioa-codex/SKILL.md` can be extracted into `~/.agents/skills/` once reviewed. Do not unzip over an existing skill without checking local modifications.

## Quick-start commands (run in a trusted local terminal)

Set `SKILL_DIR` to the installed **skill folder**, not the project repository:

```bash
SKILL_DIR="$HOME/.agents/skills/codebuddy-ioa-codex"
python3 "$SKILL_DIR/scripts/bridge_setup.py" doctor
python3 "$SKILL_DIR/scripts/bridge_setup.py" install
python3 "$SKILL_DIR/scripts/bridge_setup.py" init-key
python3 "$SKILL_DIR/scripts/bridge_setup.py" start --experimental-tools
```

`start` is **foreground**. If the official iOA key is not already in that terminal's environment, it prompts without echo. No official key is persisted or printed by the helper. The random bridge key is saved only locally at `~/.config/codebuddy-ioa-bridge/local-key` (or `$XDG_CONFIG_HOME/codebuddy-ioa-bridge/local-key`) with mode `0600`; it remains unchanged on re-run. The script requires its own folder to be on disk; no in-chat secrets are required.

The default bridge checkout is `~/.local/share/codebuddy-ioa-bridge/source`, separate from any old `~/codebuddy2api` clone. `install` refuses to touch a nonempty unrelated directory and refuses to update a dirty checkout. Use `install --update` only with user approval; this runs fast-forward-only Git operations. The source is public; the official API key is never stored in the Git repository.

In a separate terminal, after 8791 is running:

```bash
python3 "$SKILL_DIR/scripts/bridge_setup.py" probe
# A real charged call, after obtaining the owner's permission:
python3 "$SKILL_DIR/scripts/bridge_setup.py" probe --model APPROVED_MODEL_ID --invoke --accept-charges
python3 "$SKILL_DIR/scripts/bridge_setup.py" magpie
python3 "$SKILL_DIR/scripts/bridge_setup.py" copy-key
```

`copy-key` puts the local bridge key on the **local clipboard** for pasting in Magpie. Treat clipboard contents as sensitive. On systems without supported clipboard utilities, use `show-key --confirm` only in a trusted private terminal; avoid screenshots or logs. The bridge key is not the `CODEBUDDY_API_KEY`.

## Magpie GUI

1. Providers → Add provider → Custom. Name: `CodeBuddy iOA`.
2. In **OpenAI Responses** (not just “OpenAI-compatible Chat Completions”), set base URL `http://127.0.0.1:8791/v1`. **Clear the Chat Completions URL.** Never append `/responses` manually.
3. API Key = local bridge key (clipboard from `copy-key`). Model ID = one actually returned by `/v1/models` and authorized by iOA.
4. Agents → Codex → choose Magpie's real `provider/model` ID. New Codex App session required to read new routing; restart app if needed. `127.0.0.1:3425` is Magpie, not the bridge.
5. Magpie often has a CLI: `magpie models`, `magpie providers`, `magpie codex <provider>/<model>`. Only use its model-changing command with explicit permission (it updates Codex configuration). Do not pass keys as terminal command arguments.

Official Magpie guides: https://usemagpie.ai/docs/start and https://usemagpie.ai/docs/integrate .

## Experimental tool and cost notes

- Bridge `IOA_BRIDGE_EXPERIMENTAL_TOOLS=1` is **required** for current Codex Shell/Patch. This is prompt-simulated JSON tool selection plus a real Codex-hosted tool call, not SDK-native MCP. The SDK itself has `tools=[]`, denies execution, and uses `permission_mode=default`; it does not inherit Codex Full Access or execute commands.
- No silent context trimming, no unlimited allocations. Existing defaults after the 2026-10-31 patch: 128 MiB request body, 16M prompt chars, 8M tool schema chars, no fixed 64-tool count. Advanced variables `IOA_BRIDGE_MAX_BODY_MB`, `IOA_BRIDGE_MAX_PROMPT_CHARS`, `IOA_BRIDGE_MAX_TOOL_SPEC_CHARS` may be adjusted only when errors prove the need. **Do not make all limits unlimited.**
- The model's actual context size, SDK overhead and provider billing are independent. `usage.input_tokens_details.cached_tokens` and metadata cache counters reflect the backend's reported usage, not guaranteed RMB discounts. Do not auto-retry paid requests.
- Session history and Prompt Cache are different: old OpenAI cache cannot be migrated to iOA. The bridge currently does not support `previous_response_id` server-side state. Do not mix full-history Codex requests with persistent SDK sessions.

## Context window preference (1M default)

The bridge now defaults to `IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000` and passes `CODEBUDDY_AUTO_COMPACT_WINDOW=1000000` to the official SDK CLI. This is an **auto-compaction baseline**, not a guarantee that the iOA model is running the real 1M-tier context. For ACP and Codex App configuration differences, refer to [context window guide](context-window.md). Do not edit `~/.codex/config.toml` without the owner's explicit approval.

## Safety and rollback

The helper never edits the prior local repo, your `~/.codex/config.toml`, Magpie's providers JSON, shell rc files, enterprise accounts or SaaS Credits. No Docker, login-cookie transfer, token scraping, anonymous proxies, privilege bypass, or public network listening. Never run `rm -rf` on an unfamiliar old clone.

To stop bridge, press Ctrl+C in its terminal. To restore Codex's prior default, choose the original model in Magpie; if using Magpie CLI, `magpie codex default` is documented, but obtain approval first because it changes the agent's current choice. No secrets need to be pasted into chat to diagnose the bridge.
