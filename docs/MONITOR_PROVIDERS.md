# macOS lifecycle provider settings

Start the settings page with the installed Codex Pet runtime.

```sh
PET_RUNTIME="$HOME/Library/Application Support/CodexPet/runtime"
"$PET_RUNTIME/bin/python" "$PET_RUNTIME/codex_pet_monitor.py" settings
```

Open the printed `http://127.0.0.1:…/settings/…` URL in your browser. Choose
**Codex (default)**, **Hermes Agent**, **OpenClaw**, or **Claude Code**, then click
**Save provider**. Reload the page to see the current hook status. The server
binds to a temporary loopback port and issues a new private URL each time it
starts. Press Ctrl+C in the terminal to close it. The daemon applies the saved
selection on its next poll and keeps using it across restarts.

## Hook setup

Install or update the Codex Pet macOS runtime using the existing installer
before registering provider hooks. Merge the examples into your provider's
existing configuration and keep its other entries. Use the actual installed
runtime path if you changed the installer's `--runtime-dir`.

- **Codex.** Use your existing Codex lifecycle hook setup. Choosing Codex reads
  the original session state files.
- **Claude Code.** Append the [example hook groups](../examples/claude-code-monitor-hooks.json)
  to their event lists under `hooks` in your user `~/.claude/settings.json`.
  Review the commands in `/hooks` after restarting Claude Code and approve the
  applicable settings and hook trust prompts. Omit `StopFailure` if your Claude
  Code version does not support it. The hooks report running on prompt and tool
  activity, waiting on a permission request, and idle on Stop, SessionEnd, or
  StopFailure. [Claude Code hook reference](https://code.claude.com/docs/en/hooks)
- **Hermes Agent.** Merge the [example entries](../examples/hermes-monitor-hooks.yaml)
  into the active profile's `config.yaml`, appending to the event lists under
  `hooks`. Replace `/ABSOLUTE/CODEX_PET_RUNTIME` with the full runtime path.
  Keep the embedded double quotes so Hermes passes paths containing spaces as
  single arguments. Start a new Hermes session or restart the local gateway
  after changing its profile. Use `hermes hooks list` to check consent and
  approve each reviewed event/command pair through Hermes. The hooks report
  running before a turn and idle after completion or interrupted finalization.
  [Hermes shell hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#shell-hooks)
- **OpenClaw.** Merge the [example plugin entry](../examples/openclaw-monitor-plugin.json)
  into the local Gateway configuration. Append the absolute installed
  `codex-pet-monitor.mjs` path to `plugins.load.paths`. If you already use
  `plugins.allow`, include `codex-pet-monitor` in that list. Review and keep
  `plugins.entries.codex-pet-monitor.hooks.allowConversationAccess` enabled for
  the two typed hooks, then apply the configuration on the local Gateway. You
  can syntax-check the file with `node --check "$PET_RUNTIME/codex-pet-monitor.mjs"`.
  The hook callbacks ignore the event payload and pass only the typed
  `context.runId` and an allowlisted event to the adjacent runtime's Python over
  stdin. Runs without that ID are skipped.
  [OpenClaw plugin configuration](https://docs.openclaw.ai/gateway/config-extensions)
  [OpenClaw prompt and session hooks](https://docs.openclaw.ai/plugins/hooks/prompt-and-session)

## Readiness and storage

The selection is stored in
`~/Library/Application Support/CodexPet/monitor/provider.json`.
For a non-default state directory, the file is `monitor/provider.json` inside
that directory. Use the same directory for the settings page, daemon, and
hooks. The page and daemon accept `--state-dir`. All three read
`CODEX_PET_STATE_DIR` unless an explicit argument overrides it.

Each external provider writes to its own subdirectory under the Codex session
state root. Files contain the event name, mapped state, timestamp and format
version. The filename hashes the session or run ID. Hermes hashes the profile
and session together. The directories are mode `0700` and the JSON files are
written atomically at mode `0600`. Hook stdout is silent and hook input stays
out of the records and diagnostics.

The first accepted event creates an empty readiness marker, including when the
first event is terminal. Activity older than `--active-ttl` (900 seconds by
default) decays to idle, including stale activity after a crash. As the monitor
reads the selected provider, it removes expired records after at least 900
seconds and keeps the provider selected while idle.
The marker records past hook delivery. To reset readiness
when retiring or reconfiguring an integration, move its provider subdirectory
aside while its hooks are stopped.

If the selected source is missing or invalid, or the settings file is invalid
or unsupported, the daemon reads the existing Codex state. The result is idle
if Codex has no active state. The page reports the reason and the daemon emits
a diagnostic when it changes. Re-save a selection to recover invalid settings.
Quota, usage, and weather continue on their existing paths.

## Isolated verification

Run from the repository root with the macOS host test environment (Python 3.9+
and the existing `pyserial` dependency). Run the subprocess smoke first if you
want to verify the real settings endpoint and daemon entry point independently.

```sh
PYTHONPATH=tests python3 -m unittest -v test_codex_pet_monitor.MonitorTests.test_settings_and_daemon_subprocess_smoke
python3 -m unittest discover -s tests -p 'test_codex_pet_monitor.py' -v
```

The smoke posts to a loopback settings subprocess, checks a running dry-run
daemon, and starts fresh settings and `--dry-run --once` processes to check
persistence. The provider tests inject synthetic payloads at the documented
hook interfaces. The OpenClaw fixture uses Node if it is already available.
These checks use a temporary home and state root with no provider services,
Serial device, live accounts, or model turns. Live hook dispatch requires
separate testing with each provider.
