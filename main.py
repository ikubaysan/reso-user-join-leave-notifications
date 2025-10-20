#!/usr/bin/env python3
"""
Flask TTS API (OGG output, ALWAYS new file per request, UUID filenames).

Supports two engines:
  - pyttsx3 (offline, system voices)
  - gTTS (Google Translate TTS, free, no API key; requires internet)

Filename format (no caching):
  <uuid>_<username>_<action>.ogg

HTTP:
  GET /api/tts?username=<str>&action=<join|leave>
      [&base_url=<http(s)://host:port>]
      [&engine=<pyttsx3|gtts>]
      [&lang=<gtts lang code>]
      [&tld=<gtts tld>]
  → {"url": "<absolute URL to .ogg file>", "filename": "...ogg", "engine": "gtts|pyttsx3"}

  GET /api/voices
  → If engine=pyttsx3: JSON list of system voices (index/id/name/languages)
    If engine=gtts: JSON of available languages and current tld

Base URL precedence:
  1) Query param base_url (per-request override)
  2) CLI flag --external-base-url
  3) Default: Flask builds URL from the incoming request

CLI examples:
  # Use gTTS with US English voice style (Google Translate)
  python main.py --engine gtts --gtts-lang en --gtts-tld com

  # Use pyttsx3 with a specific index (Linux deterministic)
  python main.py --engine pyttsx3 --tts-voice-index 28
"""

from __future__ import annotations
import os, re, threading, argparse, json, uuid
from typing import Final, Literal, Optional, Tuple, List, Any, Dict
from urllib.parse import urljoin
from flask import Flask, jsonify, request, url_for, abort
import pyttsx3
from pydub import AudioSegment

# gTTS bits
try:
    from gtts import gTTS
    from gtts.lang import tts_langs as gtts_langs
except Exception:
    gTTS = None
    gtts_langs = None  # We'll guard usage below.

Action = Literal["join", "leave"]
EngineName = Literal["pyttsx3", "gtts"]


# ---------- CLI PARSER ----------

def parse_args() -> Tuple[str, int, str, EngineName, str, Optional[int], str, str]:
    parser = argparse.ArgumentParser(description="Start the TTS Flask server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4684, help="Port to bind (default: 4684)")
    parser.add_argument(
        "--external-base-url",
        default="",
        help="Optional absolute base URL for returned file links "
             "(e.g., http://gallery.ikubaysan.com:4648).",
    )

    # Engine selection
    parser.add_argument(
        "--engine",
        choices=["pyttsx3", "gtts"],
        default="pyttsx3",
        help="TTS engine to use (default: pyttsx3)."
    )

    # pyttsx3 options
    parser.add_argument(
        "--tts-voice",
        dest="tts_voice",
        default="",
        help="(pyttsx3) Preferred voice (substring or exact id/name), case-insensitive."
    )
    parser.add_argument(
        "--tts-voice-index",
        dest="tts_voice_index",
        type=int,
        default=None,
        help="(pyttsx3) Pick a voice by numeric index."
    )

    # gTTS options
    parser.add_argument(
        "--gtts-lang",
        dest="gtts_lang",
        default="en",
        help="(gTTS) Language code (default: en)."
    )
    parser.add_argument(
        "--gtts-tld",
        dest="gtts_tld",
        default="com",
        help="(gTTS) Accent domain like 'com', 'co.uk', 'com.au' (default: com)."
    )

    args = parser.parse_args()
    return (
        args.host,
        args.port,
        args.external_base_url.strip(),
        args.engine,  # type: ignore
        args.tts_voice.strip(),
        args.tts_voice_index,
        args.gtts_lang.strip(),
        args.gtts_tld.strip(),
    )


# ---------- UTILITIES ----------

def ensure_dir(path: str) -> None:
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        ensure_dir(parent)

def project_paths() -> Tuple[str, str]:
    root = os.path.dirname(os.path.abspath(__file__))
    audio_dir = os.path.join(root, "static", "audio")
    ensure_dir(audio_dir)
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
        return f"{self._uuid}_{self.username_safe}_{self.action}.ogg"

    @property
    def ogg_path(self) -> str:
        return os.path.join(self.audio_dir, self.filename)

    @property
    def tmp_wav_path(self) -> str:
        return os.path.join(self.audio_dir, f".tmp_{self._uuid}.wav")

    @property
    def tmp_mp3_path(self) -> str:
        return os.path.join(self.audio_dir, f".tmp_{self._uuid}.mp3")

    @property
    def phrase(self) -> str:
        return build_phrase(self.username_raw, self.action)


class BaseTTS:
    def list_voices(self) -> List[dict]:
        return []

    def generate_ogg(self, spec: AudioSpec) -> str:
        raise NotImplementedError


class PyTTSX3Generator(BaseTTS):
    """pyttsx3 (offline)"""
    def __init__(self, prefer_voice_substr: str = "", voice_index: Optional[int] = None, rate_delta: int = -20):
        self._engine = pyttsx3.init()
        self._lock = threading.Lock()

        try:
            voices: List[Any] = self._engine.getProperty("voices") or []
            chosen_id = None

            if voice_index is not None:
                if 0 <= voice_index < len(voices):
                    chosen_id = getattr(voices[voice_index], "id", None)
                    print(f"[pyttsx3] Using voice index {voice_index}: {chosen_id}")
                else:
                    print(f"[pyttsx3] Voice index {voice_index} out of range (0..{len(voices)-1}). Ignoring.")

            if chosen_id is None and prefer_voice_substr:
                needle = prefer_voice_substr.lower()
                for v in voices:
                    vid = (getattr(v, "id", "") or "").lower()
                    vname = (getattr(v, "name", "") or "").lower()
                    if needle in vid or needle in vname:
                        chosen_id = v.id
                        print(f"[pyttsx3] Using voice by substring '{prefer_voice_substr}': {chosen_id}")
                        break

            if chosen_id:
                self._engine.setProperty("voice", chosen_id)
            else:
                if prefer_voice_substr or (voice_index is not None):
                    print("[pyttsx3] Preferred voice not found; using default.")
        except Exception as e:
            print(f"[pyttsx3] Voice selection failed: {e}")

        try:
            rate = self._engine.getProperty("rate")
            self._engine.setProperty("rate", rate + rate_delta)
            print(f"[pyttsx3] Set speech rate to {self._engine.getProperty('rate')}")
        except Exception as e:
            print(f"[pyttsx3] Failed to set rate: {e}")

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

    def generate_ogg(self, spec: AudioSpec) -> str:
        ensure_dir(spec.audio_dir)
        with self._lock:
            self._engine.save_to_file(spec.phrase, spec.tmp_wav_path)
            self._engine.runAndWait()
        try:
            audio = AudioSegment.from_wav(spec.tmp_wav_path)
            audio.export(spec.ogg_path, format="ogg")
        finally:
            for p in (spec.tmp_wav_path,):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        return spec.ogg_path


class GTTSGenerator(BaseTTS):
    """gTTS (Google Translate TTS, no API key). Requires internet."""
    def __init__(self, lang: str = "en", tld: str = "com"):
        if gTTS is None:
            raise RuntimeError("gTTS is not installed. `pip install gTTS`")
        self.lang = lang
        self.tld = tld

    def list_voices(self) -> List[dict]:
        # gTTS doesn't expose voices, only languages. Return that info + tld.
        try:
            langs = gtts_langs() if gtts_langs else {}
            # Flatten to key -> name
            entries = [{"lang": k, "name": v} for k, v in sorted(langs.items(), key=lambda kv: kv[0])]
            return [{"engine": "gtts", "tld": self.tld, "languages": entries}]
        except Exception as e:
            return [{"engine": "gtts", "tld": self.tld, "error": f"Failed to list languages: {e}"}]

    def generate_ogg(self, spec: AudioSpec) -> str:
        ensure_dir(spec.audio_dir)
        # Save mp3 via gTTS, then convert to ogg
        tts = gTTS(text=spec.phrase, lang=self.lang, tld=self.tld, slow=False)
        tts.save(spec.tmp_mp3_path)
        try:
            audio = AudioSegment.from_file(spec.tmp_mp3_path, format="mp3")
            audio.export(spec.ogg_path, format="ogg")
        finally:
            for p in (spec.tmp_mp3_path,):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        return spec.ogg_path


class AudioService:
    def __init__(self, audio_dir: str, tts_engine: BaseTTS) -> None:
        self.audio_dir: Final[str] = audio_dir
        self.tts: Final[BaseTTS] = tts_engine

    def create_audio(self, username: str, action: str) -> Tuple[str, str]:
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
    engine_name: EngineName = "pyttsx3",
    tts_voice: str = "",
    tts_voice_index: Optional[int] = None,
    gtts_lang_code: str = "en",
    gtts_tld: str = "com",
) -> Flask:
    root, audio_dir = project_paths()
    app = Flask(__name__, static_url_path="/static", static_folder=os.path.join(root, "static"))
    app.config["EXTERNAL_BASE_URL"] = external_base_url
    app.config["ENGINE_NAME"] = engine_name
    app.config["GTTS_LANG"] = gtts_lang_code
    app.config["GTTS_TLD"] = gtts_tld
    app.config["PYTTSX3_VOICE"] = tts_voice
    app.config["PYTTSX3_VOICE_INDEX"] = tts_voice_index

    # Build default engine
    if engine_name == "gtts":
        tts_engine: BaseTTS = GTTSGenerator(lang=gtts_lang_code, tld=gtts_tld)
    else:
        tts_engine = PyTTSX3Generator(prefer_voice_substr=(tts_voice or ""), voice_index=tts_voice_index)

    service = AudioService(audio_dir=audio_dir, tts_engine=tts_engine)

    @app.get("/api/tts")
    def tts_endpoint():
        username = request.args.get("username", type=str)
        action = request.args.get("action", type=str)
        per_request_base = (request.args.get("base_url", type=str) or "").strip()

        # Optional per-request engine override
        req_engine = request.args.get("engine", type=str)
        # Optional per-request gTTS overrides (lang/tld)
        req_lang = request.args.get("lang", type=str)
        req_tld = request.args.get("tld", type=str)

        if not username or not action:
            return abort(400, description="Missing 'username' or 'action'.")

        # Resolve engine for this request
        current_engine_name = app.config["ENGINE_NAME"]
        if req_engine in ("pyttsx3", "gtts"):
            current_engine_name = req_engine  # type: ignore

        # Build a per-request engine if different from the default or needs different gTTS params
        local_service = service
        if current_engine_name == "gtts":
            lang = (req_lang or app.config["GTTS_LANG"])
            tld = (req_tld or app.config["GTTS_TLD"])
            local_tts = GTTSGenerator(lang=lang, tld=tld)
            local_service = AudioService(audio_dir=service.audio_dir, tts_engine=local_tts)
        elif current_engine_name == "pyttsx3" and req_engine == "pyttsx3":
            # Reuse default pyttsx3 config (no per-request options exposed here)
            pass

        try:
            ogg_path, filename = local_service.create_audio(username=username, action=action)
        except ValueError as e:
            return abort(400, description=str(e))
        except Exception as e:
            return abort(500, description=f"TTS generation failed: {e}")

        static_folder = os.path.abspath(app.static_folder)
        rel_path = os.path.relpath(ogg_path, static_folder).replace("\\", "/")
        base_override = per_request_base or app.config.get("EXTERNAL_BASE_URL") or None
        file_url = build_file_url(app, rel_path, base_override)
        return jsonify({"url": file_url, "filename": filename, "engine": current_engine_name}), 200

    @app.get("/api/voices")
    def list_voices():
        try:
            # Reflect the default engine in /api/voices
            if app.config["ENGINE_NAME"] == "gtts":
                tts = GTTSGenerator(app.config["GTTS_LANG"], app.config["GTTS_TLD"])
                voices = tts.list_voices()
            else:
                tts = PyTTSX3Generator(
                    prefer_voice_substr=app.config["PYTTSX3_VOICE"] or "",
                    voice_index=app.config["PYTTSX3_VOICE_INDEX"]
                )
                voices = tts.list_voices()
            return app.response_class(
                response=json.dumps(voices, indent=2, ensure_ascii=False),
                status=200,
                mimetype="application/json",
            )
        except Exception as e:
            return abort(500, description=f"Failed to list voices/languages: {e}")

    @app.get("/")
    def health():
        try:
            ensure_dir(os.path.join(app.static_folder, "audio"))
        except Exception:
            pass
        return jsonify({"ok": True, "engine": app.config["ENGINE_NAME"]}), 200

    return app


# ---------- ENTRYPOINT ----------

if __name__ == "__main__":
    host, port, external_base_url, engine_name, tts_voice, tts_voice_index, gtts_lang_code, gtts_tld = parse_args()
    app = create_app(
        external_base_url=external_base_url,
        engine_name=engine_name,
        tts_voice=tts_voice,
        tts_voice_index=tts_voice_index,
        gtts_lang_code=gtts_lang_code,
        gtts_tld=gtts_tld,
    )
    app.run(host=host, port=port, debug=False)
