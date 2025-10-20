#!/usr/bin/env python3
"""
Flask TTS API (OGG output, cached by username+action) with CLI flags.

GET /api/tts?username=<str>&action=<join|leave>[&base_url=<http(s)://host:port>]
→ {"url": "<absolute URL to .ogg file>"}

Base URL precedence:
  1) Query param base_url (per-request override)
  2) CLI flag --external-base-url
  3) Default: Flask builds URL from the incoming request

CLI:
  python app.py --host 0.0.0.0 --port 4684 --external-base-url http://gallery.ikubaysan.com:4648 --tts-voice "zira"
"""

from __future__ import annotations
import os, re, threading, argparse
from typing import Final, Literal, Optional, Tuple
from urllib.parse import urljoin
from flask import Flask, jsonify, request, url_for, abort
import pyttsx3
from pydub import AudioSegment

Action = Literal["join", "leave"]


# ---------- CLI PARSER ----------

def parse_args() ->  Tuple[str, int, str, str]:
    parser = argparse.ArgumentParser(description="Start the TTS Flask server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4684, help="Port to bind (default: 4684)")
    parser.add_argument("--tts-voice", dest="tts_voice", default="",
                        help="Preferred TTS voice (substring or exact id), case-insensitive. Examples: 'zira', 'english-us', 'fiona'")
    parser.add_argument(
        "--external-base-url",
        default="",
        help="Optional absolute base URL for returned file links "
             "(e.g., http://gallery.ikubaysan.com:4648).",
    )
    args = parser.parse_args()
    return args.host, args.port, args.external_base_url.strip(), args.tts_voice.strip()


# ---------- UTILITIES ----------

def ensure_dir(path: str) -> None:
    """Create a directory if it doesn't exist (no error if it already exists)."""
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def ensure_parent_dir(path: str) -> None:
    """Ensure the parent directory of a file path exists."""
    parent = os.path.dirname(path)
    if parent:
        ensure_dir(parent)

def project_paths() -> Tuple[str, str]:
    root = os.path.dirname(os.path.abspath(__file__))
    audio_dir = os.path.join(root, "static", "audio")
    ensure_dir(audio_dir)  # make sure it exists at startup
    return root, audio_dir


def sanitize_username(username: str, max_len: int = 64) -> str:
    s = username.strip().lower()
    s = re.sub(r"[^a-z0-9_\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:max_len] or "user"


def build_phrase(username: str, action: Action) -> str:
    return f"{username} has joined the session." if action == "join" else f"{username} has left the session."


def is_valid_base_url(value: str) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def build_file_url(app: Flask, rel_static_path: str, base_url_override: Optional[str]) -> str:
    """Build an absolute URL to /static/<rel_static_path>."""
    static_path = f"/static/{rel_static_path.lstrip('/')}"
    if base_url_override and is_valid_base_url(base_url_override):
        return urljoin(base_url_override.rstrip('/') + '/', static_path.lstrip('/'))
    return url_for("static", filename=rel_static_path.replace("\\", "/"), _external=True)


# ---------- CORE CLASSES ----------

class AudioSpec:
    def __init__(self, username_raw: str, action: Action, audio_dir: str):
        if action not in ("join", "leave"):
            raise ValueError("action must be 'join' or 'leave'")
        self.username_raw = username_raw
        self.action = action
        self.audio_dir = audio_dir

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
    """Thread-safe pyttsx3 wrapper that prefers Zira voice on Windows."""

    def __init__(self, prefer_voice_substr: str = "zira", rate_delta: int = -20) -> None:
        self._engine = pyttsx3.init()
        self._lock = threading.Lock()

        try:
            voices = self._engine.getProperty("voices") or []
            chosen_id = None
            needle = prefer_voice_substr.lower()
            for v in voices:
                if needle in (v.name or "").lower() or needle in (v.id or "").lower():
                    chosen_id = v.id
                    break
            if chosen_id:
                self._engine.setProperty("voice", chosen_id)
                print(f"[TTS] Using voice: {chosen_id}")
            else:
                print(f"[TTS] Preferred voice '{prefer_voice_substr}' not found; using default.")
        except Exception as e:
            print(f"[TTS] Voice selection failed: {e}")

        try:
            rate = self._engine.getProperty("rate")
            self._engine.setProperty("rate", rate + rate_delta)
            print(f"[TTS] Set speech rate to {self._engine.getProperty('rate')}")
        except Exception as e:
            print(f"[TTS] Failed to set rate: {e}")

    def synthesize_to_wav(self, text: str, wav_path: str) -> None:
        ensure_parent_dir(wav_path)  # ensure folder exists (handles folder deletion mid-run)
        with self._lock:
            self._engine.save_to_file(text, wav_path)
            self._engine.runAndWait()

    def wav_to_ogg(self, wav_path: str, ogg_path: str) -> None:
        ensure_parent_dir(ogg_path)  # ensure folder exists
        audio = AudioSegment.from_wav(wav_path)
        audio.export(ogg_path, format="ogg")

    def generate_ogg(self, spec: AudioSpec) -> str:
        # Recreate audio dir if it was removed during runtime
        ensure_dir(spec.audio_dir)

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
    def __init__(self, audio_dir: str, tts: Optional[TTSGenerator] = None) -> None:
        self.audio_dir: Final[str] = audio_dir
        self.tts: Final[TTSGenerator] = tts or TTSGenerator()

    def get_or_create_audio(self, username: str, action: str) -> str:
        # Ensure audio folder still exists at request time
        ensure_dir(self.audio_dir)

        act = action.strip().lower()
        if act not in ("join", "leave"):
            raise ValueError("Parameter 'action' must be 'join' or 'leave'.")
        spec = AudioSpec(username_raw=username, action=act, audio_dir=self.audio_dir)
        if os.path.exists(spec.ogg_path):
            return spec.ogg_path
        return self.tts.generate_ogg(spec)


# ---------- FLASK FACTORY ----------


def create_app(external_base_url: str = "", tts_voice: str = "") -> Flask:
    root, audio_dir = project_paths()
    app = Flask(__name__, static_url_path="/static", static_folder=os.path.join(root, "static"))
    app.config["EXTERNAL_BASE_URL"] = external_base_url
    service = AudioService(audio_dir=audio_dir, tts=TTSGenerator(prefer_voice_substr=(tts_voice or "zira")))

    @app.get("/api/tts")
    def tts_endpoint():
        username = request.args.get("username", type=str)
        action = request.args.get("action", type=str)
        per_request_base = (request.args.get("base_url", type=str) or "").strip()

        if not username or not action:
            return abort(400, description="Missing 'username' or 'action'.")

        try:
            ogg_path = service.get_or_create_audio(username=username, action=action)
        except ValueError as e:
            return abort(400, description=str(e))
        except Exception as e:
            return abort(500, description=f"TTS generation failed: {e}")

        static_folder = os.path.abspath(app.static_folder)
        rel_path = os.path.relpath(ogg_path, static_folder).replace("\\", "/")
        base_override = per_request_base or app.config.get("EXTERNAL_BASE_URL") or None
        file_url = build_file_url(app, rel_path, base_override)
        return jsonify({"url": file_url}), 200

    @app.get("/")
    def health():
        # Also verify the audio directory exists on health check
        try:
            ensure_dir(os.path.join(app.static_folder, "audio"))
        except Exception:
            pass
        return jsonify({"ok": True}), 200

    return app


# ---------- ENTRYPOINT ----------
if __name__ == "__main__":
    host, port, external_base_url, tts_voice = parse_args()
    app = create_app(external_base_url, tts_voice)
    app.run(host=host, port=port, debug=False)
