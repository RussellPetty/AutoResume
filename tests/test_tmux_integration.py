from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
AUTORE = ROOT / "src" / "autoresume.py"
FAKE = ROOT / "tests" / "fake_cli.py"


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TmuxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions: list[str] = []
        self.watchers: list[subprocess.Popen[bytes]] = []
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "codex").symlink_to(FAKE)
        FAKE.chmod(0o755)
        config = self.root / "config" / "autoresume"
        config.mkdir(parents=True)
        (config / "config.json").write_text(json.dumps({
            "schema_version": 1, "enabled": True, "poll_seconds": 0.1,
            "grace_seconds": 0, "continuation_prompt": "continue"}))

    def tearDown(self) -> None:
        for session in self.sessions:
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        for watcher in self.watchers:
            try:
                watcher.wait(timeout=2)
            except subprocess.TimeoutExpired:
                watcher.terminate()
                watcher.wait(timeout=2)
        self.temp.cleanup()

    def env(self, output: Path, behavior: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "FAKE_OUTPUT": str(output), "FAKE_BEHAVIOR": behavior,
            "FAKE_PROVIDER": "codex", "FAKE_COUNTER": str(self.root / f"counter-{output.name}"),
        })
        return env

    def launch(self, behavior: str, label: str) -> tuple[Path, subprocess.Popen[bytes]]:
        output = self.root / f"{label}.out"
        session = f"ar-test-{uuid.uuid4().hex[:10]}"
        self.sessions.append(session)
        env = self.env(output, behavior)
        fake_env = ["env", f"FAKE_OUTPUT={output}", f"FAKE_BEHAVIOR={behavior}",
                    "FAKE_PROVIDER=codex", f"FAKE_COUNTER={env['FAKE_COUNTER']}"]
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "--", *fake_env,
                        sys.executable, str(FAKE)], env=env, capture_output=True, check=True)
        pane_info = subprocess.check_output(["tmux", "list-panes", "-t", session, "-F", "#{pane_id} #{pane_pid}"], text=True)
        pane, pid = pane_info.strip().split()
        instance = f"test-{label}"
        watcher = subprocess.Popen([sys.executable, str(AUTORE), "_watch", "codex", instance, pane, "--pid", pid],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.watchers.append(watcher)
        return output, watcher

    @staticmethod
    def wait_lines(path: Path, count: int, timeout: float = 5) -> list[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                lines = path.read_text().splitlines()
                if len(lines) >= count:
                    return lines
            time.sleep(0.05)
        return path.read_text().splitlines() if path.exists() else []

    def test_exactly_one_continuation(self) -> None:
        output, _ = self.launch("limit", "once")
        self.assertEqual(self.wait_lines(output, 1), ["continue"])
        time.sleep(0.7)
        self.assertEqual(output.read_text().splitlines(), ["continue"])

    def test_multiple_waiting_chats_resume_independently(self) -> None:
        one, _ = self.launch("limit", "one")
        two, _ = self.launch("limit", "two")
        self.assertEqual(self.wait_lines(one, 1), ["continue"])
        self.assertEqual(self.wait_lines(two, 1), ["continue"])

    def test_non_limit_error_never_triggers(self) -> None:
        output, _ = self.launch("nonlimit", "nonlimit")
        time.sleep(0.8)
        self.assertFalse(output.exists())

    def test_draft_delays_injection(self) -> None:
        output, _ = self.launch("draft", "draft")
        time.sleep(0.6)
        self.assertFalse(output.exists())
        self.assertEqual(self.wait_lines(output, 1), ["continue"])

    def test_process_exit_cancels_timer(self) -> None:
        output, watcher = self.launch("exit", "exit")
        watcher.wait(timeout=3)
        self.assertFalse(output.exists())
        state = json.loads((self.root / "state" / "autoresume" / "instances" / "test-exit.json").read_text())
        self.assertEqual(state["status"], "exited")

    def test_second_limit_rearms(self) -> None:
        output, _ = self.launch("rearm", "rearm")
        self.assertEqual(self.wait_lines(output, 2, timeout=7), ["continue", "continue"])


if __name__ == "__main__":
    unittest.main()
