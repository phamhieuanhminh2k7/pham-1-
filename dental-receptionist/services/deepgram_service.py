"""
Deepgram real-time streaming STT.

Connects via the Deepgram SDK WebSocket.  Sends raw μ-law 8 kHz audio frames
(exactly what Twilio Media Streams delivers) and fires a callback whenever
Deepgram marks a phrase as speech_final.
"""

import asyncio
import logging
from typing import Callable, Coroutine, Any

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveTranscriptionEvents,
    LiveOptions,
)

from config import settings

logger = logging.getLogger(__name__)

TranscriptCallback = Callable[[str], Coroutine[Any, Any, None]]


class DeepgramConnection:
    """One per call.  Call connect(), feed audio with send(), then close()."""

    def __init__(self, on_speech_final: TranscriptCallback):
        self._callback = on_speech_final
        self._dg_connection = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        self._loop = asyncio.get_event_loop()

        client = DeepgramClient(
            settings.deepgram_api_key,
            config=DeepgramClientOptions(options={"keepalive": "true"}),
        )

        self._dg_connection = client.listen.asyncwebsocket.v("1")

        self._dg_connection.on(
            LiveTranscriptionEvents.Transcript, self._on_transcript
        )
        self._dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)

        options = LiveOptions(
            model="nova-2-phonecall",
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            punctuate=True,
            interim_results=True,
            utterance_end_ms="1000",
            vad_events=True,
        )

        started = await self._dg_connection.start(options)
        if not started:
            raise RuntimeError("Deepgram connection failed to start")
        logger.info("Deepgram connected")

    async def send(self, audio: bytes) -> None:
        if self._dg_connection:
            await self._dg_connection.send(audio)

    async def close(self) -> None:
        if self._dg_connection:
            await self._dg_connection.finish()
            logger.info("Deepgram connection closed")

    async def _on_transcript(self, _self, result, **kwargs) -> None:
        try:
            alternatives = result.channel.alternatives
            if not alternatives:
                return
            transcript = alternatives[0].transcript.strip()
            if not transcript:
                return
            # Fire callback only when Deepgram marks the phrase as final
            if result.speech_final:
                logger.info(f"Speech final: {transcript!r}")
                await self._callback(transcript)
        except Exception as exc:
            logger.error(f"Transcript handler error: {exc}")

    async def _on_error(self, _self, error, **kwargs) -> None:
        logger.error(f"Deepgram error: {error}")
