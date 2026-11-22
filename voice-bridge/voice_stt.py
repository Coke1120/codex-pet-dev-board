"""Bounded child worker. No credentials or network model downloads."""
import os
import sys
import wave

if __name__ == "__main__":
    import json
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
    # Open the inherited descriptor directly: an unlinked input still works on macOS.
    with os.fdopen(int(sys.argv[2]), "rb") as audio, wave.open(audio, "rb") as pcm:
        frames = pcm.readframes(160000)
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
    segments, _ = model.transcribe(samples, beam_size=1)
    parts, remaining = [], 2048
    for segment in segments:
        text = segment.text[:remaining]
        parts.append(text)
        remaining -= len(text)
        if not remaining:
            break
    print(json.dumps("".join(parts).strip()))
