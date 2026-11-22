"""Loopback settings page for the macOS Codex Pet monitor."""

import re
import secrets
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer

from codex_pet_monitor import PROVIDERS, read_monitor_selection, save_provider

MAX_FORM_BYTES = 64
FALLBACK = "Using Codex lifecycle state, or idle if Codex has no active state."


def render_settings(state_dir, notice=""):
    selection = read_monitor_selection(state_dir)
    if selection.diagnostic:
        status = f"{selection.diagnostic} {FALLBACK}"
    elif selection.selected == "codex":
        status = "Using Codex lifecycle state."
    else:
        status = f"Receiving lifecycle state from {PROVIDERS[selection.selected]}."

    choices = "\n".join(
        f'<label><input type="radio" name="provider" value="{escape(provider)}"'
        f'{" checked" if provider == selection.selected else ""}> {escape(label)}</label>'
        for provider, label in PROVIDERS.items()
    )
    feedback = f'<p role="status">{escape(notice)}</p>' if notice else ""
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex Pet Settings</title>
<style>
  :root {{ color-scheme: light dark; font: 1rem/1.5 system-ui, sans-serif; }}
  body {{ max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }}
  fieldset {{ border: 1px solid GrayText; border-radius: .5rem; padding: .75rem 1rem; }}
  label {{ display: block; padding: .35rem 0; }}
  button {{ font: inherit; margin-top: 1rem; padding: .35rem .8rem; }}
</style>
<main>
  <h1>Codex Pet Settings</h1>
  <p>{escape(status)}</p>
  {feedback}
  <form method="post">
    <fieldset>
      <legend>Lifecycle provider</legend>
      {choices}
    </fieldset>
    <button type="submit">Save provider</button>
  </form>
</main>
</html>
"""


def serve_settings(state_dir):
    capability_path = "/settings/" + secrets.token_urlsafe(32)

    class SettingsHandler(BaseHTTPRequestHandler):
        timeout = 2

        def log_message(self, *_args):
            # Access logs would include the capability URL.
            pass

        def _respond(self, status, body=""):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # Keep the capability path out of Referer and preserve the form's Origin.
            self.send_header("Referrer-Policy", "strict-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def _matches(self, name, value):
            return self.headers.get_all(name) == [value]

        def _check_address(self):
            if not self._matches("Host", host):
                self._respond(403)
                return False
            if self.path != capability_path:
                self._respond(404)
                return False
            return True

        def do_GET(self):
            if self._check_address():
                self._respond(200, render_settings(state_dir))

        def do_POST(self):
            if not self._check_address():
                return
            if not self._matches("Origin", origin):
                self._respond(403)
                return
            if not self._matches("Content-Type", "application/x-www-form-urlencoded"):
                self._respond(415)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get_all("Transfer-Encoding")
                or len(lengths) != 1
                or not re.fullmatch(r"[1-9][0-9]?", lengths[0])
                or int(lengths[0]) > MAX_FORM_BYTES
            ):
                self._respond(400)
                return
            length = int(lengths[0])
            raw = self.rfile.read(length)
            for provider in PROVIDERS:
                if len(raw) == length and raw == f"provider={provider}".encode("ascii"):
                    break
            else:
                self._respond(400, "Choose one of the listed providers.")
                return
            try:
                save_provider(state_dir, provider)
            except (OSError, ValueError):
                self._respond(500, render_settings(state_dir, "Could not save provider settings."))
                return
            self._respond(200, render_settings(state_dir, "Provider saved."))

    with HTTPServer(("127.0.0.1", 0), SettingsHandler) as server:
        host = f"127.0.0.1:{server.server_port}"
        origin = "http://" + host
        print(origin + capability_path, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
