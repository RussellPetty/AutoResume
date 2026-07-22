#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def app_server() -> None:
    counter_path = Path(os.environ.get("FAKE_COUNTER", "/tmp/autoresume-fake-counter"))
    try:
        count = int(counter_path.read_text())
    except (OSError, ValueError):
        count = 0
    counter_path.write_text(str(count + 1))
    reset = int(time.time() + (2.5 if count else float(os.environ.get("FAKE_RESET_DELAY", "0"))))
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("id") == 1:
            print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
        elif request.get("id") == 2:
            result = {"rateLimits": {"limitId": "codex", "primary": {
                "usedPercent": 100, "windowDurationMins": 300, "resetsAt": reset},
                "secondary": None, "credits": None, "individualLimit": None,
                "spendControlReached": False}, "rateLimitsByLimitId": {}}
            print(json.dumps({"id": 2, "result": result}), flush=True)
            return


def main() -> None:
    if sys.argv[1:2] == ["app-server"]:
        app_server()
        return
    behavior = os.environ.get("FAKE_BEHAVIOR", "limit")
    provider = os.environ.get("FAKE_PROVIDER", "codex")
    output = Path(os.environ["FAKE_OUTPUT"])
    if behavior == "nonlimit":
        print("Authentication failed (status 429)", flush=True)
    else:
        print("You've hit your usage limit. Try again at 11:59 PM" if provider == "codex"
              else "You've hit your limit · resets in a while", flush=True)
    if behavior == "exit":
        time.sleep(0.2)
        return
    if behavior == "draft":
        print("› my unfinished draft", flush=True)
        time.sleep(1.0)
        print("› ", flush=True)
    else:
        print("AUTORESUME_IDLE", flush=True)
    while True:
        line = sys.stdin.readline()
        if not line:
            time.sleep(0.05)
            continue
        with output.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if behavior == "rearm" and output.read_text(encoding="utf-8").count("\n") == 1:
            print("You've hit your usage limit. Try again at 11:59 PM", flush=True)
            print("AUTORESUME_IDLE", flush=True)
        else:
            print("accepted", flush=True)


if __name__ == "__main__":
    main()
