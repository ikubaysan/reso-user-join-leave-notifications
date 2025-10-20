#!/usr/bin/env python3
"""
Flask TTS API (OGG output, ALWAYS new file per request, UUID filenames).

GET /api/tts?username=<str>&action=<join|leave>[&base_url=<http(s)://host:port>]
→ {"url": "<absolute URL to .ogg file>", "filename": "<uuid>_<username>_<action>.ogg"}

GET /api/voices
→ JSON list of voices available to pyttsx3 (index, id, name, languages if available)

Filename format (no caching):
  <uuid>_<username>_<action>.ogg

Base URL precedence:
  1) Query param base_url (per-request override)
  2) CLI flag --external-base-url
  3) Default: Flask builds URL from the incoming request

CLI examples:
  python main.py --host 0.0.0.0 --port 4684 \
    --external-base-url http://gallery.ikubaysan.com:4648 \
    --tts-voice-index 28

  python main.py --tts-voice "us2"   # try MBROLA US2 (female-ish) via substring on Linux
"""

from __future__ import annotations
import os, re, threading, argparse, json, uuid
from typing import Final, Literal, Optional, Tuple, List, Any
from urllib.parse import urljoin
from flask import Flask, jsonify, request, url_for, abort
import pyttsx3
from pydub import AudioSegment

Action = Literal["join", "leave"]


# ---------- CLI PARSER ----------

def parse_args() -> Tuple[str, int, str, str, Optional[int]]:
    parser = argparse.ArgumentParser(description="Start the TTS Flask server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4684, help="Port to bind (default: 4684)")
    parser.add_argument(
        "--external-base-url",
        default="",
        help="Optional absolute base URL for returned file links "
             "(e.g., http://gallery.ikubaysan.com:4648).",
    )
    parser.add_argument(
        "--tts-voice",
        dest="tts_voice",
        default="",
        help="Preferred TTS voice (substring or exact id/name), case-insensitive. "
             "Examples on Linux: 'us2' (MBROLA female-ish), 'en-us', etc."
    )
    parser.add_argument(
        "--tts-voice-index",
        dest="tts_voice_index",
        type=int,
        default=None,
        help="Pick a voice by numeric index (as listed by /api/voices). Deterministic on Linux."
    )
    args = parser.parse_args()
    return (
        args.host,
        args.port,
        args.external_base_url.strip(),
        args.tts_voice.strip(),
        args.tts_voice_index,
    )


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
        self._uuid = str(uuid.uuid4())

    @property
    def username_safe(self) -> str:
        return sanitize_username(self.username_raw)

    @property
    def filename(self) -> str:
        # <uuid>_<username>_<action>.ogg
        return f"{self._uuid}_{self.username_safe}_{self.action}.ogg"

    @property
    def ogg_path(self) -> str:
        return os.path.join(self.audio_dir, self.filename)

    @property
    def tmp_wav_path(self) -> str:
        return os.path.join(self.audio_dir, f".tmp_{self._uuid}.wav")

    @property
    def phrase(self) -> str:
        return build_phrase(self.username_raw, self.action)


class TTSGenerator:
    """Thread-safe pyttsx3 wrapper.

    Selection order:
      1) voice_index (exact index)
      2) prefer_voice_substr (substring match in id or name, case-insensitive)
      3) leave default
    """

    def __init__(
        self,
        prefer_voice_substr: str = "",
        voice_index: Optional[int] = None,
        rate_delta: int = -20,
    ) -> None:
        self._engine = pyttsx3.init()
        self._lock = threading.Lock()

        try:
            voices: List[Any] = self._engine.getProperty("voices") or []
            chosen_id = None

            # 1) Pick by index if provided and valid
            if voice_index is not None:
                if 0 <= voice_index < len(voices):
                    chosen_id = getattr(voices[voice_index], "id", None)
                    print(f"[TTS] Using voice by index {voice_index}: {chosen_id}")
                else:
                    print(f"[TTS] Voice index {voice_index} out of range (0..{len(voices)-1}). Ignoring.")

            # 2) Otherwise pick by substring (id or name)
            if chosen_id is None and prefer_voice_substr:
                needle = prefer_voice_substr.lower()
                for v in voices:
                    vid = (getattr(v, "id", "") or "").lower()
                    vname = (getattr(v, "name", "") or "").lower()
                    if needle in vid or needle in vname:
                        chosen_id = v.id
                        print(f"[TTS] Using voice by substring '{prefer_voice_substr}': {chosen_id}")
                        break

            # 3) Apply if we found something
            if chosen_id:
                self._engine.setProperty("voice", chosen_id)
            else:
                if prefer_voice_substr or (voice_index is not None):
                    print("[TTS] Preferred voice not found; using default voice.")

        except Exception as e:
            print(f"[TTS] Voice selection failed: {e}")

        try:
            rate = self._engine.getProperty("rate")
            self._engine.setProperty("rate", rate + rate_delta)
            print(f"[TTS] Set speech rate to {self._engine.getProperty('rate')}")
        except Exception as e:
            print(f"[TTS] Failed to set rate: {e}")

    def list_voices(self) -> List[dict]:
        out: List[dict] = []
        try:
            voices = self._engine.getProperty("voices") or []
            for i, v in enumerate(voices):
                out.append({
                    "index": i,
                    "id": getattr(v, "id", None),
                    "name": getattr(v, "name", None),
                    "languages": getattr(v, "languages", None),
                })
        except Exception as e:
            out.append({"error": f"Failed to enumerate voices: {e}"})
        return out

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
        # Always generate a fresh file (no caching)
        ensure_dir(spec.audio_dir)
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

    def create_audio(self, username: str, action: str) -> Tuple[str, str]:
        """Always create a brand new audio file. Returns (absolute_path, filename)."""
        ensure_dir(self.audio_dir)

        act = action.strip().lower()
        if act not in ("join", "leave"):
            raise ValueError("Parameter 'action' must be 'join' or 'leave'.")

        spec = AudioSpec(username_raw=username, action=act, audio_dir=self.audio_dir)
        ogg_path = self.tts.generate_ogg(spec)
        return ogg_path, spec.filename


# ---------- FLASK FACTORY ----------

def create_app(
    external_base_url: str = "",
    tts_voice: str = "",
    tts_voice_index: Optional[int] = None,
) -> Flask:
    root, audio_dir = project_paths()
    app = Flask(__name__, static_url_path="/static", static_folder=os.path.join(root, "static"))
    app.config["EXTERNAL_BASE_URL"] = external_base_url

    # Init TTS with the user preferences
    tts = TTSGenerator(prefer_voice_substr=(tts_voice or ""), voice_index=tts_voice_index)
    service = AudioService(audio_dir=audio_dir, tts=tts)

    @app.get("/api/tts")
    def tts_endpoint():
        username = request.args.get("username", type=str)
        action = request.args.get("action", type=str)
        per_request_base = (request.args.get("base_url", type=str) or "").strip()

        if not username or not action:
            return abort(400, description="Missing 'username' or 'action'.")

        try:
            ogg_path, filename = service.create_audio(username=username, action=action)
        except ValueError as e:
            return abort(400, description=str(e))
        except Exception as e:
            return abort(500, description=f"TTS generation failed: {e}")

        static_folder = os.path.abspath(app.static_folder)
        rel_path = os.path.relpath(ogg_path, static_folder).replace("\\", "/")
        base_override = per_request_base or app.config.get("EXTERNAL_BASE_URL") or None
        file_url = build_file_url(app, rel_path, base_override)
        return jsonify({"url": file_url, "filename": filename}), 200

    @app.get("/api/voices")
    def list_voices():
        try:
            voices = tts.list_voices()
            return app.response_class(
                response=json.dumps(voices, indent=2, ensure_ascii=False),
                status=200,
                mimetype="application/json",
            )
        except Exception as e:
            return abort(500, description=f"Failed to list voices: {e}")

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
    host, port, external_base_url, tts_voice, tts_voice_index = parse_args()
    app = create_app(
        external_base_url=external_base_url,
        tts_voice=tts_voice,
        tts_voice_index=tts_voice_index,
    )
    app.run(host=host, port=port, debug=False)
