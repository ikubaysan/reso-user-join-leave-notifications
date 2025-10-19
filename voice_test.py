#!/usr/bin/env python3
"""
Play a short sample with EVERY available pyttsx3 voice.
Re-initializes the engine per voice to ensure the change takes effect.
"""

from __future__ import annotations

import time
import pyttsx3


def speak_with_voice(voice_id: str, text: str, rate_delta: int = 0, volume: float | None = None) -> None:
    """Init a fresh engine, set the voice, then speak."""
    engine = pyttsx3.init()
    try:
        # Optional tuning (kept conservative)
        if rate_delta:
            rate = engine.getProperty("rate") or 200
            engine.setProperty("rate", rate + rate_delta)
        if volume is not None:
            engine.setProperty("volume", max(0.0, min(1.0, volume)))

        engine.setProperty("voice", voice_id)
        engine.say(text)
        engine.runAndWait()
    finally:
        # Ensure we release resources between voices
        try:
            engine.stop()
        except Exception:
            pass
        del engine


def list_and_test_all_voices(sample_text: str = "Hello! This is my voice.") -> None:
    # Use a temp engine just to enumerate voices
    probe = pyttsx3.init()
    voices = probe.getProperty("voices")
    print(f"Found {len(voices)} voices.\n")
    # Capture a light snapshot of metadata up front
    metadata = []
    for i, v in enumerate(voices):
        meta = {
            "index": i,
            "id": getattr(v, "id", ""),
            "name": getattr(v, "name", ""),
            "languages": getattr(v, "languages", []),
        }
        metadata.append(meta)
    try:
        probe.stop()
    except Exception:
        pass
    del probe

    # Speak with each voice (fresh engine each time)
    for m in metadata:
        idx, vid, name, langs = m["index"], m["id"], m["name"], m["languages"]
        print(f"[{idx}] {name} ({vid})  Langs: {langs}")
        try:
            speak_with_voice(vid, sample_text)
        except Exception as e:
            print(f"  -> Skipped due to error: {e}")
        time.sleep(0.3)  # small pause between voices

    print("\nAll voices attempted.")


if __name__ == "__main__":
    list_and_test_all_voices("Hello! This is a test of my voice.")
