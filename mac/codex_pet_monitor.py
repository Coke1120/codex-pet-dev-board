#!/usr/bin/env python3
"""Select a lifecycle provider and accept privacy-safe local hook events."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from codex_pet_hook import default_state_dir, write_private_json

PROVIDERS = {
    "codex": "Codex (default)",
    "hermes": "Hermes Agent",
    "openclaw": "OpenClaw",
    "claude-code": "Claude Code",
}

HOOK_EVENTS = {
    "hermes": {
        "pre_llm_call": "running",
        "post_llm_call": "idle",
        "on_session_end": "idle",
    },
    "openclaw": {"before_agent_run": "running", "agent_end": "idle"},
    "claude-code": {
        "UserPromptSubmit": "running",
        "PermissionRequest": "waiting",
        "PreToolUse": "running",
        "PostToolUse": "running",
        "Stop": "idle",
        "SessionEnd": "idle",
        "StopFailure": "idle",
    },
}

RECORD_NAME = re.compile(r"[0-9a-f]{24}\.json")
RECORD_KEYS = {"version", "state", "event", "updated_at"}
MAX_HOOK_BYTES = 16 * 1024 * 1024
MIN_RECORD_RETENTION = 900.0


class MonitorSelection(NamedTuple):
    selected: str
    # None uses the legacy Codex reader. An empty list is a ready, idle provider.
    active: Optional[List[Tuple[str, float]]]
    diagnostic: Optional[str] = None


def settings_path(state_dir: Path) -> Path:
    standard = Path.home() / "Library" / "Application Support" / "CodexPet" / "sessions"
    root = state_dir.parent if state_dir == standard else state_dir
    return root / "monitor" / "provider.json"


def _read_small_json(path: Path):
    # A corrupt local file must not stall the daemon (for example, a FIFO).
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("expected a regular file")
        raw = handle.read(1025)
    if len(raw) > 1024:
        raise ValueError("JSON file too large")
    return json.loads(raw)


def _make_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("expected a directory")
    path.chmod(0o700)


def save_provider(state_dir: Path, provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("unsupported provider")
    destination = settings_path(state_dir)
    _make_private_dir(destination.parent)
    write_private_json(destination, {"version": 1, "provider": provider})


def _prune_expired_record(path: Path, original: os.stat_result) -> None:
    try:
        current = path.lstat()
        if (current.st_ino, current.st_mtime_ns) == (original.st_ino, original.st_mtime_ns):
            path.unlink()
    except OSError:
        # A writer or another consumer can race this best-effort cleanup.
        pass


def _read_provider_states(
    source: Path, events: dict, now: float, active_ttl: float
) -> Optional[List[Tuple[str, float]]]:
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise ValueError("expected a provider directory")
    active = []
    try:
        marked_ready = stat.S_ISREG((source / ".ready").lstat().st_mode)
    except FileNotFoundError:
        marked_ready = False
    ready = marked_ready
    for path in source.iterdir():
        if not RECORD_NAME.fullmatch(path.name):
            continue
        try:
            identity = path.lstat()
            record = _read_small_json(path)
        except FileNotFoundError:
            continue
        if (
            not isinstance(record, dict)
            or set(record) != RECORD_KEYS
            or type(record["version"]) is not int
            or record["version"] != 1
            or not isinstance(record["event"], str)
            or record["event"] not in events
            or record["state"] != events[record["event"]]
            or type(record["updated_at"]) not in (float, int)
            or not 0 < record["updated_at"] <= now + 60
        ):
            raise ValueError("invalid provider record")
        ready = True
        age = now - record["updated_at"]
        if age <= active_ttl:
            active.append((record["state"], record["updated_at"]))
        elif marked_ready and age > max(MIN_RECORD_RETENTION, active_ttl):
            _prune_expired_record(path, identity)
    return active if ready else None


def read_monitor_selection(
    state_dir: Path, active_ttl: float = 900.0, now: Optional[float] = None
) -> MonitorSelection:
    """Read the selected source for a poll, or request the existing Codex path."""
    try:
        settings = _read_small_json(settings_path(state_dir))
    except FileNotFoundError:
        return MonitorSelection("codex", None)
    except (OSError, ValueError, RecursionError):
        return MonitorSelection("codex", None, "Monitor settings are unreadable or invalid.")

    if (
        not isinstance(settings, dict)
        or set(settings) != {"version", "provider"}
        or type(settings["version"]) is not int
        or not isinstance(settings["provider"], str)
    ):
        return MonitorSelection("codex", None, "Monitor settings are invalid.")
    provider = settings["provider"]
    if settings["version"] != 1 or provider not in PROVIDERS:
        return MonitorSelection(
            "codex", None, "Monitor settings use an unsupported version or provider."
        )
    if provider == "codex":
        return MonitorSelection(provider, None)

    label = PROVIDERS[provider]
    try:
        active = _read_provider_states(
            state_dir / provider,
            HOOK_EVENTS[provider],
            time.time() if now is None else now,
            active_ttl,
        )
    except FileNotFoundError:
        active = None
    except (OSError, ValueError, RecursionError):
        return MonitorSelection(
            provider, None, f"{label} hook source is unreadable or invalid."
        )

    if active is None:
        return MonitorSelection(
            provider, None, f"No {label} hook event received. Check hook setup and trust."
        )
    return MonitorSelection(provider, active)


def _valid_identity(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 1024 and bool(value.strip())


def write_provider_event(provider: str, payload, state_dir: Path) -> None:
    """Record an allowlisted hook event without retaining its input or identity."""
    if provider not in HOOK_EVENTS or not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in HOOK_EVENTS[provider]:
        return
    identity = payload.get("run_id" if provider == "openclaw" else "session_id")
    profile = payload.get("profile") if provider == "hermes" else None
    if not _valid_identity(identity) or (provider == "hermes" and not _valid_identity(profile)):
        return
    # JSON tuple encoding keeps profile + session boundaries unambiguous.
    key = hashlib.sha256(json.dumps([profile, identity]).encode("ascii")).hexdigest()[:24]
    source = state_dir / provider
    _make_private_dir(source)
    record = {
        "version": 1,
        "state": HOOK_EVENTS[provider][event],
        "event": event,
        "updated_at": time.time(),
    }
    write_private_json(source / (key + ".json"), record)
    try:
        (source / ".ready").touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    settings = commands.add_parser("settings", help="serve the local provider settings page")
    settings.add_argument("--state-dir", type=Path, default=default_state_dir())
    hook = commands.add_parser("hook", help="accept one provider hook JSON object on stdin")
    hook.add_argument("provider", choices=HOOK_EVENTS)
    args = parser.parse_args()

    if args.command == "settings":
        from codex_pet_monitor_settings import serve_settings

        serve_settings(args.state_dir)
    else:
        try:
            raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
            if len(raw) <= MAX_HOOK_BYTES:
                write_provider_event(args.provider, json.loads(raw), default_state_dir())
        except (OSError, ValueError, RecursionError):
            # Monitoring is best effort. Never expose input or affect the turn.
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
