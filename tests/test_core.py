from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("autoresume", ROOT / "src" / "autoresume.py")
assert SPEC and SPEC.loader
ar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ar)


class RecognitionTests(unittest.TestCase):
    def test_codex_hard_limit(self) -> None:
        self.assertTrue(ar.is_hard_limit("codex", "You've hit your usage limit. Try again at 4:00 PM"))

    def test_claude_hard_limit(self) -> None:
        self.assertTrue(ar.is_hard_limit("claude", "You've hit your limit · resets 4pm"))

    def test_non_limit_errors_never_trigger(self) -> None:
        messages = [
            "workspace spend limit reached", "insufficient credits",
            "Authentication failed", "context window limit", "Error 429", "529 overloaded",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(ar.is_hard_limit("codex", message))

    def test_old_transient_error_does_not_hide_new_limit(self) -> None:
        text = "Error 429\n" + ("old output\n" * 100) + "You've hit your usage limit"
        self.assertTrue(ar.is_hard_limit("codex", text))


class ResetTests(unittest.TestCase):
    def test_codex_fixture(self) -> None:
        payload = json.loads((ROOT / "tests" / "fixtures" / "codex_rate_limits.json").read_text())
        self.assertEqual(ar.select_codex_reset(payload, now=1000), 2000)

    def test_codex_rejects_spend_control(self) -> None:
        payload = {"result": {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": 2000},
                    "spendControlReached": True}}}
        self.assertIsNone(ar.select_codex_reset(payload, now=1000))

    def test_codex_allows_nonexhausted_individual_control(self) -> None:
        payload = {"result": {"rateLimits": {
            "primary": {"usedPercent": 100, "resetsAt": 2000},
            "individualLimit": {"remainingPercent": 75, "resetsAt": 9000},
            "spendControlReached": False}}}
        self.assertEqual(ar.select_codex_reset(payload, now=1000), 2000)

    def test_codex_changed_protocol_fails_closed(self) -> None:
        self.assertIsNone(ar.select_codex_reset({"result": {"renamedRateLimits": {}}}, now=1000))

    def test_claude_selects_exhausted_window(self) -> None:
        record = {"rate_limits": {
            "five_hour": {"used_percentage": 100, "resets_at": 2000},
            "seven_day": {"used_percentage": 80, "resets_at": 8000}}}
        self.assertEqual(ar.select_claude_reset(record, now=1000), 2000)

    def test_claude_selects_named_weekly_window(self) -> None:
        record = {"rate_limits": {
            "five_hour": {"used_percentage": 80, "resets_at": 2000},
            "seven_day": {"used_percentage": 80, "resets_at": 8000}}}
        self.assertEqual(ar.select_claude_reset(record, "weekly limit", now=1000), 8000)

    def test_local_time_rolls_to_tomorrow(self) -> None:
        zone = dt.timezone(dt.timedelta(hours=-4))
        now = dt.datetime(2026, 7, 22, 18, 0, tzinfo=zone)
        actual = ar.parse_displayed_reset("Try again at 5:30 PM", now)
        expected = dt.datetime(2026, 7, 23, 17, 30, tzinfo=zone).timestamp()
        self.assertEqual(actual, expected)

    def test_named_date(self) -> None:
        zone = dt.timezone.utc
        now = dt.datetime(2026, 7, 22, tzinfo=zone)
        actual = ar.parse_displayed_reset("resets at Jul 25, 2026 3:15 AM", now)
        self.assertEqual(actual, dt.datetime(2026, 7, 25, 3, 15, tzinfo=zone).timestamp())

    def test_overdue_timer_fires_after_wake(self) -> None:
        self.assertTrue(ar.timer_due(1000, 30, now=5000))
        self.assertFalse(ar.timer_due(1000, 30, now=1029))


class ConfigTests(unittest.TestCase):
    def test_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": str(Path(directory) / "state")}
            path = Path(directory) / "autoresume" / "config.json"
            path.parent.mkdir()
            path.write_text('{"poll_interval": 4, "grace_period": 9, "prompt": "go"}')
            with mock.patch.dict(os.environ, env):
                cfg = ar.load_config()
            self.assertEqual(cfg["poll_seconds"], 4)
            self.assertEqual(cfg["grace_seconds"], 9)
            self.assertEqual(cfg["continuation_prompt"], "go")

    def test_invalid_numbers_return_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": str(Path(directory) / "state")}
            path = Path(directory) / "autoresume" / "config.json"
            path.parent.mkdir()
            path.write_text('{"poll_seconds": "bad", "grace_seconds": null, "enabled": "yes"}')
            with mock.patch.dict(os.environ, env):
                cfg = ar.load_config()
            self.assertEqual(cfg["poll_seconds"], 2)
            self.assertEqual(cfg["grace_seconds"], 30)
            self.assertIs(cfg["enabled"], True)

    def test_composer_detection(self) -> None:
        self.assertTrue(ar.composer_idle("output\n› "))
        self.assertFalse(ar.composer_idle("output\n› unfinished"))

    def test_goal_prompt_detection(self) -> None:
        self.assertTrue(ar.pane_has_goal("Goal paused by usage limit; use /goal resume"))


if __name__ == "__main__":
    unittest.main()
