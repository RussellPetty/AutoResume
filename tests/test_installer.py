from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerTests(unittest.TestCase):
    def run_install(self, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("XDG_CONFIG_HOME", None)
        env.pop("XDG_STATE_HOME", None)
        env.update({"HOME": str(home), "SHELL": "/bin/zsh", "AUTORESUME_SOURCE_DIR": str(ROOT)})
        return subprocess.run(["bash", str(INSTALLER), "--yes", *args], env=env,
                              text=True, capture_output=True, check=False)

    def test_repeat_install_and_restore_existing_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            original = {"theme": "dark", "statusLine": {"type": "command", "command": "old-status"}}
            settings.write_text(json.dumps(original))
            self.assertEqual(self.run_install(home).returncode, 0)
            self.assertEqual(self.run_install(home).returncode, 0)
            startup = (home / ".zshrc").read_text()
            self.assertEqual(startup.count("# >>> AutoResume >>>"), 1)
            meta = json.loads((home / ".config" / "autoresume" / "claude-statusline.json").read_text())
            self.assertEqual(meta["previous_command"], "old-status")
            self.assertEqual(self.run_install(home, "--uninstall").returncode, 0)
            self.assertEqual(json.loads(settings.read_text()), original)

    def test_malformed_settings_are_backed_up_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            malformed = b"{ definitely not json\n"
            settings.write_bytes(malformed)
            self.assertEqual(self.run_install(home).returncode, 0)
            backups = list(settings.parent.glob("settings.json.autoresume-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), malformed)
            self.assertEqual(self.run_install(home, "--uninstall").returncode, 0)
            self.assertEqual(settings.read_bytes(), malformed)

    def test_manual_status_line_change_is_not_overwritten_on_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertEqual(self.run_install(home).returncode, 0)
            settings = home / ".claude" / "settings.json"
            value = json.loads(settings.read_text())
            value["statusLine"] = {"type": "command", "command": "manual-new"}
            value["theme"] = "light"
            settings.write_text(json.dumps(value))
            self.assertEqual(self.run_install(home, "--uninstall").returncode, 0)
            self.assertEqual(json.loads(settings.read_text())["statusLine"]["command"], "manual-new")

    def test_no_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertEqual(self.run_install(home, "--no-aliases").returncode, 0)
            self.assertFalse((home / ".zshrc").exists())


if __name__ == "__main__":
    unittest.main()
