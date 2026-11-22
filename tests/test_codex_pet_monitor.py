"""Isolated macOS host fixtures for the provider settings and lifecycle ingress."""

import http.client
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

MAC = Path(__file__).resolve().parents[1] / "mac"
sys.path.insert(0, str(MAC))

from codex_pet_monitor import read_monitor_selection, save_provider, settings_path


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "sessions"
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "CODEX_PET_STATE_DIR": str(self.state),
        }

    def python(self, script, *args, payload=None):
        return subprocess.run(
            [sys.executable, str(MAC / script), *args],
            input=payload, capture_output=True, text=True, timeout=10,
            env=self.env, check=True,
        )

    @contextmanager
    def process(self, script, *args):
        with subprocess.Popen(
            [sys.executable, str(MAC / script), *args],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            try:
                yield child
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

    def line(self, child):
        ready, _, _ = select.select([child.stdout], [], [], 10)
        self.assertTrue(ready, "subprocess did not print a line")
        line = child.stdout.readline().strip()
        self.assertTrue(line, child.stderr.read() if child.poll() is not None else "")
        return line

    def request(self, url, method="GET", body=None, headers=None, path=None):
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        try:
            connection.request(method, path or url.path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def choose(self, url, provider):
        return self.request(url, "POST", "provider=" + provider, {
            "Origin": f"http://{url.netloc}",
            "Content-Type": "application/x-www-form-urlencoded",
        })

    def hook(self, provider, event, session="fixture-session", **fields):
        payload = json.dumps({"hook_event_name": event, "session_id": session, **fields})
        if provider == "codex":
            return self.python("codex_pet_hook.py", payload=payload)
        return self.python("codex_pet_monitor.py", "hook", provider, payload=payload)

    def daemon_once(self):
        return self.python("codex_pet_daemon.py", "--dry-run", "--once")

    def test_settings_and_daemon_subprocess_smoke(self):
        self.hook("codex", "UserPromptSubmit")
        self.assertEqual(self.daemon_once().stdout.strip(), "running")
        with self.process("codex_pet_monitor.py", "settings") as settings:
            url = urlsplit(self.line(settings))
            self.assertEqual(url.hostname, "127.0.0.1")
            status, page = self.request(url)
            self.assertEqual(status, 200)
            self.assertEqual(page.count('type="radio"'), 4)
            self.assertIn('value="codex" checked', page)

            with self.process("codex_pet_daemon.py", "--dry-run", "--poll", "0.05") as daemon:
                self.assertEqual(self.line(daemon), "running")
                status, page = self.choose(url, "claude-code")
                self.assertEqual(status, 200)
                self.assertIn("No Claude Code hook event received", page)
                fallback = self.daemon_once()
                self.assertEqual(fallback.stdout.strip(), "running")
                self.assertIn("Using Codex", fallback.stderr)

                for event, expected in (
                    ("PermissionRequest", "waiting"),
                    ("PostToolUse", "running"),
                    ("Stop", "idle"),
                ):
                    result = self.hook("claude-code", event, prompt="private fixture text")
                    self.assertEqual((result.stdout, result.stderr), ("", ""))
                    self.assertEqual(self.line(daemon), expected)

                self.assertEqual(self.choose(url, "codex")[0], 200)
                self.assertEqual(self.line(daemon), "running")
                self.assertEqual(self.choose(url, "claude-code")[0], 200)
                self.assertEqual(self.line(daemon), "idle")

        with self.process("codex_pet_monitor.py", "settings") as restarted:
            new_url = urlsplit(self.line(restarted))
            self.assertNotEqual(new_url.path, url.path)
            self.assertIn('value="claude-code" checked', self.request(new_url)[1])
            self.assertEqual(self.daemon_once().stdout.strip(), "idle")

    def test_settings_starts_and_serves_without_hostname_lookup(self):
        probe = self.root / "resolver"
        probe.mkdir()
        (probe / "sitecustomize.py").write_text(
            "import socket\n"
            "def unexpected_lookup(*args, **kwargs):\n"
            "    raise AssertionError('unexpected hostname lookup')\n"
            "socket.getfqdn = unexpected_lookup\n",
            encoding="utf-8",
        )
        self.env["PYTHONPATH"] = str(probe)
        with self.process("codex_pet_monitor.py", "settings") as child:
            url = urlsplit(self.line(child))
            self.assertEqual(url.hostname, "127.0.0.1")
            self.assertEqual(self.request(url)[0], 200)

    def test_settings_rejects_invalid_http_without_saving(self):
        with self.process("codex_pet_monitor.py", "settings") as child:
            url = urlsplit(self.line(child))
            origin = f"http://{url.netloc}"
            form = {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"}
            cases = (
                ({**form, "Host": "localhost"}, "provider=hermes", None, 403),
                ({**form, "Origin": "http://example.invalid"}, "provider=hermes", None, 403),
                ({"Content-Type": form["Content-Type"]}, "provider=hermes", None, 403),
                (form, "provider=hermes", "/settings/wrong", 404),
                ({**form, "Content-Type": "text/plain"}, "provider=hermes", None, 415),
                (form, "provider=unknown", None, 400),
                (form, "provider=hermes&path=/tmp", None, 400),
                (form, "x" * 65, None, 400),
            )
            for headers, body, path, expected in cases:
                with self.subTest(expected=expected, body=body):
                    self.assertEqual(self.request(url, "POST", body, headers, path)[0], expected)
            self.assertFalse(settings_path(self.state).exists())
            self.assertEqual(self.choose(url, "hermes")[0], 200)
            self.assertEqual(settings_path(self.state).parent.parent, self.state)
            self.assertEqual(read_monitor_selection(self.root / "other-sessions").selected, "codex")
            self.assertEqual(stat.S_IMODE(settings_path(self.state).parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(settings_path(self.state).stat().st_mode), 0o600)

    def test_hermes_isolation_expiry_privacy_and_fallbacks(self):
        self.hook("codex", "PermissionRequest")
        save_provider(self.state, "hermes")
        self.python("codex_pet_monitor.py", "hook", "hermes", payload="{invalid")
        self.assertFalse((self.state / "hermes").exists())
        self.hook("hermes", "pre_llm_call", profile="first-profile", extra={"user_message": "fixture prompt"})
        self.hook("hermes", "on_session_end", profile="second-profile")
        self.hook("hermes", "unknown", profile="third-profile")
        source = self.state / "hermes"
        paths = list(source.glob("*.json"))
        self.assertEqual(len(paths), 2)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700)
        for path in paths:
            self.assertRegex(path.name, r"^[0-9a-f]{24}\.json$")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            content = path.read_text()
            self.assertEqual(set(json.loads(content)), {"version", "state", "event", "updated_at"})
            for private in ("first-profile", "second-profile", "fixture-session", "fixture prompt"):
                self.assertNotIn(private, content)
        self.assertEqual(self.daemon_once().stdout.strip(), "running")
        expired = read_monitor_selection(self.state, active_ttl=1, now=time.time() + 2)
        self.assertEqual((expired.active, expired.diagnostic), ([], None))
        self.assertEqual(len(list(source.glob("*.json"))), 2)
        read_monitor_selection(self.state, now=time.time() + 901)
        self.assertEqual(list(source.glob("*.json")), [])
        self.assertEqual(self.daemon_once().stdout.strip(), "idle")
        self.hook("hermes", "post_llm_call", profile="first-profile")
        self.assertEqual(self.daemon_once().stdout.strip(), "idle")

        paths[0].write_text("{invalid")
        corrupt = self.daemon_once()
        self.assertEqual(corrupt.stdout.strip(), "waiting")
        self.assertIn("Hermes Agent hook source is unreadable or invalid", corrupt.stderr)
        config = settings_path(self.state)
        config.write_text('{"version":1,"provider":"<unsupported>"}')
        unsupported = self.daemon_once()
        self.assertIn("unsupported version or provider", unsupported.stderr)
        self.assertNotIn("<unsupported>", unsupported.stderr)
        config.write_text("{invalid")
        self.assertIn("settings are unreadable or invalid", self.daemon_once().stderr)

        empty = self.root / "empty"
        save_provider(empty, "openclaw")
        result = self.python("codex_pet_daemon.py", "--state-dir", str(empty), "--dry-run", "--once")
        self.assertEqual(result.stdout.strip(), "idle")

    @unittest.skipUnless(shutil.which("node"), "Node.js is only needed for the OpenClaw fixture")
    def test_openclaw_plugin_uses_typed_run_id_and_never_reads_event(self):
        runtime = self.root / "fixture runtime"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin" / "python").symlink_to(sys.executable)
        for name in ("codex-pet-monitor.mjs", "codex_pet_monitor.py", "codex_pet_hook.py"):
            shutil.copyfile(MAC / name, runtime / name)
        fixture = """
            import plugin from './codex-pet-monitor.mjs';
            const hooks = new Map();
            plugin.register({on: (name, callback) => hooks.set(name, callback)});
            const event = new Proxy({}, {get() { throw Error('unexpected event access'); }});
            const begin = hooks.get('before_agent_run');
            const end = hooks.get('agent_end');
            if (begin(event, {runId: 'finished-run'}) !== undefined) throw Error('hook must not wait');
            end(event, {runId: 'finished-run'});
            begin(event, {runId: 'in-progress-run'});
            begin(event, {sessionId: 'ignore-without-run-id'});
        """
        subprocess.run(
            ["node", "--input-type=module", "-e", fixture],
            cwd=runtime, env=self.env, timeout=10, capture_output=True, check=True,
        )
        save_provider(self.state, "openclaw")
        selection = read_monitor_selection(self.state)
        self.assertIsNone(selection.diagnostic)
        self.assertCountEqual([state for state, _ in selection.active], ["idle", "running"])
        self.assertEqual(self.daemon_once().stdout.strip(), "running")
