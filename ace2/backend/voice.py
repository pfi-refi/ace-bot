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

DEFAULT_MODEL = "eleven_flash_v2_5"          # ~75ms, ElevenLabs' real-time pick
DEFAULT_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.3,
    "use_speaker_boost": True,
    "speed": 1.1,
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


async def synthesize(text: str):
    """Render `text` to mp3 bytes with the ACE voice. → (bytes, mime) | (None, reason)."""
    text = (text or "").strip()
    if not text:
        return None, "empty text"
    if not configured():
        return None, "not configured"

    api_key = os.environ["ELEVENLABS_API_KEY"].strip()
    voice_id = os.environ["ELEVENLABS_VOICE_ID"].strip()
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", DEFAULT_MODEL).strip()
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={"text": text, "model_id": model_id, "voice_settings": _settings()},
            )
        if resp.status_code != 200:
            logger.warning("ElevenLabs %s: %s", resp.status_code, resp.text[:300])
            return None, f"tts {resp.status_code}"
        return resp.content, "audio/mpeg"
    except Exception as e:
        logger.warning("ElevenLabs error: %s", e)
        return None, str(e)
