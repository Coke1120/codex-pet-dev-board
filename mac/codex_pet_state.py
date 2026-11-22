#!/usr/bin/env python3
"""Shared privacy-safe Codex lifecycle state aggregation and sink for macOS.

Both the ESP32-P4 Serial daemon and the Mac-local floating media player consume
the exact same hook session JSON files, state priority, and stale-session TTL.
"""

import argparse
import json
import math
import os
import re
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

VALID_STATES = ("idle", "running", "waiting", "review")
VALID_SOURCES = ("sessions", "manual")
STATE_PRIORITY = {"idle": 0, "running": 1, "review": 2, "waiting": 3}
HOOK_RECORD_NAME = re.compile(r"^[0-9a-f]{24}\.json$")
HOOK_RECORD_KEYS = frozenset(("version", "state", "event", "updated_at"))
CURRENT_STATE_KEYS = frozenset(
    ("version", "state", "source", "active_sessions", "updated_at")
)
DEFAULT_ACTIVE_TTL = 900.0
DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_HEARTBEAT_INTERVAL = 5.0


def positive_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("expected a positive finite number")
    return value


def resolve_state_dir(
    env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None
) -> Path:
    source_env = os.environ if env is None else env
    override = source_env.get("CODEX_PET_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base_home = Path.home() if home is None else home
    return base_home / "Library" / "Application Support" / "CodexPet" / "sessions"


def default_state_dir() -> Path:
    return resolve_state_dir()


def default_current_state_path(
    state_dir: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    source_env = os.environ if env is None else env
    override = source_env.get("CODEX_PET_CURRENT_STATE_FILE")
    if override:
        return Path(override).expanduser()
    resolved_sessions = (
        state_dir
        if state_dir is not None
        else resolve_state_dir(env=source_env, home=home)
    )
    return resolved_sessions.parent / "runtime" / "current_state.json"


def _file_identity(path: Path) -> Optional[Tuple[int, int, int, int]]:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _prune_if_unchanged(
    path: Path, identity: Optional[Tuple[int, int, int, int]]
) -> None:
    if identity is None or _file_identity(path) != identity:
        return
    try:
        path.unlink()
    except OSError:
        # Another hook may have replaced it, or a transient filesystem error
        # may make it readable again on the next poll.
        pass


def read_active_states(
    state_dir: Path,
    now: Optional[float] = None,
    active_ttl: float = DEFAULT_ACTIVE_TTL,
    *,
    prune: bool = True,
) -> List[Tuple[str, float]]:
    current = time.time() if now is None else now
    active: List[Tuple[str, float]] = []
    if not state_dir.exists():
        return active
    for path in state_dir.glob("*.json"):
        if HOOK_RECORD_NAME.fullmatch(path.name) is None:
            continue
        identity = _file_identity(path) if prune else None
        if prune and identity is None:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or set(record) != HOOK_RECORD_KEYS
                or record.get("version") != 1
                or not isinstance(record.get("event"), str)
            ):
                continue
            state = record["state"]
            updated_at = float(record["updated_at"])
        except OSError:
            continue
        except (ValueError, TypeError, KeyError):
            continue
        if state not in VALID_STATES or not math.isfinite(updated_at):
            _prune_if_unchanged(path, identity)
            continue
        age = current - updated_at
        if age < -60 or age > active_ttl:
            _prune_if_unchanged(path, identity)
            continue
        active.append((state, updated_at))
    return active


def aggregate_state(active: Iterable[Tuple[str, float]]) -> str:
    entries = list(active)
    if not entries:
        return "idle"
    # A recent Stop event writes idle for that session; concurrent active
    # sessions still win through priority.
    return max(entries, key=lambda item: (STATE_PRIORITY[item[0]], item[1]))[0]


def resolve_state_snapshot(
    state_dir: Path,
    now: Optional[float] = None,
    active_ttl: float = DEFAULT_ACTIVE_TTL,
) -> Tuple[str, int]:
    # The player observes hook records without deleting files that the hook may replace.
    active = read_active_states(state_dir, now=now, active_ttl=active_ttl, prune=False)
    return aggregate_state(active), len(active)


def should_send_state(
    desired: str, sent: Optional[str], now: float, next_heartbeat_at: float
) -> bool:
    return desired != sent or now >= next_heartbeat_at


def write_current_state(
    path: Path,
    state: str,
    source: str = "sessions",
    active_sessions: int = 0,
    updated_at: Optional[float] = None,
) -> Dict[str, Any]:
    if state not in VALID_STATES:
        raise ValueError("invalid state: {}".format(state))
    if source not in VALID_SOURCES:
        raise ValueError("invalid source: {}".format(source))
    if isinstance(active_sessions, bool) or not isinstance(active_sessions, int) or active_sessions < 0:
        raise ValueError("active_sessions must be a non-negative integer")
    timestamp = time.time() if updated_at is None else float(updated_at)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("updated_at must be a finite non-negative timestamp")

    payload: Dict[str, Any] = {
        "version": 1,
        "state": state,
        "source": source,
        "active_sessions": active_sessions,
        "updated_at": round(timestamp, 3),
    }

    temporary: Optional[Path] = None
    fd: Optional[int] = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".{}-".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return payload


def read_current_state(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if set(payload) != CURRENT_STATE_KEYS:
        return None
    if payload.get("version") != 1:
        return None
    if payload.get("state") not in VALID_STATES:
        return None
    if payload.get("source") not in VALID_SOURCES:
        return None
    active_sessions = payload.get("active_sessions")
    if isinstance(active_sessions, bool) or not isinstance(active_sessions, int) or active_sessions < 0:
        return None
    try:
        updated_at = float(payload["updated_at"])
    except (ValueError, TypeError):
        return None
    if not math.isfinite(updated_at) or updated_at < 0:
        return None
    return {
        "version": 1,
        "state": payload["state"],
        "source": payload["source"],
        "active_sessions": active_sessions,
        "updated_at": updated_at,
    }


class StateAggregator:
    """Publish aggregated lifecycle state only on state change or heartbeat."""

    def __init__(
        self,
        state_dir: Path,
        state_file: Path,
        active_ttl: float = DEFAULT_ACTIVE_TTL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        clock: Any = time.monotonic,
        wall_clock: Any = time.time,
    ) -> None:
        self.state_dir = state_dir
        self.state_file = state_file
        self.active_ttl = active_ttl
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._clock = clock
        self._wall_clock = wall_clock
        self._last_written_state: Optional[str] = None
        self._next_heartbeat_at = 0.0
        self.write_count = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None

    def step(self) -> Tuple[str, bool]:
        now_wall = float(self._wall_clock())
        now_mono = float(self._clock())
        desired, active_count = resolve_state_snapshot(
            self.state_dir, now=now_wall, active_ttl=self.active_ttl
        )
        if should_send_state(
            desired, self._last_written_state, now_mono, self._next_heartbeat_at
        ):
            write_current_state(
                self.state_file,
                desired,
                source="sessions",
                active_sessions=active_count,
                updated_at=now_wall,
            )
            self._last_written_state = desired
            self._next_heartbeat_at = now_mono + self.heartbeat_interval
            self.write_count += 1
            return desired, True
        return desired, False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="codex-pet-state-aggregator", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.poll_interval * 2.0))
            self._thread = None

    def last_error(self) -> Optional[str]:
        return self._last_error

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
                self._last_error = None
            except OSError as exc:
                self._last_error = "{}: {}".format(type(exc).__name__, exc)
            self._stop.wait(self.poll_interval)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate Codex Pet lifecycle session files and publish "
            "current_state.json for Mac-local consumers."
        )
    )
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="path to current_state.json (default: <state-dir>/../runtime/current_state.json)",
    )
    parser.add_argument(
        "--active-ttl", type=positive_float, default=DEFAULT_ACTIVE_TTL
    )
    parser.add_argument(
        "--poll", type=positive_float, default=DEFAULT_POLL_INTERVAL
    )
    parser.add_argument(
        "--heartbeat",
        type=positive_float,
        default=DEFAULT_HEARTBEAT_INTERVAL,
        help="seconds between unchanged-state heartbeat writes (default: 5)",
    )
    parser.add_argument(
        "--print-state",
        action="store_true",
        help="print the resolved aggregate state once and exit",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="also write current_state.json when used with --print-state",
    )
    parser.add_argument(
        "--force-state",
        choices=VALID_STATES,
        help="write a manual override state to current_state.json and exit",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="continuously update current_state.json on state changes and heartbeat",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="resolve and write current_state.json once, then exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON output instead of a bare state name",
    )
    args = parser.parse_args(argv)

    state_file = (
        args.state_file
        if args.state_file is not None
        else default_current_state_path(args.state_dir)
    )

    selected = sum(
        bool(flag)
        for flag in (args.print_state, args.force_state, args.watch, args.once)
    )
    if selected == 0:
        args.print_state = True
    elif selected > 1:
        parser.error("choose at most one of --print-state, --force-state, --watch, or --once")

    if args.force_state:
        payload = write_current_state(
            state_file, args.force_state, source="manual", active_sessions=0
        )
        print(json.dumps(payload, separators=(",", ":")) if args.json else payload["state"])
        return 0

    if args.print_state:
        state, active_count = resolve_state_snapshot(
            args.state_dir, active_ttl=args.active_ttl
        )
        if args.write:
            payload = write_current_state(
                state_file, state, source="sessions", active_sessions=active_count
            )
        else:
            payload = {
                "version": 1,
                "state": state,
                "source": "sessions",
                "active_sessions": active_count,
                "updated_at": round(time.time(), 3),
            }
        print(json.dumps(payload, separators=(",", ":")) if args.json else state)
        return 0

    if args.once:
        state, active_count = resolve_state_snapshot(
            args.state_dir, active_ttl=args.active_ttl
        )
        payload = write_current_state(
            state_file, state, source="sessions", active_sessions=active_count
        )
        print(json.dumps(payload, separators=(",", ":")) if args.json else state)
        return 0

    stopped = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    aggregator = StateAggregator(
        state_dir=args.state_dir,
        state_file=state_file,
        active_ttl=args.active_ttl,
        poll_interval=args.poll,
        heartbeat_interval=args.heartbeat,
    )
    previous_state: Optional[str] = None
    while not stopped:
        state, wrote = aggregator.step()
        if wrote and state != previous_state:
            print(state, flush=True)
            previous_state = state
        time.sleep(args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
