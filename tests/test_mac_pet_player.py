#!/usr/bin/env python3
"""Focused tests for the Mac-local Codex Pet state aggregator and player."""

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load {}".format(relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pet_state = load("codex_pet_state_test", "mac/codex_pet_state.py")
pet_player = load("codex_pet_player_test", "mac/codex_pet_player.py")


def write_session_record(state_dir: Path, hex_digit: str, state: str, updated_at: float) -> Path:
    path = state_dir / ((hex_digit * 24) + ".json")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "state": state,
                "event": "UserPromptSubmit",
                "updated_at": updated_at,
            }
        ),
        encoding="utf-8",
    )
    return path


class MacPetStateTests(unittest.TestCase):
    def test_aggregator_writes_only_on_change_and_heartbeat_with_priority_and_ttl(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sessions = base / "sessions"
            sessions.mkdir(mode=0o700)
            state_file = base / "runtime" / "current_state.json"

            now_wall = [1_000.0]
            now_mono = [10.0]
            aggregator = pet_state.StateAggregator(
                state_dir=sessions,
                state_file=state_file,
                active_ttl=30.0,
                heartbeat_interval=5.0,
                clock=lambda: now_mono[0],
                wall_clock=lambda: now_wall[0],
            )

            # Empty sessions -> idle written once
            state, wrote = aggregator.step()
            self.assertEqual((state, wrote), ("idle", True))
            self.assertEqual(aggregator.write_count, 1)
            self.assertEqual(stat.S_IMODE(state_file.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)

            # Unchanged before 5s heartbeat -> no rewrite
            now_mono[0] = 14.9
            now_wall[0] = 1_004.9
            self.assertEqual(aggregator.step(), ("idle", False))
            self.assertEqual(aggregator.write_count, 1)

            # At 5s heartbeat -> rewrite
            now_mono[0] = 15.0
            now_wall[0] = 1_005.0
            self.assertEqual(aggregator.step(), ("idle", True))
            self.assertEqual(aggregator.write_count, 2)

            # Multi-session priority: waiting > review > running > idle
            write_session_record(sessions, "a", "running", 1_005.0)
            write_session_record(sessions, "b", "review", 1_004.0)
            write_session_record(sessions, "c", "waiting", 1_003.0)
            now_mono[0] = 15.5
            now_wall[0] = 1_005.5
            self.assertEqual(aggregator.step(), ("waiting", True))
            payload = pet_state.read_current_state(state_file)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["state"], "waiting")
            self.assertEqual(payload["source"], "sessions")
            self.assertEqual(payload["active_sessions"], 3)
            self.assertEqual(set(payload), set(pet_state.CURRENT_STATE_KEYS))

            # Stale TTL expiry returns to idle
            now_mono[0] = 16.0
            now_wall[0] = 1_050.0
            self.assertEqual(aggregator.step(), ("idle", True))
            self.assertEqual(pet_state.read_current_state(state_file)["state"], "idle")
            self.assertEqual(len(list(sessions.glob("*.json"))), 3)

    def test_cli_print_state_and_force_state_are_privacy_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sessions = base / "sessions"
            sessions.mkdir(mode=0o700)
            state_file = base / "runtime" / "current_state.json"

            out = io.StringIO()
            with redirect_stdout(out):
                rc = pet_state.main(
                    [
                        "--state-dir",
                        str(sessions),
                        "--state-file",
                        str(state_file),
                        "--print-state",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "idle")

            out_force = io.StringIO()
            with redirect_stdout(out_force):
                rc = pet_state.main(
                    [
                        "--state-dir",
                        str(sessions),
                        "--state-file",
                        str(state_file),
                        "--force-state",
                        "review",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            parsed = json.loads(out_force.getvalue())
            self.assertEqual(parsed["state"], "review")
            self.assertEqual(parsed["source"], "manual")
            self.assertEqual(pet_state.read_current_state(state_file)["state"], "review")


class MacPetPlayerConfigTests(unittest.TestCase):
    def test_media_map_and_dir_resolution_and_missing_soft_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            clips = base / "clips"
            clips.mkdir()
            (clips / "idle.mp4").write_bytes(b"mp4")
            (clips / "idle.gif").write_bytes(b"gif")
            (clips / "running.gif").write_bytes(b"gif")
            map_file = base / "media-map.json"
            map_file.write_text(
                json.dumps(
                    {
                        "idle": "clips/idle.mp4",
                        "waiting": "clips/missing-waiting.mp4",
                    }
                ),
                encoding="utf-8",
            )

            discovered = pet_player.discover_media_dir(clips)
            self.assertEqual(discovered["idle"], str((clips / "idle.mp4").resolve()))
            self.assertEqual(discovered["running"], str((clips / "running.gif").resolve()))
            self.assertNotIn("waiting", discovered)

            resolved = pet_player.resolve_media_map(
                media_map_path=map_file,
                media_dir=clips,
                overrides={"review": str(clips / "running.gif")},
            )
            self.assertEqual(resolved["idle"], str((clips / "idle.mp4").resolve()))
            self.assertEqual(resolved["running"], str((clips / "running.gif").resolve()))
            self.assertEqual(
                resolved["waiting"], str((clips / "missing-waiting.mp4").resolve())
            )
            self.assertEqual(resolved["review"], str((clips / "running.gif").resolve()))

    def test_player_module_and_print_config_require_no_pyserial(self) -> None:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "mac" / "codex_pet_player.py"),
                    "--state-dir",
                    str(Path(temporary) / "sessions"),
                    "--media-map",
                    str(ROOT / "examples" / "mac-player-media-map.json"),
                    "--print-config",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            config = json.loads(result.stdout)
            self.assertEqual(sorted(config["media_map"].keys()), sorted(pet_state.VALID_STATES))
            self.assertTrue(config["always_on_top"])
            self.assertTrue(config["state_file"].endswith("current_state.json"))

    @unittest.skipUnless(
        sys.platform == "darwin" and shutil.which("swiftc") is not None,
        "native Swift build requires macOS with swiftc",
    )
    def test_native_swift_player_compiles_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = pet_player.build_player_binary(
                cache_dir=Path(temporary), force_rebuild=True
            )
            self.assertTrue(binary.is_file())
            self.assertTrue(os.access(binary, os.X_OK))
            help_run = subprocess.run(
                [str(binary), "--help"], capture_output=True, text=True, check=True
            )
            self.assertIn("--media-map", help_run.stderr)
            self.assertIn("--force-state", help_run.stderr)


if __name__ == "__main__":
    unittest.main()
