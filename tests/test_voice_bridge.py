"""Real loopback ingress and webhook wire tests with synthetic inputs."""
from contextlib import contextmanager
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import wave

SPEC = importlib.util.spec_from_file_location(
    "pet_voice_bridge", Path(__file__).resolve().parents[1] / "voice-bridge" / "bridge.py"
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)
DEVICE = "synthetic-pet"
DEVICE_KEY = bytes(range(32))
ROUTE_KEY = b"example-test-route-secret"


def sample_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(bytes(1600))
    return output.getvalue()


@contextmanager
def running(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()
            if hasattr(server, "db"):
                server.db.close()


class VoiceBridgeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = Path(directory.name) / "voice.sqlite"

    def server(self, stt=lambda audio: "synthetic transcript", webhook=lambda *_: None):
        return bridge.serve(0, self.ledger, {DEVICE: DEVICE_KEY}, stt, webhook)

    def request(self, port, audio=None, nonce=1, timestamp=None, signed_audio=None, **overrides):
        audio = sample_wav() if audio is None else audio
        stamp = str(int(time.time()) if timestamp is None else timestamp)
        nonce = f"{nonce:032x}"
        checksum = hashlib.sha256(audio if signed_audio is None else signed_audio).hexdigest()
        signed = "\n".join(("POST", bridge.PATH, DEVICE, stamp, nonce, checksum)).encode()
        headers = {
            "Content-Type": "audio/wav", "X-Voice-Device": DEVICE, "X-Voice-Timestamp": stamp,
            "X-Voice-Nonce": nonce, "X-Voice-SHA256": checksum,
            "X-Voice-Signature": hmac.digest(DEVICE_KEY, signed, "sha256").hex(),
        }
        headers.update(overrides)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=45)
        try:
            conn.request("POST", bridge.PATH, audio, headers)
            result = conn.getresponse()
            return result.status, json.loads(result.read())["status"]
        finally:
            conn.close()

    def webhook_sink(self, events, status=202):
        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                events.append((self.path, self.headers, payload))
                self.send_response(status)
                self.end_headers()

        return HTTPServer(("127.0.0.1", 0), Sink)

    def assert_delivery(self, event, nonce):
        path, headers, raw = event
        payload = json.loads(raw)
        expected = hmac.digest(ROUTE_KEY, headers["X-Webhook-Timestamp"].encode() + b"." + raw, "sha256")
        self.assertEqual(headers["X-Webhook-Signature-V2"], expected.hex())
        self.assertEqual(headers["X-Request-ID"], f"{DEVICE}-{nonce:032x}")
        self.assertEqual(path, "/webhooks/esp32-voice")
        self.assertEqual(set(payload), {"event_type", "device", "request_id", "transcript"})
        self.assertEqual(payload["event_type"], "esp32_voice")
        self.assertEqual(payload["device"], DEVICE)
        self.assertEqual(payload["request_id"], headers["X-Request-ID"])
        return payload["transcript"]

    def test_hmac_handoff_and_replay_across_restart(self):
        events = []
        with running(self.webhook_sink(events)) as sink:
            def forward(*args):
                bridge.deliver(ROUTE_KEY, sink, *args)

            with running(self.server(webhook=forward)) as port:
                self.assertEqual(self.request(port), (202, "submitted"))
            with running(self.server(webhook=forward)) as port:
                self.assertEqual(self.request(port), (409, "replay"))
                for nonce in (2, 3):
                    self.assertEqual(self.request(port, nonce=nonce), (202, "submitted"))
                self.assertEqual(self.request(port, nonce=4), (429, "rate_limited"))
        self.assertEqual(len(events), 3)
        self.assertEqual(self.assert_delivery(events[0], 1), "synthetic transcript")

    def test_rejects_bad_auth_stale_clock_and_digest(self):
        with running(self.server(stt=lambda _: self.fail("rejected audio reached STT"))) as port:
            self.assertEqual(self.request(port, **{"X-Voice-Signature": "0" * 64})[0], 401)
            self.assertEqual(self.request(port, timestamp=int(time.time()) - bridge.CLOCK_SKEW - 1)[0], 401)
            self.assertEqual(self.request(port, sample_wav()[:-2] + b"\x01\x02", signed_audio=sample_wav())[0], 401)
            self.assertEqual(self.request(port, **{"X-Voice-Device": "unknown"})[0], 401)

    def test_rejects_oversize_invalid_pcm_and_ambiguous_framing(self):
        with running(self.server(stt=lambda _: self.fail("invalid audio reached STT"))) as port:
            self.assertEqual(self.request(port, b"abc", **{"Content-Length": str(bridge.MAX_BYTES + 1)})[0], 413)
            wrong_rate = bytearray(sample_wav())
            wrong_rate[24:28] = (8000).to_bytes(4, "little")
            self.assertEqual(self.request(port, wrong_rate), (415, "invalid_wav"))
            self.assertEqual(self.request(port, **{"Transfer-Encoding": "chunked"})[0], 400)

    def test_timeout_and_unavailable_hermes_consume_nonce(self):
        def timeout(_):
            raise subprocess.TimeoutExpired("synthetic-worker", 1)

        with running(self.server(stt=timeout)) as port:
            self.assertEqual(self.request(port), (504, "stt_timeout"))
            self.assertEqual(self.request(port), (409, "replay"))

        events = []
        # Hermes can return 200 for a filtered event, which must not count as submitted.
        with running(self.webhook_sink(events, status=200)) as sink:
            with running(self.server(webhook=lambda *args: bridge.deliver(ROUTE_KEY, sink, *args))) as port:
                self.assertEqual(self.request(port, nonce=2), (502, "hermes_unavailable"))
                self.assertEqual(self.request(port, nonce=2), (409, "replay"))
        self.assertEqual(len(events), 1)

    @unittest.skipUnless(os.getenv("VOICE_SMOKE_MODEL") and os.getenv("VOICE_SMOKE_WAV"), "offline model and synthetic WAV not supplied")
    def test_offline_faster_whisper_and_signed_loopback_smoke(self):
        model = Path(os.environ["VOICE_SMOKE_MODEL"])
        audio = Path(os.environ["VOICE_SMOKE_WAV"]).read_bytes()
        events = []
        with running(self.webhook_sink(events)) as sink:
            with running(self.server(stt=lambda data: bridge.transcribe(data, model),
                                     webhook=lambda *args: bridge.deliver(ROUTE_KEY, sink, *args))) as port:
                self.assertEqual(self.request(port, audio), (202, "submitted"))
        self.assertEqual(len(events), 1)
        self.assertIn("hello", self.assert_delivery(events[0], 1).lower())


if __name__ == "__main__":
    unittest.main()
