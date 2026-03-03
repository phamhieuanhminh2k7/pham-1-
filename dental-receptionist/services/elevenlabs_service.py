"""
ElevenLabs streaming TTS.

Uses the HTTP streaming endpoint so we can start sending audio to Twilio
the moment the first audio bytes arrive — without waiting for the full sentence
to be synthesised.

Output format: ulaw_8000  (μ-law 8 kHz, exactly what Twilio Media Streams expects)
No audio conversion needed.
"""

import logging
from typing import AsyncGenerator

import httpx

from config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.elevenlabs.io/v1/text-to-speech"
_HEADERS = {
    "xi-api-key": settings.elevenlabs_api_key,
    "Content-Type": "application/json",
    "Accept": "audio/basic",  # μ-law
}


async def stream_tts(text: str) -> AsyncGenerator[bytes, None]:
    """
    Stream synthesised μ-law audio for `text`.
    Yields chunks as they arrive — caller can forward directly to Twilio.
    """
    if not text.strip():
        return

    url = f"{_BASE_URL}/{settings.elevenlabs_voice_id}/stream?output_format=ulaw_8000"

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2",  # available on all plans including free
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.8,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", url, headers=_HEADERS, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                logger.error(f"ElevenLabs error {resp.status_code}: {body}")
                return
            async for chunk in resp.aiter_bytes(chunk_size=512):
                if chunk:
                    yield chunk
