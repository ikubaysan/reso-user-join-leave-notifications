#!/usr/bin/env python3
"""
Flask TTS API (OGG output, cached by username+action).

GET /api/tts?username=<str>&action=<join|leave>[&base_url=<http(s)://host:port>]
→ {"url": "<absolute URL to .ogg file>"}

Base URL precedence:
  1) query param base_url (per-request override)
  2) env var EXTERNAL_BASE_URL
  3) default: Flask builds URL from the incoming request (local IP/host)

Dependencies:
  - Flask
  - pyttsx3
  - pydub   (requires ffmpeg or avconv on system PATH)
"""

from __future__ import annotations
import os, re, threading
from dataclasses import dataclass
from typing import Final, Literal, Optional, Tuple, Dict
from urllib.parse import urljoin

from flask import Flask, jsonify, request, url_for, abort
import pyttsx3
from pydub import AudioSegment


Action = Literal["join", "leave"]


# ---------- Utilities ----------

def project_paths() -> Tuple[str, str]:
    root = os.path.dirname(os.path.abspath(__file__))
    audio_dir = os.path.join(root, "static", "audio")
    os.makedirs(audio_dir, exist_ok=True)
    return root, audio_dir


def sanitize_username(username: str, max_len: int = 64) -> str:
    s = username.strip().lower()
    s = re.sub(r"[^a-z0-9_\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:max_len] or "user"


def build_phrase(username: str, action: Action) -> str:
    return f"{username} has joined the session." if action == "join" else f"{username} has left the session."


def is_valid_base_url(value: str) -> bool:
    """Very light validation to avoid bad inputs in base_url."""
    if not value:
        return False
    v = value.strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def build_file_url(app: Flask, rel_static_path: str, base_url_override: Optional[str]) -> str:
    """
    Build an absolute URL to a static file.
    - rel_static_path: 'audio/leave_alice.ogg'
    - If base_url_override is provided (and valid), use it; otherwise use Flask's url_for(_external=True).
    """
    # path we want under /static
    static_path = f"/static/{rel_static_path.lstrip('/')}"
    if base_url_override and is_valid_base_url(base_url_override):
        # Ensure exactly one slash join
        return urljoin(base_url_override.rstrip('/') + '/', static_path.lstrip('/'))
    # Fallback: use Flask's host/scheme from the request
    return url_for("static", filename=rel_static_path.replace("\\", "/"), _external=True)


# ---------- Core Classes ----------

@dataclass(frozen=True)
class AudioSpec:
    username_raw: str
    action: Action
    audio_dir: str

    def __post_init__(self) -> None:
        if self.action not in ("join", "leave"):
            raise ValueError("action must be 'join' or 'leave'")

    @property
    def username_safe(self) -> str:
        return sanitize_username(self.username_raw)

    @property
    def ogg_path(self) -> str:
        return os.path.join(self.audio_dir, f"{self.action}_{self.username_safe}.ogg")

    @property
    def tmp_wav_path(self) -> str:
        return os.path.join(self.audio_dir, f".tmp_{self.action}_{self.username_safe}.wav")

    @property
    def phrase(self) -> str:
        return build_phrase(self.username_raw, self.action)


class TTSGenerator:
    """Thread-safe pyttsx3 wrapper that prefers the Zira voice on Windows."""

    def __init__(self, prefer_voice_substr: str = "zira", rate_delta: int = -20) -> None:
        self._engine = pyttsx3.init()
        self._lock = threading.Lock()

        # Try selecting preferred voice
        try:
            voices = self._engine.getProperty("voices") or []
            chosen_id = None
            needle = prefer_voice_substr.lower() if prefer_voice_substr else ""
            for v in voices:
                name = getattr(v, "name", "") or ""
                vid  = getattr(v, "id", "") or ""
                if needle and (needle in name.lower() or needle in vid.lower()):
                    chosen_id = v.id
                    break
            if chosen_id:
                self._engine.setProperty("voice", chosen_id)
                print(f"[TTS] Using voice: {chosen_id}")
            else:
                print(f"[TTS] Preferred voice '{prefer_voice_substr}' not found; using default.")
        except Exception as e:
            print(f"[TTS] Voice selection failed: {e}")

        # Optionally slow down a bit
        try:
            current_rate = self._engine.getProperty("rate")
            self._engine.setProperty("rate", current_rate + rate_delta)
            print(f"[TTS] Set speech rate to {self._engine.getProperty('rate')}")
        except Exception as e:
            print(f"[TTS] Failed to set rate: {e}")

    def synthesize_to_wav(self, text: str, wav_path: str) -> None:
        with self._lock:
            self._engine.save_to_file(text, wav_path)
            self._engine.runAndWait()

    def wav_to_ogg(self, wav_path: str, ogg_path: str) -> None:
        audio = AudioSegment.from_wav(wav_path)
        audio.export(ogg_path, format="ogg")

    def generate_ogg(self, spec: AudioSpec) -> str:
        if os.path.exists(spec.ogg_path):
            return spec.ogg_path
        self.synthesize_to_wav(spec.phrase, spec.tmp_wav_path)
        try:
            self.wav_to_ogg(spec.tmp_wav_path, spec.ogg_path)
        finally:
            if os.path.exists(spec.tmp_wav_path):
                try:
                    os.remove(spec.tmp_wav_path)
                except Exception:
                    pass
        return spec.ogg_path


class AudioService:
    """Handles input validation, caching, and URL building."""

    def __init__(self, audio_dir: str, tts: Optional[TTSGenerator] = None) -> None:
        self.audio_dir: Final[str] = audio_dir
        self.tts: Final[TTSGenerator] = tts or TTSGenerator()

    def get_or_create_audio(self, username: str, action: str) -> str:
        act = self._validate_action(action)
        spec = AudioSpec(username_raw=username, action=act, audio_dir=self.audio_dir)
        if os.path.exists(spec.ogg_path):
            return spec.ogg_path
        return self.tts.generate_ogg(spec)

    @staticmethod
    def _validate_action(action: str) -> Action:
        a = action.strip().lower()
        if a not in ("join", "leave"):
            raise ValueError("Parameter 'action' must be 'join' or 'leave'.")
        return a  # type: ignore[return-value]


# ---------- Flask Factory ----------

def create_app() -> Flask:
    root, audio_dir = project_paths()
    app = Flask(__name__, static_url_path="/static", static_folder=os.path.join(root, "static"))

    # Optional global override via env var (e.g., "http://gallery.ikubaysan.com:4648")
    app.config["EXTERNAL_BASE_URL"] = os.getenv("EXTERNAL_BASE_URL", "").strip()

    service = AudioService(audio_dir=audio_dir, tts=TTSGenerator(prefer_voice_substr="zira"))

    @app.get("/api/tts")
    def tts_endpoint():
        username = request.args.get("username", type=str)
        action = request.args.get("action", type=str)

        # Optional per-request override of base URL
        per_request_base = request.args.get("base_url", type=str)
        if per_request_base:
            per_request_base = per_request_base.strip()

        if not username or not action:
            return abort(400, description="Missing 'username' or 'action'.")

        try:
            ogg_path = service.get_or_create_audio(username=username, action=action)
        except ValueError as e:
            return abort(400, description=str(e))
        except Exception as e:
            return abort(500, description=f"TTS generation failed: {e}")

        # Build relative path under /static and then the absolute URL with the chosen base
        static_folder = os.path.abspath(app.static_folder)
        rel_path = os.path.relpath(ogg_path, static_folder).replace("\\", "/")  # e.g., "audio/leave_alice.ogg"

        # Choose which base to use
        base_override = per_request_base or app.config.get("EXTERNAL_BASE_URL") or None
        file_url = build_file_url(app, rel_path, base_override)

        return jsonify({"url": file_url}), 200

    @app.get("/")
    def health():
        return jsonify({"ok": True}), 200

    return app


# ---------- Entrypoint ----------

if __name__ == "__main__":
    app = create_app()
    # Bind to all interfaces so port-forwarding or reverse-proxy can reach it
    app.run(host="0.0.0.0", port=4684, debug=False)
