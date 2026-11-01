#!/usr/bin/env python3
"""Conservative local setup helper for an authorized CodeBuddy iOA → Magpie → Codex bridge.

Standard library only. Never collects OAuth cookies, SaaS Credits, or unauthorized tokens.
It does not edit Magpie or Codex application configuration. Official iOA API keys
are read from the process environment or a private, non-echoing human terminal.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request

REPO_URL = "https://github.com/ReyJerry/codebuddy2api.git"
BRANCH = "feat/ioa-sdk-responses-bridge"
DEFAULT_CHECKOUT = Path.home() / ".local/share/codebuddy-ioa-bridge/source"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
KEY_FILE = CONFIG_HOME / "codebuddy-ioa-bridge/local-key"
HEALTH_URL = "http://127.0.0.1:8791/health"
MODELS_URL = "http://127.0.0.1:8791/v1/models"
RESPONSES_URL = "http://127.0.0.1:8791/v1/responses"
MAGPIE_URL = "http://127.0.0.1:3425/api/hello"
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SetupError(Exception):
    """Safe, non-credential-bearing explanation suitable for display."""


def _request_json(url: str, *, key: str | None = None,
                  body: dict | None = None, timeout: int = 8) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(8192).decode("utf-8"))
            detail = payload.get("error", {}) if isinstance(payload, dict) else {}
            if isinstance(detail, dict):
                code = str(detail.get("code", "http_error"))[:80]
                message = str(detail.get("message", ""))[:180]
                raise SetupError(f"HTTP {exc.code}: {code} — {message}") from None
        except (ValueError, UnicodeDecodeError):
            pass
        raise SetupError(f"HTTP {exc.code}: local endpoint refused the request") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SetupError(f"Local endpoint unavailable: {url}") from None
    except ValueError:
        raise SetupError("Local endpoint returned non-JSON content") from None
    if not isinstance(result, dict):
        raise SetupError("Local endpoint returned a non-object JSON response")
    return result


def _read_local_key() -> str:
    key = os.environ.get("IOA_BRIDGE_KEY")
    if key:
        if len(key) < 16:
            raise SetupError("IOA_BRIDGE_KEY must contain at least 16 characters")
        return key
    if not KEY_FILE.exists() and not KEY_FILE.is_symlink():
        raise SetupError("No local bridge key. Run 'init-key' first (or set IOA_BRIDGE_KEY).")
    if KEY_FILE.is_symlink() or not KEY_FILE.is_file():
        raise SetupError("Refusing to read a symlink or non-regular local key file")
    info = KEY_FILE.stat()
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise SetupError("Local key file permissions are too broad; use chmod 600 on it")
    key = KEY_FILE.read_text(encoding="ascii").strip()
    if len(key) < 16:
        raise SetupError("Stored local key is too short; manual correction required")
    return key


def _write_new_key() -> None:
    if os.environ.get("IOA_BRIDGE_KEY"):
        print("IOA_BRIDGE_KEY already exists in this terminal; not replacing it.")
        print("For stable Magpie integration, use the same value in the bridge process and UI.")
        return
    if KEY_FILE.exists() or KEY_FILE.is_symlink():
        _read_local_key()
        print(f"Local bridge key already exists (not changed): {KEY_FILE}")
        return
    key_dir = KEY_FILE.parent
    key_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if key_dir.is_symlink():
        raise SetupError("Refusing a symlink for the local bridge key directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(KEY_FILE, flags, 0o600)
    except FileExistsError:
        raise SetupError("Local key was created concurrently; will not overwrite") from None
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(secrets.token_hex(24) + "\n")
    if os.name == "posix":
        os.chmod(KEY_FILE, 0o600)
    print(f"Created stable LOCAL key: {KEY_FILE} (0600). Key value is not printed.")
    print("This is NOT your official CODEBUDDY_API_KEY.")


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=100,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError(f"Cannot complete command: {argv[0]}") from None
    if proc.returncode:
        # Avoid copying arbitrary git stderr / credentialed URL into chat/logs.
        raise SetupError(f"Command failed (exit {proc.returncode}): {argv[0]} {argv[1] if len(argv)>1 else ''}")
    return proc.stdout.strip()


def _assert_checkout(repo_dir: Path) -> None:
    if not (repo_dir / ".git").exists():
        raise SetupError(f"Not a Git checkout: {repo_dir}")
    origin = _run(["git", "remote", "get-url", "origin"], cwd=repo_dir)
    if origin not in (REPO_URL, "https://github.com/ReyJerry/codebuddy2api",
                      "git@github.com:ReyJerry/codebuddy2api.git"):
        raise SetupError("Existing checkout has a different origin; refusing to update or run it")
    if not (repo_dir / "ioa_bridge/__main__.py").is_file():
        raise SetupError("Checked-out source lacks ioa_bridge; use the feature branch")


def _install(repo_dir: Path, update: bool) -> None:
    if not shutil.which("git"):
        raise SetupError("Git is required and not installed")
    if repo_dir.exists():
        _assert_checkout(repo_dir)
        if not update:
            print(f"Existing bridge checkout left unchanged: {repo_dir}")
            print("To fast-forward safely, run 'install --update' after reviewing local changes.")
            return
        if _run(["git", "status", "--porcelain"], cwd=repo_dir):
            raise SetupError("Checkout has local changes; refusing to update (no resets)")
        branch = _run(["git", "branch", "--show-current"], cwd=repo_dir)
        if branch != BRANCH:
            raise SetupError(f"Checkout is on {branch!r}, not expected feature branch; refusing to switch")
        _run(["git", "fetch", "origin", BRANCH], cwd=repo_dir)
        _run(["git", "pull", "--ff-only", "origin", BRANCH], cwd=repo_dir)
    else:
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--single-branch", "--branch", BRANCH,
              REPO_URL, str(repo_dir)])
    _assert_checkout(repo_dir)
    revision = _run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo_dir)
    print(f"Verified source: {repo_dir}\nBranch: {BRANCH}\nCommit: {revision}")
    print("Start with: python3 <skill>/scripts/bridge_setup.py start --experimental-tools")


def _doctor(repo_dir: Path) -> None:
    print("CodeBuddy iOA / Magpie / Codex read-only diagnostic")
    print(f"Platform: {platform.system()} {platform.release()}")
    for binary in ("python3", "git", "uv", "magpie", "codex"):
        value = shutil.which(binary)
        print(f"{binary}: {value or 'not on PATH'}")
    if platform.system() == "Darwin":
        print(f"Codex.app: {'found' if Path('/Applications/Codex.app').exists() else 'not detected in /Applications (may be elsewhere)'}")
    print(f"Bridge checkout: {repo_dir} — {'present' if (repo_dir / 'ioa_bridge/__main__.py').is_file() else 'not detected'}")
    print(f"Official iOA API key in environment: {'set' if os.environ.get('CODEBUDDY_API_KEY') else 'not set (can prompt when starting)'}")
    print(f"Conflicting CODEBUDDY_AUTH_TOKEN: {'SET: must unset' if os.environ.get('CODEBUDDY_AUTH_TOKEN') else 'not set'}")
    try:
        _read_local_key()
        print("Local bridge key: available (value hidden)")
    except SetupError as exc:
        print(f"Local bridge key: {exc}")
    for title, url in (("Bridge 8791", HEALTH_URL), ("Magpie 3425", MAGPIE_URL)):
        try:
            data = _request_json(url, timeout=2)
            print(f"{title}: reachable ({data.get('status') or data.get('name') or 'JSON'})")
        except SetupError:
            print(f"{title}: not reachable (service might not have been started)")
    print("No changes were made. See references/setup.md for next steps.")


def _start(repo_dir: Path, experimental: bool) -> None:
    if not shutil.which("uv"):
        raise SetupError("uv is missing; install it from https://docs.astral.sh/uv/ (with approval)")
    _assert_checkout(repo_dir)
    try:
        _request_json(HEALTH_URL, timeout=1)
    except SetupError:
        pass
    else:
        raise SetupError("A bridge already responds on 8791; stop it before launching another")
    key = _read_local_key()
    if os.environ.get("CODEBUDDY_AUTH_TOKEN"):
        raise SetupError("CODEBUDDY_AUTH_TOKEN is set and conflicts with official API key; unset it")
    official = os.environ.get("CODEBUDDY_API_KEY", "")
    if not official:
        if not sys.stdin.isatty():
            raise SetupError("Official iOA API key is absent. Set it locally or launch 'start' in a human terminal; never paste it in chat")
        official = getpass.getpass("Enter your authorized official CodeBuddy iOA API Key (hidden): ")
    if not official.strip():
        raise SetupError("Official API Key cannot be empty")
    env = dict(os.environ)
    env["CODEBUDDY_API_KEY"] = official
    env["CODEBUDDY_INTERNET_ENVIRONMENT"] = "ioa"
    env["IOA_BRIDGE_KEY"] = key
    if experimental:
        env["IOA_BRIDGE_EXPERIMENTAL_TOOLS"] = "1"
    enabled = env.get("IOA_BRIDGE_EXPERIMENTAL_TOOLS") == "1"
    print(f"Starting bridge on 127.0.0.1:8791; experimental Codex tool calls: {'ON' if enabled else 'OFF'}", flush=True)
    if enabled:
        print("Tools are selected via experimental JSON prompting; Codex App still owns tool approval/execution.", flush=True)
    print("This process stays in the foreground. Ctrl+C stops it. No credentials are printed.", flush=True)
    os.chdir(repo_dir)
    executable = shutil.which("uv")
    assert executable
    os.execvpe(executable,
               [executable, "run", "--locked", "--with", "codebuddy-agent-sdk",
                "python", "-m", "ioa_bridge"], env)


def _probe(model: str | None, invoke: bool, accepted: bool) -> None:
    if invoke and not (model and accepted):
        raise SetupError("Real paid invocation requires both --model MODEL and --accept-charges")
    print(f"Bridge health: {_request_json(HEALTH_URL).get('status', 'unknown')}")
    key = _read_local_key()
    models = _request_json(MODELS_URL, key=key).get("data")
    if not isinstance(models, list):
        raise SetupError("Bridge model catalog has an unexpected format")
    found = [str(x.get("id")) for x in models if isinstance(x, dict) and isinstance(x.get("id"), str)]
    print("Locally discovered model IDs (not authorization proof):")
    for item in found:
        print(" -", item)
    if model and model not in found:
        raise SetupError("Requested model is not in this bridge's discovered model list")
    if not invoke:
        print("No paid inference request was sent. Use --invoke --accept-charges only with owner's permission.")
        return
    assert model
    if not MODEL_RE.fullmatch(model):
        raise SetupError("Invalid model ID")
    result = _request_json(RESPONSES_URL, key=key,
                           body={"model": model, "input": "Reply exactly: OK", "stream": False},
                           timeout=180)
    print("Paid response status:", result.get("status", "unknown"))
    usage = result.get("usage")
    if isinstance(usage, dict):
        print("Usage (vendor-reported):", json.dumps(usage, ensure_ascii=False))
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        print("Cache tokens (not billing amounts):", json.dumps(metadata, ensure_ascii=False))
    outputs = result.get("output")
    if isinstance(outputs, list):
        pieces = [str(p.get("text", "")) for item in outputs if isinstance(item, dict)
                  for p in item.get("content", []) if isinstance(p, dict) and p.get("type") == "output_text"]
        if pieces:
            print("Short synthetic reply:", " ".join(pieces)[:200])
    if result.get("status") != "completed":
        raise SetupError("Bridge did not report a completed response")


def _magpie() -> None:
    print("Magpie provider settings (GUI; no files are changed):")
    print("Provider Name: CodeBuddy iOA")
    print("OpenAI Responses Base URL: http://127.0.0.1:8791/v1")
    print("OpenAI Chat Completions URL: LEAVE EMPTY")
    print("API Key: your LOCAL bridge key (use 'copy-key'; not the official iOA Key)")
    print("Model ID: one from 'probe' that your account is authorized to use")
    print("Then Agents → Codex → choose Magpie's provider/model and start a NEW Codex App session.")
    print("Do not overwrite ~/.codex/config.toml or ~/.config/magpie/providers.json manually.")


def _copy_key() -> None:
    key = _read_local_key()
    system = platform.system()
    if system == "Darwin":
        argv = ["pbcopy"]
    elif shutil.which("wl-copy"):
        argv = ["wl-copy"]
    elif shutil.which("xclip"):
        argv = ["xclip", "-selection", "clipboard"]
    else:
        raise SetupError("No local clipboard utility detected; in a trusted terminal use show-key --confirm")
    try:
        subprocess.run(argv, input=key, text=True, timeout=5, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Could not copy the local key to clipboard") from None
    print("LOCAL bridge key copied to clipboard. Paste into Magpie; clear clipboard afterward.")


def _show_key(confirm: bool) -> None:
    if not confirm or not sys.stdout.isatty() or not sys.stdin.isatty():
        raise SetupError("Refusing to print a key in logs/agent tools. Use copy-key instead, or --confirm in a trusted private terminal")
    print("LOCAL bridge key (not official iOA key):", _read_local_key())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=str(DEFAULT_CHECKOUT),
                        help="Isolated bridge checkout; default ~/.local/share/codebuddy-ioa-bridge/source")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Read-only prerequisites and localhost checks")
    installer = commands.add_parser("install", help="Clone reviewed bridge feature branch in isolated directory")
    installer.add_argument("--update", action="store_true", help="Fast-forward clean matching checkout (never reset)")
    commands.add_parser("init-key", help="Generate stable local-only key in a private file; never overwrite")
    starter = commands.add_parser("start", help="Foreground bridge; prompts for official iOA key in human terminal")
    starter.add_argument("--experimental-tools", action="store_true", help="Enable prompt-based Codex tools (owner approval required)")
    prob = commands.add_parser("probe", help="Free health/model checks; optional paid minimal inference")
    prob.add_argument("--model", help="Actually returned and authorized model ID")
    prob.add_argument("--invoke", action="store_true", help="Send a real paid inference request")
    prob.add_argument("--accept-charges", action="store_true", help="Explicitly acknowledge potential AI TOKEN charges")
    commands.add_parser("magpie", help="Print Magpie-only Responses provider fields")
    commands.add_parser("copy-key", help="Put LOCAL key on trusted clipboard without logging it")
    show = commands.add_parser("show-key", help="Print LOCAL key only in a human TTY (avoid logs)")
    show.add_argument("--confirm", action="store_true")
    opts = parser.parse_args(argv)
    repo_dir = Path(opts.repo_dir).expanduser().resolve()
    try:
        if opts.command == "doctor":
            _doctor(repo_dir)
        elif opts.command == "install":
            _install(repo_dir, opts.update)
        elif opts.command == "init-key":
            _write_new_key()
        elif opts.command == "start":
            _start(repo_dir, opts.experimental_tools)
        elif opts.command == "probe":
            _probe(opts.model, opts.invoke, opts.accept_charges)
        elif opts.command == "magpie":
            _magpie()
        elif opts.command == "copy-key":
            _copy_key()
        elif opts.command == "show-key":
            _show_key(opts.confirm)
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
