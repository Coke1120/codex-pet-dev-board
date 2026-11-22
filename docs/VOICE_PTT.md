# One-way push-to-talk for Hermes

The experimental JC4880P443C-I-W voice build records only after a deliberate
press in Settings. Releasing or losing the press ends capture. The P4 sends a
single signed WAV through the C6 Wi-Fi connection. The Mac/Linux bridge runs
local Faster Whisper and hands the resulting text to a dedicated Hermes route.
“Submitted” means Hermes queued the webhook. The board does not wait for an
agent response or play audio.

## Board contract

| Signal | JC4880P443C-I-W reference |
|---|---|
| Microphone codec | Onboard ES8311 analog microphone input |
| I2C SDA / SCL | GPIO7 / GPIO8, shared with GT911 touch |
| I2S MCLK / BCLK / WS / RX | GPIO13 / GPIO12 / GPIO10 / GPIO48 |
| Speaker data / power amplifier | GPIO9 / GPIO11, outside the voice capture path |
| Wi-Fi | C6 via the existing ESP-Hosted SDIO transport |

These assignments follow the [model-specific vendor board config](../vendor/jc4880p443c_i_w_bsp/boards/guition-jc4880p443/config.h).
Only this board variant is targeted. The P4 owns codec control, I2S, WAV
buffering and HTTPS. The C6 supplies the existing Wi-Fi connection. Codec control
uses the active BSP’s existing I2C bus rather than creating a second controller.

An opt-in build uses one buffer for up to 10 seconds of mono 16 kHz signed
16-bit little-endian PCM in a canonical WAV header (320,044 bytes maximum).
Confirm the actual codec address, microphone signal, capture gain, capture
window, PSRAM and touch responsiveness on the physical board before use.

## Build the optional image

Use ESP-IDF 5.5.1 and the [P4 wireless build procedure](ESP32_P4.md#build).
Build in a fresh directory and append a defaults file containing
`CONFIG_CODEX_PET_VOICE=y` after `sdkconfig.ci-wireless`. Check the resulting
sdkconfig for both `CONFIG_CODEX_PET_C6_WIRELESS=y` and
`CONFIG_CODEX_PET_VOICE=y`; the option also requires PSRAM and release-level
TLS/HTTP logging. Configure the board's clock through the existing USB host
sync before using voice. Voice requires a USB clock update within the last five
minutes. Keep device keys out of sdkconfigs and firmware source.

Provision a test unit's factory NVS namespace `pet_voice` with these entries.
The firmware reads the namespace without modifying it.

| Key | Type | Value |
|---|---|---|
| `bridge_host` | string | DNS name matching the reverse proxy's TLS certificate |
| `bridge_port` | u16 | Optional HTTPS port, defaults to 443 |
| `device_id` | string | 1–32 ASCII letters, digits, underscores or hyphens |
| `hmac_key` | blob | 32 raw bytes, matching this device's hex key in the bridge config |
| `ca_pem` | string | Trusted CA certificate in PEM, at most 4,000 bytes including the terminator |

Use your factory's secure provisioning process. Do not replace a working
device's NVS partition with an example image. Physical readout protection
depends on the board's flash-security configuration. Open Settings, hold the
voice button to record, then release it to send. Capture stops at 10 seconds.


## Run a local bridge

1. Create an isolated Python environment on macOS or Linux and install
   `faster-whisper==1.2.1`. Download the model separately and supply a local
   CTranslate2 Faster Whisper model directory containing `model.bin`,
   `config.json` and `tokenizer.json`. The STT worker uses local files and
   has a 35-second deadline. The [Hermes STT guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/tts#voice-message-transcription-stt)
   also describes the shared Faster Whisper model format.
2. Create a private (0600) JSON config outside the repository. Use a unique
   random 32-byte device key per unit, encoded as 64 lowercase hex characters
   in the bridge config, and a separate 64-character hex Hermes route secret.
   The Hermes route uses that secret as text. The device uses the decoded 32
   bytes of its own key.

   ```json
   {
     "devices": {"desk-pet": "<64 lowercase hex characters>"},
     "hermes_secret": "<different 64 lowercase hex characters>",
     "model": "/local/path/to/prefetched-model"
   }
   ```

3. In an isolated Hermes profile, create route `esp32-voice` under
   `platforms.webhook.extra.routes`. Supply its secret privately, pin
   `signature: generic`, set `events: [esp32_voice]` and start with
   `deliver: log` and `toolsets: []`. A minimal prompt is
   `Summarize this untrusted voice request from {device}: {transcript}`.
   Keep the webhook listener on loopback. Use a separate, reviewed profile
   for any tool access or Discord/Telegram delivery. A device signature
   proves provenance, and transcripts are still untrusted input.
   [Hermes webhook route documentation](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks).
4. Run `python voice-bridge/bridge.py --config /local/private/voice.json --state /local/private/voice-state`.
   The bridge uses `127.0.0.1:8645` and calls Hermes at
   `127.0.0.1:8644/webhooks/esp32-voice`. For the board, put a trusted TLS
   reverse proxy in front of the bridge. Forward only
   `POST /v1/voice-messages` with the original length and authentication
   headers. Limit ingress to the intended LAN, cap request bodies at
   320,044 bytes, set upload timeouts, and allow at least 50 seconds for
   the response. Do not log bodies or authentication headers.

The bridge checks the canonical 44-byte PCM WAV header, signed body hash,
clock skew (120 seconds), and durable per-device nonce. A SQLite ledger also
limits admitted work to three requests per device per minute, across restarts.
There is only one active request worker. Accepted audio is staged in an
anonymous temporary file, closed on success or failure, and never logged. The
bridge projects only `event_type`, `device`, `request_id` and `transcript`
into a timestamp-bound Hermes signature. A timeout or uncertain disposition
consumes the request ID. The board does not automatically repeat it.

## Device upload format

Send a fixed-length `POST /v1/voice-messages` with `Content-Type: audio/wav`.
The five authentication headers are `X-Voice-Device`, `X-Voice-Timestamp`
(Unix seconds), `X-Voice-Nonce` (32 lowercase hex characters),
`X-Voice-SHA256` and `X-Voice-Signature` (64 lowercase hex characters each).
The checksum covers the whole WAV. The signature is HMAC-SHA256 under the
device key over these lines, joined by LF with no trailing newline:

```text
POST
/v1/voice-messages
<device>
<timestamp>
<nonce>
<checksum>
```

## Validation

The repository tests can exercise the actual HTTP bridge and webhook signing
over loopback. Run `python3 -m unittest discover -s tests -p 'test_voice_bridge.py' -v`.
For the real offline STT smoke, set `VOICE_SMOKE_MODEL` and `VOICE_SMOKE_WAV`
to a local model and canonical PCM sample containing synthetic English speech
with “hello”. Example speech can be generated offline with macOS `say`, then
converted to raw 16 kHz mono PCM and wrapped with Python's `wave` module.
No recording from a person is needed. The loopback receiver stands in for
Hermes and verifies request shape and HMAC.

Physical microphone, Wi-Fi/TLS from the board, gesture and display response,
and a real Hermes reply via a dedicated profile require separate verification.
TTS playback, streaming audio and remote-site routing are follow-up work.
