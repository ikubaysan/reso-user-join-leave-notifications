#!/usr/bin/env python3
"""
List all available pyttsx3 voices, showing name, ID, language, and gender.
Optionally play a sample for each voice.

Examples:
  # Just list voices
  ./list_voices.py

  # List and play sample
  ./list_voices.py --play

  # Use a custom text
  ./list_voices.py --play --text "Testing one two three."
"""

from __future__ import annotations
import argparse
import time
import pyttsx3


# ---------- Helpers ----------

def get_voice_gender(voice) -> str | None:
    """Return gender from voice metadata or inferred from id/name."""
    gender = getattr(voice, "gender", None)
    if gender:
        return str(gender)

    vid = getattr(voice, "id", "").lower()
    name = getattr(voice, "name", "").lower()

    # Heuristics for Linux (espeak) and naming hints
    if "+m" in vid or "male" in name:
        return "Male"
    if "+f" in vid or "female" in name:
        return "Female"

    return None


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

    # Gather metadata
    metadata = []
    for i, v in enumerate(voices):
        meta = {
            "index": i,
            "id": getattr(v, "id", ""),
            "name": getattr(v, "name", ""),
            "languages": getattr(v, "languages", []),
            "gender": get_voice_gender(v),
        }
        metadata.append(meta)

    try:
        probe.stop()
    except Exception:
        pass
    del probe

    # Display and optionally play
    for m in metadata:
        idx, vid, name, langs, gender = (
            m["index"],
            m["id"],
            m["name"],
            m["languages"],
            m["gender"] or "Unknown",
        )
        print(f"[{idx}] {name} ({vid})  Gender: {gender}  Langs: {langs}")
        if play:
            try:
                speak_with_voice(vid, sample_text)
            except Exception as e:
                print(f"  -> Skipped due to error: {e}")
            time.sleep(0.3)

    print("\nAll voices listed." if not play else "\nAll voices attempted.")


# ---------- CLI ----------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List or test all available pyttsx3 voices.")
    parser.add_argument("--play", action="store_true", help="Play each voice sample aloud.")
    parser.add_argument("--text", type=str, default="Hello! This is a test of my voice.",
                        help="Sample text to use when playing voices.")
    args = parser.parse_args()

    list_and_test_all_voices(args.text, play=args.play)
