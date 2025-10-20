#!/usr/bin/env python3
"""
Play or list all available pyttsx3 voices.

Examples:
  # Just list voices (no audio)
  ./list_voices.py

  # List and play sample for each voice
  ./list_voices.py --play
"""

from __future__ import annotations
import argparse
import time
import pyttsx3


def speak_with_voice(voice_id: str, text: str, rate_delta: int = 0, volume: float | None = None) -> None:
    """Init a fresh engine, set the voice, then speak."""
    engine = pyttsx3.init()
    try:
        if rate_delta:
            rate = engine.getProperty("rate") or 200
            engine.setProperty("rate", rate + rate_delta)
        if volume is not None:
            engine.setProperty("volume", max(0.0, min(1.0, volume)))

        engine.setProperty("voice", voice_id)
        engine.say(text)
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass
        del engine


def list_and_test_all_voices(sample_text: str = "Hello! This is my voice.", play: bool = False) -> None:
    """List all voices, and optionally play a sample."""
    probe = pyttsx3.init()
    voices = probe.getProperty("voices")
    print(f"Found {len(voices)} voices.\n")

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

    for m in metadata:
        idx, vid, name, langs = m["index"], m["id"], m["name"], m["languages"]
        print(f"[{idx}] {name} ({vid})  Langs: {langs}")
        if play:
            try:
                speak_with_voice(vid, sample_text)
            except Exception as e:
                print(f"  -> Skipped due to error: {e}")
            time.sleep(0.3)

    print("\nAll voices listed." if not play else "\nAll voices attempted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List or test all available pyttsx3 voices.")
    parser.add_argument("--play", action="store_true", help="Play each voice sample aloud.")
    parser.add_argument("--text", type=str, default="Hello! This is a test of my voice.",
                        help="Sample text to use when playing voices.")
    args = parser.parse_args()

    list_and_test_all_voices(args.text, play=args.play)
