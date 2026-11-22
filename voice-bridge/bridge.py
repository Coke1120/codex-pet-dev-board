"""Single-flight, loopback-only ESP32 voice ingress for a dedicated Hermes route."""
import argparse
from contextlib import closing
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

PATH = "/v1/voice-messages"
DEVICE = r"[A-Za-z0-9_-]{1,32}"
MAX_BYTES = 44 + 16000 * 2 * 10
CLOCK_SKEW = 120
BODY_TIMEOUT = 8
STT_TIMEOUT = 35
WEBHOOK_TIMEOUT = 5
MAX_TRANSCRIPT = 2048
PCM_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


class Refused(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def sign(key, content):
    return hmac.new(key, content, hashlib.sha256).hexdigest()


def valid_wav(audio):
    if not 44 < len(audio) <= MAX_BYTES or len(audio) % 2:
        return False
    return PCM_HEADER.unpack_from(audio) == (
        b"RIFF", len(audio) - 8, b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000,
        2, 16, b"data", len(audio) - 44,
    )


def reserve(db, device, nonce, timestamp, now):
    # Commit before STT/webhook. Uncertain downstream writes never auto-retry.
    with db:
        db.execute("DELETE FROM voice_requests WHERE expires < ? AND received <= ?", (now, now - 60))
        if db.execute("SELECT 1 FROM voice_requests WHERE device=? AND nonce=?", (device, nonce)).fetchone():
            raise Refused(409, "replay")
        count = db.execute(
            "SELECT count(*) FROM voice_requests WHERE device=? AND received>?", (device, now - 60)
        ).fetchone()[0]
        if count >= 3:
            raise Refused(429, "rate_limited")
        db.execute("INSERT INTO voice_requests VALUES (?, ?, ?, ?)", (device, nonce, timestamp + CLOCK_SKEW, now))


def transcribe(audio, model):
    # Anonymous/unlinked storage. The isolated child gets only this descriptor.
    with tempfile.TemporaryFile() as recording:
        recording.write(audio)
        recording.seek(0)
        fd = recording.fileno()
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("voice_stt.py")), str(model), str(fd)],
            env={"HF_HUB_OFFLINE": "1", "OMP_NUM_THREADS": "2"}, pass_fds=(fd,),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=STT_TIMEOUT, check=True,
        )
    if len(result.stdout) > MAX_TRANSCRIPT * 6:
        raise ValueError("invalid STT result size")
    transcript = json.loads(result.stdout)
    if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > MAX_TRANSCRIPT:
        raise ValueError("invalid STT result")
    return transcript


def deliver(secret, port, device, nonce, transcript):
    request_id = f"{device}-{nonce}"
    payload = json.dumps({
        "event_type": "esp32_voice", "device": device, "request_id": request_id,
        "transcript": transcript,
    }, ensure_ascii=False, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request_id, "X-Webhook-Timestamp": stamp,
        "X-Webhook-Signature-V2": sign(secret, stamp.encode() + b"." + payload),
    }
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=WEBHOOK_TIMEOUT)) as pipe:
        pipe.request("POST", "/webhooks/esp32-voice", payload, headers)
        # Current Hermes returns 202 when queued. 200 can mean it ignored the event.
        if pipe.getresponse().status != 202:
            raise Refused(502, "hermes_unavailable")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(BODY_TIMEOUT)
        super().setup()

    def log_message(self, *_args):
        pass

    def send_error(self, code, *_args, **_kwargs):
        self.reply(code, "request_refused")

    def reply(self, status, code):
        body = json.dumps({"status": code}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def header(self, name, pattern, status=401):
        values = self.headers.get_all(name, [])
        if len(values) != 1 or re.fullmatch(pattern, values[0], flags=re.ASCII) is None:
            raise Refused(status, "request_refused")
        return values[0]

    def read_audio(self, size):
        audio = bytearray()
        deadline = time.monotonic() + BODY_TIMEOUT
        while len(audio) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.connection.settimeout(remaining)
            block = self.rfile.read1(min(16384, size - len(audio)))
            if not block:
                raise Refused(400, "incomplete_body")
            audio.extend(block)
        self.connection.settimeout(BODY_TIMEOUT)
        return audio

    def do_GET(self):
        self.reply(200, "ready") if self.path == "/health" else self.reply(404, "not_found")

    def accept(self):
        if self.path != PATH:
            raise Refused(404, "not_found")
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Expect"):
            raise Refused(400, "unsupported_framing")
        self.header("Content-Type", "audio/wav", 415)
        size = int(self.header("Content-Length", r"[1-9][0-9]{0,8}", 400))
        if size > MAX_BYTES:
            raise Refused(413, "too_large")

        device = self.header("X-Voice-Device", DEVICE)
        stamp = self.header("X-Voice-Timestamp", r"[1-9][0-9]{0,11}")
        nonce = self.header("X-Voice-Nonce", r"[a-f0-9]{32}")
        checksum = self.header("X-Voice-SHA256", r"[a-f0-9]{64}")
        signature = self.header("X-Voice-Signature", r"[a-f0-9]{64}")
        timestamp = int(stamp)
        key = self.server.devices.get(device)
        signed = "\n".join(("POST", PATH, device, stamp, nonce, checksum)).encode()
        if key is None or abs(int(time.time()) - timestamp) > CLOCK_SKEW or not hmac.compare_digest(sign(key, signed), signature):
            raise Refused(401, "unauthorized")
        audio = self.read_audio(size)
        if not hmac.compare_digest(hashlib.sha256(audio).hexdigest(), checksum):
            raise Refused(401, "unauthorized")
        if not valid_wav(audio):
            raise Refused(415, "invalid_wav")

        now = int(time.time())
        if abs(now - timestamp) > CLOCK_SKEW:
            raise Refused(401, "unauthorized")
        reserve(self.server.db, device, nonce, timestamp, now)
        try:
            transcript = self.server.stt(audio)
        except subprocess.TimeoutExpired:
            raise Refused(504, "stt_timeout") from None
        except (OSError, subprocess.CalledProcessError, ValueError):
            raise Refused(502, "stt_failed") from None
        try:
            self.server.webhook(device, nonce, transcript)
        except (OSError, http.client.HTTPException):
            raise Refused(502, "hermes_unavailable") from None

    def do_POST(self):
        status, code = 202, "submitted"
        try:
            self.accept()
        except Refused as failure:
            status, code = failure.status, failure.code
        except TimeoutError:
            status, code = 408, "body_timeout"
        except sqlite3.Error:
            status, code = 503, "storage_unavailable"
        try:
            self.reply(status, code)
        except OSError:
            pass


def serve(port, ledger, devices, stt, webhook):
    server = HTTPServer(("127.0.0.1", port), Handler)
    try:
        server.db = sqlite3.connect(ledger, check_same_thread=False)
        server.db.execute("""CREATE TABLE IF NOT EXISTS voice_requests (
            device TEXT, nonce TEXT, expires INTEGER, received INTEGER, PRIMARY KEY(device, nonce))""")
        server.devices, server.stt, server.webhook = devices, stt, webhook
        return server
    except Exception:
        if hasattr(server, "db"):
            server.db.close()
        server.server_close()
        raise


def load_config(file):
    if file.is_symlink() or not file.is_file() or file.stat().st_mode & 0o077:
        raise ValueError("use an owner-only regular config file")
    with file.open() as stream:
        config = json.load(stream)
    devices = config["devices"]
    if not isinstance(devices, dict) or not 1 <= len(devices) <= 32 or not all(
        re.fullmatch(DEVICE, device) and re.fullmatch(r"[a-f0-9]{64}", key)
        for device, key in devices.items()
    ):
        raise ValueError("invalid device credentials")
    secret = config["hermes_secret"]
    if not re.fullmatch(r"[a-f0-9]{64}", secret) or secret in devices.values() or len(set(devices.values())) != len(devices):
        raise ValueError("use distinct device and Hermes secrets")
    model = Path(config["model"]).resolve(strict=True)
    if not all((model / asset).is_file() for asset in ("model.bin", "config.json", "tokenizer.json")):
        raise ValueError("a preinstalled local Faster Whisper model is required")
    return {name: bytes.fromhex(key) for name, key in devices.items()}, secret.encode(), model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8645)
    parser.add_argument("--hermes-port", type=int, default=8644)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        devices, secret, model = load_config(args.config)
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.state.is_symlink() or args.state.stat().st_mode & 0o077 or not 1 <= args.hermes_port <= 65535:
            raise ValueError("invalid private state directory or Hermes port")
        with serve(args.port, args.state / "voice.sqlite", devices, lambda audio: transcribe(audio, model),
                   lambda device, nonce, transcript: deliver(secret, args.hermes_port, device, nonce, transcript)) as server:
            try:
                server.serve_forever()
            finally:
                server.db.close()
    except KeyboardInterrupt:
        return
    except (KeyError, ValueError, TypeError, OSError, sqlite3.Error):
        sys.exit("Voice bridge could not start. Check private config, model, state and ports.")


if __name__ == "__main__":
    main()
