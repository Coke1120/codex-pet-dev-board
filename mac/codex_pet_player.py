#!/usr/bin/env python3
"""Mac-local floating Codex Pet media player launcher.

Runs the shared privacy-safe Codex lifecycle aggregator (writing
current_state.json only on state change and 5-second heartbeat) alongside the
native Swift/AVFoundation always-on-top player. Does not require an ESP32-P4
board, pyserial, or a paid Apple Developer account.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

MAC_DIR = Path(__file__).resolve().parent
if str(MAC_DIR) not in sys.path:
    sys.path.insert(0, str(MAC_DIR))

from codex_pet_state import (
    DEFAULT_ACTIVE_TTL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    StateAggregator,
    VALID_STATES,
    default_current_state_path,
    default_state_dir,
    positive_float,
)

PLAYER_SWIFT_SOURCE = MAC_DIR / "player" / "CodexPetPlayer.swift"
MEDIA_EXTENSIONS = (".mp4", ".m4v", ".mov", ".gif")


def default_media_map_path(env: Optional[Mapping[str, str]] = None) -> Path:
    source_env = os.environ if env is None else env
    override = source_env.get("CODEX_PET_MEDIA_MAP")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "CodexPet" / "media-map.json"


def default_cache_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    source_env = os.environ if env is None else env
    override = source_env.get("CODEX_PET_PLAYER_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Caches" / "CodexPet"


def discover_media_dir(media_dir: Path) -> Dict[str, str]:
    resolved_dir = media_dir.expanduser().resolve()
    mapping: Dict[str, str] = {}
    for state in VALID_STATES:
        for extension in MEDIA_EXTENSIONS:
            candidate = resolved_dir / "{}{}".format(state, extension)
            if candidate.is_file():
                mapping[state] = str(candidate)
                break
    return mapping


def load_media_map(path: Path) -> Dict[str, str]:
    resolved_path = path.expanduser().resolve()
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("media map must be a JSON object")
    base_dir = resolved_path.parent
    mapping: Dict[str, str] = {}
    for state in VALID_STATES:
        value = raw.get(state)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("media path for '{}' must be a string".format(state))
        trimmed = value.strip()
        if not trimmed:
            continue
        candidate = Path(trimmed).expanduser()
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()
        else:
            candidate = candidate.resolve()
        mapping[state] = str(candidate)
    return mapping


def resolve_media_map(
    media_map_path: Optional[Path] = None,
    media_dir: Optional[Path] = None,
    overrides: Optional[Mapping[str, Optional[str]]] = None,
) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    if media_dir is not None:
        resolved.update(discover_media_dir(media_dir))
    if media_map_path is not None:
        resolved.update(load_media_map(media_map_path))
    if overrides:
        for state in VALID_STATES:
            raw_value = overrides.get(state)
            if raw_value:
                resolved[state] = str(Path(raw_value).expanduser().resolve())
    return resolved


def write_resolved_media_map(path: Path, mapping: Mapping[str, str]) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".media-map-", suffix=".json", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(dict(mapping), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return path


def swift_source_digest(source_path: Path = PLAYER_SWIFT_SOURCE) -> str:
    return hashlib.sha256(source_path.read_bytes()).hexdigest()[:16]


def build_player_binary(
    cache_dir: Optional[Path] = None,
    source_path: Path = PLAYER_SWIFT_SOURCE,
    force_rebuild: bool = False,
) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("CodexPetPlayer requires macOS (Darwin)")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RuntimeError(
            "swiftc is required to build the native Mac player. "
            "Install Xcode Command Line Tools with: xcode-select --install"
        )
    if not source_path.is_file():
        raise FileNotFoundError("Swift player source not found: {}".format(source_path))

    target_cache = default_cache_dir() if cache_dir is None else cache_dir
    bin_dir = target_cache / "bin"
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = swift_source_digest(source_path)
    binary_path = bin_dir / "CodexPetPlayer-{}".format(digest)
    if binary_path.is_file() and os.access(binary_path, os.X_OK) and not force_rebuild:
        return binary_path

    tmp_binary = bin_dir / ".CodexPetPlayer-{}-{}.tmp".format(digest, os.getpid())
    command = [
        swiftc,
        "-O",
        str(source_path),
        "-framework",
        "AppKit",
        "-framework",
        "AVFoundation",
        "-framework",
        "CoreMedia",
        "-o",
        str(tmp_binary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                "swiftc failed (exit {}):\n{}".format(
                    result.returncode, result.stderr.strip()
                )
            )
        os.chmod(tmp_binary, 0o755)
        os.replace(tmp_binary, binary_path)
    finally:
        if tmp_binary.exists():
            try:
                tmp_binary.unlink()
            except OSError:
                pass
    return binary_path


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Launch the Mac-local always-on-top Codex Pet media player without "
            "requiring an ESP32-P4 board."
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
        "--media-map",
        type=Path,
        default=None,
        help="JSON mapping idle/running/waiting/review to local MP4 or GIF files",
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=None,
        help="directory containing <state>.mp4/.mov/.m4v/.gif clips",
    )
    parser.add_argument("--idle", help="override media file for idle state")
    parser.add_argument("--running", help="override media file for running state")
    parser.add_argument("--waiting", help="override media file for waiting state")
    parser.add_argument("--review", help="override media file for review state")
    parser.add_argument(
        "--force-state",
        choices=VALID_STATES,
        help="start in manual state override for testing without a live Codex turn",
    )
    parser.add_argument(
        "--no-aggregator",
        action="store_true",
        help="do not run the local sessions->current_state.json aggregator thread",
    )
    parser.add_argument(
        "--active-ttl", type=positive_float, default=DEFAULT_ACTIVE_TTL
    )
    parser.add_argument(
        "--poll", type=positive_float, default=DEFAULT_POLL_INTERVAL
    )
    parser.add_argument(
        "--heartbeat", type=positive_float, default=DEFAULT_HEARTBEAT_INTERVAL
    )
    parser.add_argument("--width", type=positive_int, default=300)
    parser.add_argument("--height", type=positive_int, default=324)
    parser.add_argument("--no-always-on-top", action="store_true")
    parser.add_argument("--dock-icon", action="store_true")
    parser.add_argument("--emit-events", action="store_true")
    parser.add_argument("--status-file", type=Path, default=None)
    parser.add_argument("--auto-exit-seconds", type=positive_float, default=None)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="directory for cached native player binary",
    )
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="compile the native Swift player binary and print its path",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print resolved paths and media map as JSON and exit",
    )
    args = parser.parse_args(argv)

    state_file = (
        args.state_file
        if args.state_file is not None
        else default_current_state_path(args.state_dir)
    )

    media_map_path = args.media_map
    if (
        media_map_path is None
        and args.media_dir is None
        and not any((args.idle, args.running, args.waiting, args.review))
    ):
        candidate_default = default_media_map_path()
        if candidate_default.is_file():
            media_map_path = candidate_default

    try:
        resolved_media = resolve_media_map(
            media_map_path=media_map_path,
            media_dir=args.media_dir,
            overrides={
                "idle": args.idle,
                "running": args.running,
                "waiting": args.waiting,
                "review": args.review,
            },
        )
    except (OSError, ValueError) as exc:
        print("Codex Pet player config error: {}".format(exc), file=sys.stderr)
        return 2

    if args.print_config:
        summary: Dict[str, Any] = {
            "state_dir": str(args.state_dir),
            "state_file": str(state_file),
            "media_map": resolved_media,
            "missing_states": [
                state for state in VALID_STATES if state not in resolved_media
            ],
            "always_on_top": not args.no_always_on_top,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    try:
        binary_path = build_player_binary(
            cache_dir=args.cache_dir, force_rebuild=args.rebuild
        )
    except (OSError, RuntimeError) as exc:
        print("Codex Pet player build error: {}".format(exc), file=sys.stderr)
        return 1

    if args.build_only:
        print(binary_path)
        return 0

    if not resolved_media:
        print(
            "Codex Pet player note: no media clips configured; starting in "
            "placeholder mode. Pass --media-map or --media-dir to play local "
            "MP4/GIF loops.",
            file=sys.stderr,
        )

    runtime_dir = state_file.parent
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    effective_map_path = write_resolved_media_map(
        runtime_dir / ".resolved-media-map-{}.json".format(os.getpid()),
        resolved_media,
    )

    aggregator: Optional[StateAggregator] = None
    if not args.no_aggregator:
        aggregator = StateAggregator(
            state_dir=args.state_dir,
            state_file=state_file,
            active_ttl=args.active_ttl,
            poll_interval=args.poll,
            heartbeat_interval=args.heartbeat,
        )
        # Prime current_state.json before the GUI reads it on startup.
        aggregator.step()
        aggregator.start()

    player_cmd = [
        str(binary_path),
        "--state-file",
        str(state_file),
        "--media-map",
        str(effective_map_path),
        "--poll-ms",
        str(max(100, int(round(args.poll * 1000)))),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
    ]
    if args.force_state:
        player_cmd.extend(["--force-state", args.force_state])
    if args.no_always_on_top:
        player_cmd.append("--no-always-on-top")
    if args.dock_icon:
        player_cmd.append("--dock-icon")
    if args.emit_events:
        player_cmd.append("--emit-events")
    if args.status_file is not None:
        player_cmd.extend(["--status-file", str(args.status_file)])
    if args.auto_exit_seconds is not None:
        player_cmd.extend(["--auto-exit-seconds", str(args.auto_exit_seconds)])

    try:
        proc = subprocess.Popen(player_cmd)

        def forward_stop(signum: int, _frame: Any) -> None:
            if proc.poll() is None:
                proc.send_signal(signum)

        signal.signal(signal.SIGINT, forward_stop)
        signal.signal(signal.SIGTERM, forward_stop)
        return proc.wait()
    except OSError as exc:
        print("Codex Pet player launch error: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        if aggregator is not None:
            aggregator.stop()
        try:
            effective_map_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
