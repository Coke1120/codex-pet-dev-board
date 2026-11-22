# Mac-local Codex Pet Media Player

Run Codex Pet directly on macOS in a small floating always-on-top window without
flashing or connecting a GUITION JC4880P443C-I-W board.

This path reuses the same official Codex lifecycle hooks, four states
(`idle`, `running`, `waiting`, `review`), multi-session priority
(`waiting > review > running > idle`), and 15-minute stale-session TTL as the
ESP32-P4 Serial daemon. The USB Serial daemon remains independent and can run
separately or side-by-side.

## Architecture

```text
Codex lifecycle hooks
  ──▶ mac/codex_pet_hook.py
  ──▶ ~/Library/Application Support/CodexPet/sessions/*.json
  ──▶ mac/codex_pet_state.py (shared priority + TTL aggregator)
  ──▶ ~/Library/Application Support/CodexPet/runtime/current_state.json
  ──▶ mac/codex_pet_player.py ──▶ CodexPetPlayer (native Swift + AVFoundation)

ESP32-P4 USB Serial daemon (mac/codex_pet_daemon.py) stays independent.
```

### Shared host files

- `mac/codex_pet_state.py` — shared session reader, priority/TTL aggregator,
  atomic `current_state.json` writer, and `--print-state` / `--force-state` /
  `--watch` CLI
- `mac/codex_pet_daemon.py` — reuses `codex_pet_state.py` for aggregation while
  preserving all USB Serial, capability, clock, weather, usage, and quota behavior
- `mac/install.sh` — stages `codex_pet_state.py` alongside `codex_pet_daemon.py`
  for the optional ESP32-P4 LaunchAgent runtime

## Requirements

- macOS with Xcode Command Line Tools (`swiftc` — install via `xcode-select --install`)
- Python 3 (standard library only; `pyserial` is **not** required for Mac-local mode)
- No paid Apple Developer account or notarization required for personal local use
- Local media clips for your four states (private/custom artwork stays local and out of Git)

## Media format and performance budget

- **Primary format**: H.264 `.mp4` (also accepts `.m4v`, `.mov`) played through
  a single muted `AVPlayer` instance
- **Secondary format**: animated `.gif` via AppKit (`NSImageView`); switching
  between MP4 and GIF releases the previous decoder so at most **one** active
  decoder/presentation path runs at a time
- **Switching**: hard cut on state change (no crossfade)
- **State writes**: `current_state.json` is written atomically (`0700` dir /
  `0600` file for the default private runtime directory) only when state
  changes or every **5 seconds** (heartbeat)
- **Recommended clip specs**:
  - Window size around `240–360 px` wide (default `300×324`)
  - 12–24 FPS, 2–8 second seamless loops, no audio track
  - Total 4-state media library ≤ 40 MB

### Resource targets (v1)

| Metric | Target | Hard fail |
|---|---|---|
| Player steady CPU | ≤ 3% | > 8% for 60s |
| Aggregator CPU | ≤ 0.3% | > 1% for 60s |
| Player RSS | ≤ 120 MB | > 250 MB for 60s |
| Media switch p95 | ≤ 150–300 ms | > 500 ms on local SSD |
| Unchanged-state writes | heartbeat only (5s) | multi-Hz rewrites |

## Quickstart

### 1. Prepare a local 4-state media map

Copy [`examples/mac-player-media-map.json`](../examples/mac-player-media-map.json)
to a local directory (for example
`~/Library/Application Support/CodexPet/media-map.json`) and point each state
to your local `.mp4` or `.gif` files. Relative paths resolve against the map
file's directory:

```json
{
  "idle": "idle.mp4",
  "running": "running.mp4",
  "waiting": "waiting.mp4",
  "review": "review.mp4"
}
```

Alternatively, place `idle.mp4`, `running.mp4`, `waiting.mp4`, and `review.mp4`
(or `.gif`) in a folder and pass `--media-dir /path/to/clips`.

### 2. Launch the floating player

```bash
python3 mac/codex_pet_player.py --media-map /path/to/media-map.json
```

Or with a directory of clips:

```bash
python3 mac/codex_pet_player.py --media-dir /path/to/clips
```

On first launch, `codex_pet_player.py` compiles `mac/player/CodexPetPlayer.swift`
with `swiftc` into `~/Library/Caches/CodexPet/bin/` (cached by source SHA-256),
starts the lightweight session aggregator, and opens the floating always-on-top
window.

Useful flags:

- `--force-state {idle,running,waiting,review}` — open directly in a manual state
- `--idle PATH` / `--running PATH` / `--waiting PATH` / `--review PATH` — override individual clips
- `--width 300 --height 324` — set window dimensions
- `--no-always-on-top` — use normal window level instead of floating
- `--no-aggregator` — watch an existing `current_state.json` without starting the local aggregator thread
- `--print-config` — inspect resolved paths and mapped clips without launching GUI
- `--build-only` — compile and print the cached native player binary path
- `--emit-events` — print JSONL state/media transition events to stdout

## Missing-media fallback behavior

When a state has no mapped clip, its file is missing, or decoding fails, the
player shows `· fallback` and loops the last valid clip if it can still play it.
Otherwise, the window shows a native placeholder with the current state and
its color indicator.

## Manual state switching and offline testing

You can test clip playback and transitions without running a live Codex turn:

1. **In-window controls**: click `Idle`, `Run`, `Wait`, or `Rev` in the bottom
   strip (or press keys `1`, `2`, `3`, `4` when the window is focused). Click
   `Auto` (or press `0` / `A`) to return to live Codex hooks. Press `R` to
   retry the current clip after restoring or replacing its file. Restart the
   player after editing the media map.
2. **Startup override**:

```bash
python3 mac/codex_pet_player.py \
  --media-map /path/to/media-map.json \
  --force-state waiting
```

3. **CLI state inspection and manual sink injection**:

```bash
# Inspect current aggregate state from session files
python3 mac/codex_pet_state.py --print-state

# Inject a manual state while the player is running in Auto with --no-aggregator
python3 mac/codex_pet_state.py --force-state review
```

## Connecting live Codex hooks

The Mac-local player reads the exact same hook session directory as the ESP32-P4
daemon (`~/Library/Application Support/CodexPet/sessions/`, overridable via
`CODEX_PET_STATE_DIR` or `--state-dir`).

Follow step 4 of [`docs/CODEX_DESKTOP.md`](CODEX_DESKTOP.md#4-configure-codex-hooks)
to merge `mac/codex_pet_hook.py` into `~/.codex/hooks.json` using
`tools/install_codex_hooks.py` and review/trust the hooks in Codex `/hooks`.

## Optional LaunchAgent notes

The ESP32-P4 Serial LaunchAgent (`mac/install.sh`) remains opt-in for hardware
owners and does **not** auto-start the Mac-local player. Mac-local startup stays
user-initiated by default until you configure your own media map; it never
requires an attached board.

## Privacy and v1 non-goals

- `current_state.json` contains only `version`, `state`, `source`,
  `active_sessions`, and `updated_at`. New runtime directories and state files use
  modes `0700` and `0600`, respectively. The player reads the state file and
  selected media paths, while the aggregator reads only lifecycle records.
- Not included in v1: App Store / notarized `.dmg` packaging, Electron/Chromium
  runtime, multi-decoder preloading, or Mac reimplementation of board weather,
  quota, or wireless Settings pages.
