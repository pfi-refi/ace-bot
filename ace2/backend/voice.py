"""
ElevenLabs text-to-speech proxy for Ace 2.0.

Key stays server-side (ELEVENLABS_API_KEY), same pattern as weather.py. The
portal gets mp3 (plays in the browser); the Telegram bot uses opus elsewhere.
The voice is Brady's designed "ACE" voice — the SAME ELEVENLABS_VOICE_ID the bot
uses, so both interfaces are literally one voice. ELEVENLABS_MODEL_ID must match
across services too — the same voice rendered by a different model is subtly a
different voice.

Returns (audio_bytes, mime) on success, or (None, reason) so the frontend can
fall back to browser speechSynthesis gracefully.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger("ace2.voice")

DEFAULT_MODEL = "eleven_turbo_v2_5"          # conversational quality, still low-latency
FALLBACK_MODEL = "eleven_flash_v2_5"         # if the primary model errors (e.g. plan), still the real ACE voice — never the robot
DEFAULT_SETTINGS = {
    "stability": 0.55,        # a touch steadier → less warble between phrases
    "similarity_boost": 0.75,
    "style": 0.25,
    "use_speaker_boost": True,
    "speed": 1.0,             # normal cadence (1.1 read rushed/clipped)
}


def configured() -> bool:
    return bool(
        os.environ.get("ELEVENLABS_API_KEY", "").strip()
        and os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    )


def _settings() -> dict:
    raw = os.environ.get("ELEVENLABS_VOICE_SETTINGS", "").strip()
    if not raw:
        return dict(DEFAULT_SETTINGS)
    try:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(json.loads(raw))
        return merged
    except Exception as e:
        logger.warning("bad ELEVENLABS_VOICE_SETTINGS (%s) — defaults", e)
        return dict(DEFAULT_SETTINGS)


STT_MODEL = "scribe_v1"          # ElevenLabs Scribe — real transcription, one vendor with TTS


async def transcribe(audio: bytes, filename: str = "speech.webm", content_type: str = "audio/webm"):
    """Transcribe recorded mic audio with ElevenLabs Scribe. → (text, None) | (None, reason).

    Replaces the browser's flaky Web Speech recognition: the frontend records the mic
    and posts the audio here; a real STT model returns accurate text.
    """
    if not audio:
        return None, "empty audio"
    if not os.environ.get("ELEVENLABS_API_KEY", "").strip():
        return None, "not configured"
    api_key = os.environ["ELEVENLABS_API_KEY"].strip()
    model = os.environ.get("ELEVENLABS_STT_MODEL", STT_MODEL).strip()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": api_key},
                data={"model_id": model},
                files={"file": (filename, audio, content_type or "audio/webm")},
            )
        if resp.status_code != 200:
            logger.warning("ElevenLabs STT %s: %s", resp.status_code, resp.text[:200])
            return None, f"stt {resp.status_code}"
        return (resp.json().get("text") or "").strip(), None
    except Exception as e:
        logger.warning("ElevenLabs STT error: %s", e)
        return None, str(e)


async def synthesize(text: str):
    """Render `text` to mp3 bytes with the ACE voice. → (bytes, mime) | (None, reason)."""
    text = (text or "").strip()
    if not text:
        return None, "empty text"
    if not configured():
        return None, "not configured"

    api_key = os.environ["ELEVENLABS_API_KEY"].strip()
    voice_id = os.environ["ELEVENLABS_VOICE_ID"].strip()
    primary = os.environ.get("ELEVENLABS_MODEL_ID", DEFAULT_MODEL).strip()
    # Try the good conversational model; if it errors, drop to flash before ever
    # letting the frontend fall back to the browser's robot voice.
    models = [primary] + ([FALLBACK_MODEL] if primary != FALLBACK_MODEL else [])
    settings = _settings()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    last = "tts failed"
    for model_id in models:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    url,
                    params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json={"text": text, "model_id": model_id, "voice_settings": settings},
                )
            if resp.status_code == 200:
                return resp.content, "audio/mpeg"
            last = f"tts {resp.status_code}"
            logger.warning("ElevenLabs %s (model=%s): %s", resp.status_code, model_id, resp.text[:200])
        except Exception as e:
            last = str(e)
            logger.warning("ElevenLabs error (model=%s): %s", model_id, e)
    return None, last
