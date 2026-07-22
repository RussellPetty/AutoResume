#!/usr/bin/env python3
"""AutoResume: resume usage-limited Codex and Claude Code terminal chats."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.request import urlopen

VERSION = "0.1.0"
DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "enabled": True,
    "poll_seconds": 2,
    "grace_seconds": 30,
    "continuation_prompt": "continue",
}
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CODEX_HARD_LIMITS = (
    re.compile(r"you(?:'|’)ve hit your (?:current )?usage limit", re.I),
    re.compile(r"usage limit (?:has been )?reached", re.I),
    re.compile(r"you have reached your .*usage limit", re.I),
)
CLAUDE_HARD_LIMITS = (
    re.compile(r"you(?:'|’)ve hit your limit", re.I),
    re.compile(r"usage limit reached", re.I),
    re.compile(r"you have reached your .*usage limit", re.I),
)
NEVER_RETRY = re.compile(
    r"(?:workspace (?:spend|budget)|spend cap|depleted credits?|insufficient credits?|"
    r"authentication|not logged in|context (?:window|limit)|too much context|"
    r"(?:error|status)\s*(?:429|529)|overloaded|capacity)",
    re.I,
)


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autoresume"


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "autoresume"


def share_home() -> Path:
    return Path(os.environ.get("AUTORESUME_SHARE", Path.home() / ".local" / "share" / "autoresume"))


def ensure_dirs() -> None:
    for path in (config_home(), state_home(), state_home() / "instances", state_home() / "claude"):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def load_config() -> dict[str, Any]:
    ensure_dirs()
    path = config_home() / "config.json"
    raw = load_json(path, {})
    if not isinstance(raw, dict):
        raw = {}
    # Migrate the short-lived pre-1 schema names if encountered.
    aliases = {"poll_interval": "poll_seconds", "grace_period": "grace_seconds", "prompt": "continuation_prompt"}
    for old, new in aliases.items():
        if old in raw and new not in raw:
            raw[new] = raw.pop(old)
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    cfg["schema_version"] = 1
    try:
        cfg["poll_seconds"] = max(0.1, float(cfg["poll_seconds"]))
    except (TypeError, ValueError):
        cfg["poll_seconds"] = float(DEFAULTS["poll_seconds"])
    try:
        cfg["grace_seconds"] = max(0.0, float(cfg["grace_seconds"]))
    except (TypeError, ValueError):
        cfg["grace_seconds"] = float(DEFAULTS["grace_seconds"])
    if not isinstance(cfg["enabled"], bool):
        cfg["enabled"] = bool(DEFAULTS["enabled"])
    cfg["continuation_prompt"] = str(cfg["continuation_prompt"])
    if raw != cfg:
        atomic_json(path, cfg)
    return cfg


def log(instance: str, message: str) -> None:
    ensure_dirs()
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with (state_home() / f"{instance}.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def state_path(instance: str) -> Path:
    return state_home() / "instances" / f"{instance}.json"


def write_state(instance: str, **updates: Any) -> dict[str, Any]:
    state = load_json(state_path(instance), {})
    if not isinstance(state, dict):
        state = {}
    state.update(updates)
    state["instance"] = instance
    state["updated_at"] = int(time.time())
    atomic_json(state_path(instance), state)
    return state


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def hard_limit_count(provider: str, text: str) -> int:
    clean = strip_ansi(text)
    patterns = CODEX_HARD_LIMITS if provider == "codex" else CLAUDE_HARD_LIMITS
    count = 0
    for pattern in patterns:
        for match in pattern.finditer(clean):
            nearby = clean[max(0, match.start() - 240):match.end() + 240]
            if not NEVER_RETRY.search(nearby):
                count += 1
    return count


def is_hard_limit(provider: str, text: str) -> bool:
    return hard_limit_count(provider, text) > 0


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _windows(snapshot: dict[str, Any]) -> list[tuple[float, float]]:
    found: list[tuple[float, float]] = []
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = _number(window.get("usedPercent")), _number(window.get("resetsAt"))
        if used is not None and reset is not None:
            found.append((used, reset))
    return found


def select_codex_reset(payload: dict[str, Any], now: float | None = None) -> float | None:
    """Select an authoritative reset without treating budget/spend caps as usage limits."""
    now = time.time() if now is None else now
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return None
    snapshots: list[dict[str, Any]] = []
    top = result.get("rateLimits")
    if isinstance(top, dict):
        snapshots.append(top)
    buckets = result.get("rateLimitsByLimitId")
    if isinstance(buckets, dict):
        snapshots.extend(v for v in buckets.values() if isinstance(v, dict) and v not in snapshots)

    exhausted: list[float] = []
    for snap in snapshots:
        individual = snap.get("individualLimit")
        individual_exhausted = isinstance(individual, dict) and _number(individual.get("remainingPercent")) == 0
        reached_type = snap.get("rateLimitReachedType")
        if snap.get("spendControlReached") is True or individual_exhausted or reached_type in (
            "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached"
        ):
            continue
        windows = [(used, reset) for used, reset in _windows(snap) if reset >= now - 60]
        hit = [reset for used, reset in windows if used >= 99.0]
        if hit:
            # If multiple windows bind this bucket, all must reset.
            exhausted.append(max(hit))
    if exhausted:
        # Prefer the soonest exhausted product bucket; unrelated model buckets may coexist.
        return min(exhausted)

    # Some server versions round/reset usage before the CLI redraws. In that case the
    # top-level snapshot is the best correlated view, but never infer from credits.
    top_individual = top.get("individualLimit") if isinstance(top, dict) else None
    top_spend_blocked = (
        isinstance(top, dict) and (
            top.get("spendControlReached") is True
            or (isinstance(top_individual, dict) and _number(top_individual.get("remainingPercent")) == 0)
            or top.get("rateLimitReachedType") in (
                "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached"
            )
        )
    )
    if isinstance(top, dict) and not top_spend_blocked:
        future = [reset for _used, reset in _windows(top) if reset >= now - 60]
        return min(future) if future else None
    return None


def select_claude_reset(record: dict[str, Any], limit_text: str = "", now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    rates = record.get("rate_limits", {})
    if not isinstance(rates, dict):
        return None
    candidates: dict[str, tuple[float | None, float]] = {}
    for key in ("five_hour", "seven_day"):
        window = rates.get(key)
        if not isinstance(window, dict):
            continue
        reset = _number(window.get("resets_at"))
        used = _number(window.get("used_percentage"))
        if reset is not None and reset >= now - 60:
            candidates[key] = (used, reset)
    lower = limit_text.lower()
    if re.search(r"(?:7\s*-?\s*day|week)", lower) and "seven_day" in candidates:
        return candidates["seven_day"][1]
    if re.search(r"(?:5\s*-?\s*hour|five\s+hour)", lower) and "five_hour" in candidates:
        return candidates["five_hour"][1]
    exhausted = [reset for used, reset in candidates.values() if used is not None and used >= 99.0]
    if exhausted:
        return max(exhausted)
    return min((reset for _used, reset in candidates.values()), default=None)


def parse_displayed_reset(text: str, now: dt.datetime | None = None) -> float | None:
    """Parse conservative local reset displays emitted by Codex as a protocol fallback."""
    now = now or dt.datetime.now().astimezone()
    clean = strip_ansi(text)
    epoch = re.search(r"(?:reset|try again)[^\n]{0,60}?\b(1[0-9]{9})\b", clean, re.I)
    if epoch:
        return float(epoch.group(1))
    patterns = (
        r"(?:resets?|try again)(?:\s+at|\s+after|:)?\s*"
        r"(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?[, ]+)?"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AP]M)?",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, re.I)
        if not match:
            continue
        hour = int(match.group("hour"))
        ampm = match.group("ampm")
        if ampm:
            hour = hour % 12 + (12 if ampm.upper() == "PM" else 0)
        month = now.month
        day = now.day
        year = now.year
        if match.group("month"):
            try:
                month = dt.datetime.strptime(match.group("month")[:3], "%b").month
            except ValueError:
                continue
            day = int(match.group("day"))
            year = int(match.group("year") or now.year)
        try:
            candidate = now.replace(year=year, month=month, day=day, hour=hour,
                                    minute=int(match.group("minute")), second=0, microsecond=0)
        except ValueError:
            continue
        if not match.group("month") and candidate <= now:
            candidate += dt.timedelta(days=1)
        elif match.group("month") and not match.group("year") and candidate < now - dt.timedelta(days=2):
            candidate = candidate.replace(year=year + 1)
        return candidate.timestamp()
    return None


def codex_rate_limits(timeout: float = 6.0, command: str = "codex") -> dict[str, Any] | None:
    binary = shutil.which(command)
    if not binary:
        return None
    try:
        proc = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
    except OSError:
        return None
    messages = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "autoresume", "title": "AutoResume", "version": VERSION}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/rateLimits/read", "params": {}},
    ]
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for message in messages:
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not selector.select(min(0.25, max(0.0, deadline - time.monotonic()))):
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                return message if "result" in message else None
    except (OSError, BrokenPipeError):
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
    return None


def tmux(*args: str, socket: str | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["tmux"]
    if socket:
        command.extend((["-L", socket[2:]]) if socket.startswith("L:") else ["-S", socket.removeprefix("S:")])
    return subprocess.run([*command, *args], text=True, capture_output=True, check=check)


def pane_alive(pane: str, provider_pid: int | None, socket: str | None = None) -> bool:
    if provider_pid:
        try:
            os.kill(provider_pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
    result = tmux("display-message", "-p", "-t", pane, "#{pane_dead}", socket=socket)
    return result.returncode == 0 and result.stdout.strip() != "1"


def capture_pane(pane: str, socket: str | None = None) -> str:
    result = tmux("capture-pane", "-p", "-J", "-S", "-300", "-t", pane, socket=socket)
    return strip_ansi(result.stdout) if result.returncode == 0 else ""


def transcript_tail(record: dict[str, Any], max_bytes: int = 131072, modified_after: float | None = None) -> str:
    raw_path = record.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        return ""
    try:
        if modified_after is not None and os.path.getmtime(raw_path) < modified_after:
            return ""
        with open(raw_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def composer_idle(screen: str) -> bool:
    lines = [line.rstrip() for line in strip_ansi(screen).splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    lines = lines[-18:]
    if any(line.strip() == "AUTORESUME_IDLE" for line in lines):
        return True
    for line in reversed(lines):
        compact = line.strip(" │┃┆┊")
        match = re.match(r"^[›❯>]\s*(.*)$", compact)
        if match:
            tail = match.group(1).strip()
            return tail == "" or tail.startswith(("Ask", "Type", "Message"))
    return False


def pane_has_goal(screen: str) -> bool:
    lower = screen.lower()
    return "/goal resume" in lower or bool(re.search(r"goal.{0,80}(?:usage.limit|paused|persisted)", lower, re.S))


def timer_due(reset_at: float, grace_seconds: float, now: float | None = None) -> bool:
    """Wall-clock comparison intentionally fires overdue timers after system sleep."""
    return (time.time() if now is None else now) >= reset_at + grace_seconds


def send_to_pane(pane: str, prompt: str, socket: str | None = None) -> bool:
    literal = tmux("send-keys", "-t", pane, "-l", prompt, socket=socket)
    if literal.returncode != 0:
        return False
    return tmux("send-keys", "-t", pane, "Enter", socket=socket).returncode == 0


def claude_record(instance: str) -> dict[str, Any]:
    value = load_json(state_home() / "claude" / f"{instance}.json", {})
    return value if isinstance(value, dict) else {}


def watcher(provider: str, instance: str, pane: str, provider_pid: int | None = None,
            tmux_socket: str | None = None) -> int:
    cfg = load_config()
    poll = float(cfg["poll_seconds"])
    grace = float(cfg["grace_seconds"])
    armed_reset: float | None = None
    sent_reset: float | None = None
    sent_limit_count = 0
    status = "watching"
    started_wall = time.time()
    write_state(instance, provider=provider, pane=pane, pid=provider_pid, tmux_socket=tmux_socket,
                status=status, started_at=int(started_wall))
    log(instance, f"watcher started provider={provider} pane={pane} pid={provider_pid or '-'}")
    while pane_alive(pane, provider_pid, tmux_socket):
        cfg = load_config()
        if not cfg.get("enabled", True):
            status = "disabled"
            write_state(instance, status=status)
            time.sleep(poll)
            continue
        screen = capture_pane(pane, tmux_socket)
        record: dict[str, Any] = {}
        evidence = screen
        if provider == "claude":
            record = claude_record(instance)
            # A transcript can contain limits from earlier turns. Only accept transcript
            # evidence written during this watcher lifetime; the pane remains primary.
            evidence += "\n" + transcript_tail(record, modified_after=started_wall - 2)
        limit_count = hard_limit_count(provider, evidence)
        limited = limit_count > sent_limit_count
        if limited and (armed_reset is None):
            reset: float | None
            source = ""
            if provider == "codex":
                response = codex_rate_limits()
                reset = select_codex_reset(response or {})
                source = "app-server"
                if reset is None:
                    reset = parse_displayed_reset(screen)
                    source = "terminal fallback"
            else:
                reset = select_claude_reset(record, evidence)
                source = "status line"
            if reset is not None:
                armed_reset = reset
                status = "waiting"
                due = reset + grace
                write_state(instance, status=status, reset_at=int(reset), due_at=int(due), source=source)
                log(instance, f"hard usage limit confirmed; reset={int(reset)} source={source} due={int(due)}")
            elif reset is None:
                write_state(instance, status="limit-detected-no-reset")
        if armed_reset is not None and timer_due(armed_reset, grace):
            if not pane_alive(pane, provider_pid, tmux_socket):
                break
            if not composer_idle(screen):
                if status != "draft-blocked":
                    status = "draft-blocked"
                    write_state(instance, status=status)
                    log(instance, "continuation delayed: composer is not verifiably empty")
            else:
                prompt = "/goal resume" if provider == "codex" and pane_has_goal(screen) else str(cfg["continuation_prompt"])
                if send_to_pane(pane, prompt, tmux_socket):
                    sent_reset = armed_reset
                    sent_limit_count = limit_count
                    armed_reset = None
                    status = "continued"
                    write_state(instance, status=status, prompt=prompt, sent_at=int(time.time()), reset_at=int(sent_reset))
                    log(instance, f"sent {prompt!r}")
                else:
                    break
        time.sleep(max(0.1, float(cfg.get("poll_seconds", poll))))
    write_state(instance, status="exited", exited_at=int(time.time()))
    log(instance, "provider process exited; watcher stopped")
    return 0


def find_provider(provider: str) -> str | None:
    override = os.environ.get(f"AUTORESUME_{provider.upper()}_BIN")
    if override:
        return override
    return shutil.which(provider)


def is_interactive(provider: str, args: list[str]) -> bool:
    if os.environ.get("AUTORESUME_FORCE_INTERACTIVE") == "1":
        return True
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if any(arg in ("-h", "--help", "-V", "--version") for arg in args):
        return False
    if provider == "codex":
        return not any(arg in ("exec", "e", "app-server", "mcp-server", "completion", "cloud") for arg in args[:1])
    if any(arg in ("-p", "--print") or arg.startswith("--print=") for arg in args):
        return False
    return not any(arg in ("agents", "auth", "doctor", "install", "mcp", "plugin", "setup-token", "update", "upgrade") for arg in args[:1])


def passthrough(binary: str, args: list[str]) -> int:
    os.execv(binary, [binary, *args])
    return 127


def start_watcher(provider: str, instance: str, pane: str, pid: int | None,
                  tmux_socket: str | None = None) -> None:
    argv = [sys.executable, str(Path(__file__).resolve()), "_watch", provider, instance, pane]
    if pid:
        argv.extend(["--pid", str(pid)])
    if tmux_socket:
        argv.extend(["--tmux-socket", tmux_socket])
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def run_provider(provider: str, args: list[str]) -> int:
    binary = find_provider(provider)
    if not binary:
        print(f"autoresume: {provider} was not found in PATH", file=sys.stderr)
        return 127
    cfg = load_config()
    if os.environ.get("AUTORESUME_DISABLE") == "1" or not cfg.get("enabled", True) or not is_interactive(provider, args):
        return passthrough(binary, args)
    if not shutil.which("tmux"):
        print("autoresume: tmux is required for supervised interactive sessions", file=sys.stderr)
        return 126
    instance = f"{provider}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    environment = os.environ.copy()
    environment["AUTORESUME_INSTANCE_ID"] = instance
    environment["AUTORESUME_PROVIDER"] = provider
    current_pane = environment.get("TMUX_PANE") if environment.get("TMUX") else None
    if current_pane:
        socket_path = environment["TMUX"].split(",", 1)[0]
        socket_spec = f"S:{socket_path}"
        tmux("set-option", "-p", "-t", current_pane, "@autoresume_instance", instance, socket=socket_spec)
        start_watcher(provider, instance, current_pane, os.getpid(), socket_spec)
        os.execve(binary, [binary, *args], environment)
        return 127

    unique = uuid.uuid4().hex[:12]
    session = f"autoresume-{unique}"
    socket_name = f"autoresume-{unique}"
    socket_spec = f"L:{socket_name}"
    # A private tmux server inherits this invocation's complete environment. That avoids
    # stale environment variables from an unrelated, already-running tmux server.
    command = ["tmux", "-L", socket_name, "new-session", "-d", "-s", session, "-n", provider, "-c", os.getcwd(), "--",
               "env", f"AUTORESUME_INSTANCE_ID={instance}", f"AUTORESUME_PROVIDER={provider}", binary, *args]
    created = subprocess.run(command, text=True, capture_output=True)
    if created.returncode != 0:
        print(f"autoresume: could not create tmux session: {created.stderr.strip()}", file=sys.stderr)
        return created.returncode
    panes = tmux("list-panes", "-t", session, "-F", "#{pane_id} #{pane_pid}", socket=socket_spec)
    try:
        pane, pid_text = panes.stdout.strip().splitlines()[0].split()
        provider_pid = int(pid_text)
    except (IndexError, ValueError):
        tmux("kill-session", "-t", session, socket=socket_spec)
        print("autoresume: could not identify provider pane", file=sys.stderr)
        return 1
    tmux("set-option", "-p", "-t", pane, "@autoresume_instance", instance, socket=socket_spec)
    start_watcher(provider, instance, pane, provider_pid, socket_spec)
    return subprocess.call(["tmux", "-L", socket_name, "attach-session", "-t", session])


def cmd_status(_args: argparse.Namespace) -> int:
    ensure_dirs()
    paths = sorted((state_home() / "instances").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        print("No supervised sessions have been recorded.")
        return 0
    print(f"{'INSTANCE':38} {'PROVIDER':8} {'STATUS':24} {'DUE'}")
    for path in paths:
        item = load_json(path, {})
        if not isinstance(item, dict):
            continue
        if item.get("status") not in ("exited", "stale") and isinstance(item.get("pane"), str):
            if not pane_alive(item["pane"], item.get("pid") if isinstance(item.get("pid"), int) else None,
                              item.get("tmux_socket") if isinstance(item.get("tmux_socket"), str) else None):
                item = write_state(str(item.get("instance", path.stem)), status="stale")
        due = item.get("due_at")
        due_text = dt.datetime.fromtimestamp(due).astimezone().isoformat(timespec="seconds") if isinstance(due, (int, float)) else "-"
        print(f"{str(item.get('instance', path.stem))[:38]:38} {str(item.get('provider', '-')):8} {str(item.get('status', '-')):24} {due_text}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.instance:
        path = state_home() / f"{args.instance}.log"
    else:
        files = sorted(state_home().glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            print("No logs have been recorded.")
            return 0
        path = files[0]
    try:
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0
    except OSError as exc:
        print(f"autoresume: {exc}", file=sys.stderr)
        return 1


def cmd_doctor(_args: argparse.Namespace) -> int:
    problems = 0
    print(f"AutoResume {VERSION}")
    print(f"Python: {sys.version.split()[0]} ({'ok' if sys.version_info >= (3, 9) else 'requires 3.9+'})")
    if sys.version_info < (3, 9):
        problems += 1
    for name in ("tmux", "codex", "claude"):
        binary = shutil.which(name)
        print(f"{name}: {binary or 'not found'}")
        if name == "tmux" and not binary:
            problems += 1
    cfg = load_config()
    print(f"Enabled: {str(cfg['enabled']).lower()}")
    print(f"Config: {config_home() / 'config.json'}")
    print(f"State: {state_home()}")
    settings = load_json(Path.home() / ".claude" / "settings.json", {})
    status_command = settings.get("statusLine", {}).get("command") if isinstance(settings, dict) and isinstance(settings.get("statusLine"), dict) else None
    print(f"Claude status line: {status_command or 'not configured'}")
    response = codex_rate_limits(timeout=4.0) if shutil.which("codex") else None
    print(f"Codex app-server rate limits: {'ok' if response else 'unavailable (terminal fallback will be used)'}")
    return 1 if problems else 0


def cmd_toggle(enabled: bool) -> int:
    cfg = load_config()
    cfg["enabled"] = enabled
    atomic_json(config_home() / "config.json", cfg)
    print(f"AutoResume is now {'enabled' if enabled else 'disabled'}.")
    return 0


def cmd_update(_args: argparse.Namespace) -> int:
    url = "https://raw.githubusercontent.com/RussellPetty/AutoResume/main/install.sh"
    try:
        script = urlopen(url, timeout=20).read()
    except OSError as exc:
        print(f"autoresume: update download failed: {exc}", file=sys.stderr)
        return 1
    proc = subprocess.run(["bash", "-s", "--", "--yes"], input=script)
    return proc.returncode


def cmd_uninstall(_args: argparse.Namespace) -> int:
    installer = share_home() / "current" / "install.sh"
    if not installer.exists():
        print("autoresume: installed uninstaller not found; download install.sh and run it with --uninstall", file=sys.stderr)
        return 1
    return subprocess.call(["bash", str(installer), "--uninstall", "--yes"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoresume", description=__doc__)
    parser.add_argument("--version", action="version", version=f"AutoResume {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a provider with interactive supervision")
    run.add_argument("provider", choices=("codex", "claude"))
    run.add_argument("args", nargs=argparse.REMAINDER)
    watch = sub.add_parser("_watch", help=argparse.SUPPRESS)
    watch.add_argument("provider", choices=("codex", "claude"))
    watch.add_argument("instance")
    watch.add_argument("pane")
    watch.add_argument("--pid", type=int)
    watch.add_argument("--tmux-socket")
    status = sub.add_parser("status", help="show supervised sessions")
    status.set_defaults(func=cmd_status)
    logs = sub.add_parser("logs", help="show watcher logs")
    logs.add_argument("instance", nargs="?")
    logs.set_defaults(func=cmd_logs)
    doctor = sub.add_parser("doctor", help="check dependencies and adapters")
    doctor.set_defaults(func=cmd_doctor)
    enable = sub.add_parser("enable", help="enable supervision")
    enable.set_defaults(func=lambda _a: cmd_toggle(True))
    disable = sub.add_parser("disable", help="disable supervision")
    disable.set_defaults(func=lambda _a: cmd_toggle(False))
    update = sub.add_parser("update", help="install the latest release")
    update.set_defaults(func=cmd_update)
    uninstall = sub.add_parser("uninstall", help="remove AutoResume")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        provider_args = args.args[1:] if args.args[:1] == ["--"] else args.args
        return run_provider(args.provider, provider_args)
    if args.command == "_watch":
        return watcher(args.provider, args.instance, args.pane, args.pid, args.tmux_socket)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
