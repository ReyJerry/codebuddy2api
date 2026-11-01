"""Completely offline tests for the distributable Codex skill bootstrap helper."""
from __future__ import annotations

import contextlib
from io import BytesIO, StringIO
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/codebuddy-ioa-codex"
SCRIPT = SKILL / "scripts/bridge_setup.py"
spec = importlib.util.spec_from_file_location("bridge_setup", SCRIPT)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = setup
spec.loader.exec_module(setup)


class TestSkillPackage(unittest.TestCase):
    def test_skill_manifest_and_all_reference_paths(self):
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\nname: codebuddy-ioa-codex\n"))
        self.assertIn("description:", content.split("---", 2)[1])
        self.assertIn("Magpie", content)
        for ref in ("setup.md", "troubleshooting.md", "e2e-tests.md", "快速开始.md"):
            self.assertTrue((SKILL / "references" / ref).is_file())
        self.assertIn("display_name:", (SKILL / "agents/openai.yaml").read_text())
        self.assertTrue(SCRIPT.is_file())

    def test_cli_help_and_readonly_magpie(self):
        stdout = StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(setup.main(["magpie"]), 0)
        text = stdout.getvalue()
        self.assertIn("OpenAI Responses", text)
        self.assertIn("LEAVE EMPTY", text)
        self.assertIn("127.0.0.1:8791/v1", text)
        self.assertNotIn("CODEBUDDY_API_KEY=", text)

    def test_first_local_key_is_private_and_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "private" / "local-key"
            with mock.patch.object(setup, "KEY_FILE", key_file), mock.patch.dict(os.environ, {}, clear=True):
                stdout = StringIO()
                with contextlib.redirect_stdout(stdout):
                    setup._write_new_key()
                original = key_file.read_text(encoding="ascii").strip()
                self.assertEqual(len(original), 48)
                self.assertNotIn(original, stdout.getvalue())
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
                self.assertEqual(setup._read_local_key(), original)
                setup._write_new_key()
                self.assertEqual(original, key_file.read_text(encoding="ascii").strip())

    def test_existing_environment_key_is_not_replaced_by_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "local-key"
            with mock.patch.object(setup, "KEY_FILE", key_file), mock.patch.dict(
                os.environ, {"IOA_BRIDGE_KEY": "safe-local-key-012345"}, clear=True
            ):
                setup._write_new_key()
                self.assertFalse(key_file.exists())
                self.assertEqual(setup._read_local_key(), "safe-local-key-012345")

    def test_file_permissions_and_symlink_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "local-key"
            key_file.write_text("a" * 40, encoding="ascii")
            if os.name == "posix":
                key_file.chmod(0o644)
                with mock.patch.object(setup, "KEY_FILE", key_file), mock.patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(setup.SetupError, "too broad"):
                        setup._read_local_key()
            key_file.chmod(0o600)
            link = Path(tmp) / "link"
            link.symlink_to(key_file)
            with mock.patch.object(setup, "KEY_FILE", link), mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(setup.SetupError, "symlink"):
                    setup._read_local_key()

    def test_paid_requests_require_explicit_ack_before_network(self):
        with mock.patch.object(setup, "_request_json") as req:
            with self.assertRaisesRegex(setup.SetupError, "--accept-charges"):
                setup._probe("demo-model", invoke=True, accepted=False)
            with self.assertRaisesRegex(setup.SetupError, "--model"):
                setup._probe(None, invoke=True, accepted=True)
            req.assert_not_called()

    def test_probe_without_invoke_is_never_charged(self):
        with mock.patch.object(setup, "_read_local_key", return_value="local-secret"), mock.patch.object(
            setup, "_request_json", side_effect=[{"status": "ok"}, {"data": [{"id": "glm-5.2-ioa"}]}]
        ) as req:
            with contextlib.redirect_stdout(StringIO()):
                setup._probe(None, invoke=False, accepted=False)
            self.assertEqual(req.call_count, 2)
            self.assertTrue(all(x.args[0] != setup.RESPONSES_URL for x in req.call_args_list))

    def test_paid_probe_only_after_ack_and_uses_synthetic_prompt(self):
        results = [
            {"status": "ok"},
            {"data": [{"id": "glm-5.2-ioa"}]},
            {"status": "completed", "usage": {"input_tokens": 24, "output_tokens": 1},
             "output": [{"content": [{"type": "output_text", "text": "OK"}]}]},
        ]
        with mock.patch.object(setup, "_read_local_key", return_value="local-secret"), mock.patch.object(
            setup, "_request_json", side_effect=results
        ) as req:
            with contextlib.redirect_stdout(StringIO()):
                setup._probe("glm-5.2-ioa", invoke=True, accepted=True)
            self.assertEqual(req.call_count, 3)
            self.assertEqual(req.call_args_list[2].args[0], setup.RESPONSES_URL)
            self.assertEqual(req.call_args_list[2].kwargs["body"]["input"], "Reply exactly: OK")

    def test_http_401_redacted_error_from_bridge(self):
        payload = b'{"error":{"code":"invalid_api_key","message":"bad local key"}}'
        err = urllib.error.HTTPError(setup.MODELS_URL, 401, "Unauthorized", {}, BytesIO(payload))
        with mock.patch.object(setup.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(setup.SetupError, "invalid_api_key"):
                setup._request_json(setup.MODELS_URL, key="do-not-print")

    def test_install_will_not_touch_existing_unrelated_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonrepo"
            path.mkdir()
            (path / "private.txt").write_text("precious")
            with self.assertRaisesRegex(setup.SetupError, "Not a Git checkout"):
                setup._install(path, update=False)
            self.assertEqual((path / "private.txt").read_text(), "precious")

    def test_install_wrong_git_origin_does_not_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            (path / ".git").mkdir(parents=True)
            (path / "ioa_bridge").mkdir()
            (path / "ioa_bridge/__main__.py").write_text("# mock")
            with mock.patch.object(setup, "_run", return_value="https://github.com/other/project.git") as run:
                with self.assertRaisesRegex(setup.SetupError, "different origin"):
                    setup._install(path, update=True)
                self.assertEqual(run.call_count, 1)

    def test_install_dirty_checkout_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            (path / ".git").mkdir(parents=True)
            (path / "ioa_bridge").mkdir()
            (path / "ioa_bridge/__main__.py").write_text("# mock")
            with mock.patch.object(setup, "_run", side_effect=[setup.REPO_URL, " M local_file"] ) as run:
                with self.assertRaisesRegex(setup.SetupError, "local changes"):
                    setup._install(path, update=True)
                self.assertEqual(run.call_count, 2)

    def test_start_noninteractive_without_key_does_not_execute_uv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with mock.patch.object(setup, "_assert_checkout"), mock.patch.object(
                setup.shutil, "which", return_value="/usr/bin/uv"
            ), mock.patch.object(setup, "_request_json", side_effect=setup.SetupError("offline")), mock.patch.object(
                setup, "_read_local_key", return_value="local-bridge-key"
            ), mock.patch.object(setup.os, "execvpe") as execvpe, mock.patch.dict(
                os.environ, {"CODEBUDDY_API_KEY": ""}, clear=True
            ):
                with self.assertRaisesRegex(setup.SetupError, "human terminal"):
                    setup._start(path, experimental=True)
                execvpe.assert_not_called()

    def test_start_preserves_default_sdk_permissions_and_only_sets_safe_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with mock.patch.object(setup, "_assert_checkout"), mock.patch.object(
                setup.shutil, "which", return_value="/usr/bin/uv"
            ), mock.patch.object(setup, "_request_json", side_effect=setup.SetupError("offline")), mock.patch.object(
                setup, "_read_local_key", return_value="local-bridge-key-1234"
            ), mock.patch.object(setup.os, "execvpe") as execvpe, mock.patch.dict(
                os.environ, {"CODEBUDDY_API_KEY": "official-secret", "CODEBUDDY_INTERNET_ENVIRONMENT": "wrong"}, clear=True
            ):
                with mock.patch.object(setup.os, "chdir"):
                    with contextlib.redirect_stdout(StringIO()):
                        setup._start(path, experimental=True)
                self.assertEqual(execvpe.call_count, 1)
                args = execvpe.call_args.args
                self.assertIn("codebuddy-agent-sdk", args[1])
                self.assertEqual(args[2]["CODEBUDDY_INTERNET_ENVIRONMENT"], "ioa")
                self.assertEqual(args[2]["IOA_BRIDGE_EXPERIMENTAL_TOOLS"], "1")
                self.assertEqual(args[2]["CODEBUDDY_API_KEY"], "official-secret")

    def test_show_key_is_blocked_for_agent_tool_logs(self):
        with mock.patch.object(setup, "_read_local_key", return_value="never-log-me"), mock.patch.object(
            setup.sys.stdout, "isatty", return_value=False
        ):
            with self.assertRaisesRegex(setup.SetupError, "Refusing"):
                setup._show_key(confirm=True)

    def test_validated_local_key_is_only_passed_to_clipboard(self):
        with mock.patch.object(setup, "_read_local_key", return_value="local-value-1234567890"), mock.patch.object(
            setup.platform, "system", return_value="Darwin"
        ), mock.patch.object(setup.subprocess, "run") as run:
            stdout = StringIO()
            with contextlib.redirect_stdout(stdout):
                setup._copy_key()
            self.assertEqual(run.call_args.kwargs["input"], "local-value-1234567890")
            self.assertNotIn("local-value", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
