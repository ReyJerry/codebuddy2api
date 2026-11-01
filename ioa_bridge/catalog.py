"""Model discovery: from the installed SDK's headless --help, never hardcoded."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .core import MODEL_ID


# Multiple observed CLI help styles are supported. The environment override is
# only a local allowlist, never a claim about actual upstream entitlement.
_CHOICE_BLOCK = re.compile(r"--model(?:\s+|=)\{([^}]+)\}", re.I | re.S)
_CHOICE_BRACKET = re.compile(r"--model[^\n]*?\((?:choices?:\s*)?([^)]+)\)", re.I)
_HEADER = re.compile(r"^\s*(?:-m,\s*)?--model\b.*$", re.I)
_OPTION = re.compile(r"^\s*-{1,2}[a-zA-Z]", re.I)


def parse_help_models(help_text: str) -> list[str]:
    if not isinstance(help_text, str) or len(help_text) > 250_000:
        return []
    choices = _CHOICE_BLOCK.search(help_text) or _CHOICE_BRACKET.search(help_text)
    raw = choices.group(1) if choices else ""
    if not raw:
        lines = help_text.splitlines()
        for i, line in enumerate(lines):
            if _HEADER.search(line):
                # Only read the model option's continuation, never unrelated flags.
                candidate = [line]
                for next_line in lines[i + 1:i + 16]:
                    if _OPTION.match(next_line):
                        break
                    candidate.append(next_line)
                joined = " ".join(candidate)
                pattern = re.search(r"\bchoices?\s*:\s*([^\]\)]+)", joined, re.I)
                if pattern:
                    raw = pattern.group(1)
                elif "{" in joined and "}" in joined:
                    raw = joined.split("{", 1)[1].split("}", 1)[0]
                break
    models = []
    for token in re.split(r"[,|\s]+", raw):
        token = token.strip("'\"[]{}() ")
        if MODEL_ID.fullmatch(token) and token not in models:
            models.append(token)
    return models


def _sdk_binary_path() -> Path | None:
    explicit = os.getenv("CODEBUDDY_CODE_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    try:
        from codebuddy_agent_sdk._binary import try_cli_path
        found = try_cli_path()
        return Path(found) if found else None
    except (ImportError, OSError, AttributeError, TypeError):
        return None


class ModelCatalog:
    def __init__(self, *, ttl: int = 3600):
        self.ttl = ttl
        self._expires = 0.0
        self._cached: list[str] = []

    def models(self) -> list[str]:
        now = time.monotonic()
        if self._expires > now:
            return list(self._cached)
        configured = os.getenv("IOA_BRIDGE_MODELS", "")
        if configured.strip():
            names = [s.strip() for s in configured.split(",")]
            models = list(dict.fromkeys(s for s in names if MODEL_ID.fullmatch(s)))
        else:
            binary = _sdk_binary_path()
            models = []
            if binary:
                try:
                    proc = subprocess.run([str(binary), "--help"], capture_output=True,
                                          text=True, timeout=10, check=False)
                    models = parse_help_models((proc.stdout or "") + "\n" + (proc.stderr or ""))
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        self._expires = now + self.ttl
        self._cached = models
        return list(models)
