#!/usr/bin/env python3
"""Claude Code status-line shim for AutoResume."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "autoresume" / "claude"


def config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autoresume"


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    instance = os.environ.get("AUTORESUME_INSTANCE_ID")
    if instance and isinstance(payload, dict):
        record = {
            "instance_id": instance,
            "session_id": payload.get("session_id"),
            "transcript_path": payload.get("transcript_path"),
            "rate_limits": payload.get("rate_limits", {}),
            "recorded_at": int(time.time()),
        }
        root = state_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{instance}.json"
        temp = root / f".{instance}.{os.getpid()}.tmp"
        temp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)

    metadata_path = config_root() / "claude-statusline.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    previous = metadata.get("previous_command") if isinstance(metadata, dict) else None
    if isinstance(previous, str) and previous.strip():
        try:
            completed = subprocess.run(previous, shell=True, input=raw, stdout=sys.stdout.buffer,
                                       stderr=sys.stderr.buffer, timeout=10)
            return completed.returncode
        except (OSError, subprocess.TimeoutExpired):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
