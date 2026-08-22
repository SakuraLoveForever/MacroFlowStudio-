from __future__ import annotations

import threading
import io
import math
import struct
import wave
import winsound


PATTERNS = {
    "record_start": [(880, 90), (1175, 130)],
    "record_stop": [(1175, 90), (784, 150)],
    "run_start": [(988, 80)],
    "run_done": [(784, 80), (988, 80), (1319, 130)],
    "emergency_stop": [(660, 100), (523, 100), (392, 180)],
    "error": [(330, 130), (330, 180)],
}

_WAV_CACHE: dict[str, bytes] = {}
_CACHE_LOCK = threading.Lock()


def _render_alert(name: str) -> bytes | None:
    pattern = PATTERNS.get(name)
    if not pattern:
        return None
    sample_rate = 44100
    frames: list[bytes] = []
    for index, (frequency, duration) in enumerate(pattern):
        count = max(1, int(sample_rate * duration / 1000))
        fade = max(1, int(sample_rate * 0.006))
        samples = bytearray()
        for sample_index in range(count):
            envelope = min(1.0, sample_index / fade, (count - sample_index) / fade)
            value = int(10500 * envelope * math.sin(2 * math.pi * frequency * sample_index / sample_rate))
            samples.extend(struct.pack("<h", value))
        frames.append(bytes(samples))
        if index < len(pattern) - 1:
            frames.append(b"\x00\x00" * int(sample_rate * 0.018))
    wav_data = io.BytesIO()
    with wave.open(wav_data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))
    return wav_data.getvalue()


def _cached_alert(name: str) -> bytes | None:
    with _CACHE_LOCK:
        cached = _WAV_CACHE.get(name)
    if cached is not None:
        return cached
    rendered = _render_alert(name)
    if rendered is not None:
        with _CACHE_LOCK:
            _WAV_CACHE[name] = rendered
    return rendered


def prewarm_alert(name: str) -> None:
    """Render an alert before a hotkey needs it, without playing audio."""
    threading.Thread(target=lambda: _cached_alert(name), name="MacroFlowAlertWarmup", daemon=True).start()


def play_alert(name: str, enabled: bool = True) -> None:
    if not enabled:
        return
    pattern = PATTERNS.get(name)
    if not pattern:
        return

    def worker():
        try:
            wav_data = _cached_alert(name)
            if wav_data is not None:
                winsound.PlaySound(wav_data, winsound.SND_MEMORY)
        except Exception:
            try:
                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass

    threading.Thread(target=worker, name="MacroFlowAlert", daemon=True).start()
