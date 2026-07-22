from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "src" / "statusline.py"


class StatusLineTests(unittest.TestCase):
    def run_shim(self, root: Path, payload: dict, previous: str | None = None) -> subprocess.CompletedProcess[bytes]:
        config = root / "config" / "autoresume"
        config.mkdir(parents=True)
        if previous is not None:
            (config / "claude-statusline.json").write_text(json.dumps({"previous_command": previous}))
        env = os.environ.copy()
        env.update({"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_STATE_HOME": str(root / "state"), "AUTORESUME_INSTANCE_ID": "claude-test"})
        return subprocess.run([sys.executable, str(SHIM)], input=json.dumps(payload).encode(),
                              capture_output=True, env=env, check=False)

    def test_records_documented_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"session_id": "abc", "transcript_path": "/tmp/t.jsonl",
                       "rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": 1234}}}
            result = self.run_shim(root, payload)
            self.assertEqual(result.returncode, 0)
            record = json.loads((root / "state" / "autoresume" / "claude" / "claude-test.json").read_text())
            self.assertEqual(record["session_id"], "abc")
            self.assertEqual(record["rate_limits"], payload["rate_limits"])

    def test_chains_previous_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_shim(Path(directory), {"session_id": "abc"}, "python3 -c 'print(\"old-line\")'")
            self.assertEqual(result.stdout.strip(), b"old-line")

    def test_malformed_input_is_harmless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update({"HOME": directory, "XDG_CONFIG_HOME": str(Path(directory) / "config"),
                        "XDG_STATE_HOME": str(Path(directory) / "state"), "AUTORESUME_INSTANCE_ID": "x"})
            result = subprocess.run([sys.executable, str(SHIM)], input=b"not json", env=env, capture_output=True)
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
