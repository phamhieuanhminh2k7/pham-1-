"""
CallSession — one instance per active phone call.

Orchestrates four concurrent async streams:
  1. Twilio Media Streams WebSocket  (inbound μ-law audio → Deepgram)
  2. Deepgram WebSocket              (real-time STT → transcript queue)
  3. Claude streaming API            (text → sentence detector)
  4. ElevenLabs HTTP stream          (TTS audio → Twilio outbound)

State machine per session:
  GREETING → LISTENING → THINKING → SPEAKING → LISTENING → …
"""

import asyncio
import base64
import json
import logging
import re
from enum import Enum

from fastapi import WebSocket

from services import deepgram_service, claude_service, elevenlabs_service
from services import notification_service, sheets_service
from utils.sentence_detector import extract_sentence, flush

logger = logging.getLogger(__name__)


def _clean_for_tts(text: str) -> str:
    """Strip markdown symbols that ElevenLabs would read aloud (e.g. 'asterisk')."""
    text = re.sub(r'\*+', '', text)                        # * and **
    text = re.sub(r'#{1,6}\s?', '', text)                  # # headers
    text = re.sub(r'`+', '', text)                         # backticks
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)   # [text](url) → text
    return text.strip()


class State(str, Enum):
    GREETING  = "greeting"
    LISTENING = "listening"
    THINKING  = "thinking"
    SPEAKING  = "speaking"


class CallSession:
    def __init__(self, call_sid: str, twilio_ws: WebSocket):
        self.call_sid    = call_sid
        self.twilio_ws   = twilio_ws
        self.stream_sid: str | None = None
        self.state       = State.GREETING

        self.history:  list[dict] = []
        self.context:  dict       = {}

        self._transcript_queue: asyncio.Queue[str] = asyncio.Queue()
        self._deepgram: deepgram_service.DeepgramConnection | None = None
        self._interrupted = False

    # ──────────────────────────────────────────────────────────────────────────
    # Entry point
    # ──────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Drive the session.  Called once per call from the WS endpoint."""
        # Load context in a background thread so it doesn't delay the greeting.
        # Twilio's "start" event takes a few hundred ms, so context is usually
        # ready before _greet() fires.
        context_task = asyncio.create_task(
            asyncio.to_thread(self._load_context)
        )
        await asyncio.gather(
            self._twilio_receiver(),
            self._response_handler(),
            context_task,
        )

    def _load_context(self) -> None:
        try:
            self.context = sheets_service.get_full_context()
        except Exception as exc:
            logger.error(f"[{self.call_sid}] Failed to load context: {exc}")
            self.context = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Twilio receiver — reads every incoming WS message
    # ──────────────────────────────────────────────────────────────────────────

    async def _twilio_receiver(self) -> None:
        try:
            async for raw in self.twilio_ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "start":
                    self.stream_sid = msg["start"]["streamSid"]
                    logger.info(f"[{self.call_sid}] Media stream started ({self.stream_sid})")
                    # Kick off the greeting (non-blocking — response_handler drives it)
                    await self._transcript_queue.put("__GREET__")

                elif event == "media":
                    # Forward audio to Deepgram regardless of state so that
                    # patient speech during SPEAKING triggers an interrupt.
                    if self._deepgram:
                        audio = base64.b64decode(msg["media"]["payload"])
                        await self._deepgram.send(audio)

                elif event == "stop":
                    logger.info(f"[{self.call_sid}] Call ended by Twilio")
                    await self._stop_listening()
                    await self._transcript_queue.put(None)  # sentinel
                    break

        except Exception as exc:
            logger.error(f"[{self.call_sid}] Twilio receiver error: {exc}")
            await self._transcript_queue.put(None)

    # ──────────────────────────────────────────────────────────────────────────
    # Response handler — generates speech for every incoming transcript
    # ──────────────────────────────────────────────────────────────────────────

    async def _response_handler(self) -> None:
        while True:
            transcript = await self._transcript_queue.get()
            if transcript is None:
                break

            if transcript == "__GREET__":
                await self._greet()
            else:
                await self._respond(transcript)

    # ──────────────────────────────────────────────────────────────────────────
    # Greeting
    # ──────────────────────────────────────────────────────────────────────────

    async def _greet(self) -> None:
        """Synthesise and play the opening greeting, then start listening."""
        info = self.context.get("business_info", {})
        clinic = info.get("clinic_name", "our dental clinic")
        dentist = info.get("dentist_name", "the dentist")
        greeting = (
            f"Hello, thank you for calling {clinic}. "
            f"I'm the AI receptionist for {dentist}. "
            f"How can I help you today?"
        )
        self.state = State.SPEAKING
        await self._speak_text(greeting)
        await self._start_listening()

    # ──────────────────────────────────────────────────────────────────────────
    # Generate + stream a Claude response
    # ──────────────────────────────────────────────────────────────────────────

    async def _respond(self, user_text: str) -> None:
        logger.info(f"[{self.call_sid}] User: {user_text!r}")
        self.state = State.THINKING
        # Keep Deepgram running so patient speech during SPEAKING is detected.

        self.history.append({"role": "user", "content": user_text})

        sentence_buffer = ""
        full_response   = ""
        booking_data: dict | None = None

        self.state = State.SPEAKING
        self._interrupted = False

        async for text_chunk, tool_data in claude_service.stream_response(
            self.history, self.context
        ):
            if self._interrupted:
                break

            if tool_data:
                booking_data = tool_data
                break

            sentence_buffer += text_chunk
            full_response   += text_chunk

            sentence, sentence_buffer = extract_sentence(sentence_buffer)
            if sentence:
                await self._speak_text(sentence)
                if self._interrupted:
                    break

        # Flush any remaining buffer
        if not self._interrupted:
            remainder = flush(sentence_buffer)
            if remainder:
                full_response += remainder
                await self._speak_text(remainder)

        self.history.append({"role": "assistant", "content": full_response})

        # ── Booking flow ──────────────────────────────────────────────────────
        if booking_data and not self._interrupted:
            await self._handle_booking(booking_data)

        self.state = State.LISTENING

    # ──────────────────────────────────────────────────────────────────────────
    # Booking
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_booking(self, booking_data: dict) -> None:
        logger.info(f"[{self.call_sid}] Booking: {booking_data}")

        # 1. Append booking row to Google Sheet A
        # Look up the duration from the service the patient chose
        services = self.context.get("services", [])
        booked_service = booking_data["service"].strip().lower()
        matched = next(
            (s for s in services if s["name"].strip().lower() == booked_service),
            None,
        )
        duration = matched["duration"] if matched and matched.get("duration") else "30"
        try:
            sheets_service.book_appointment(
                patient_name=booking_data["patient_name"],
                patient_phone=booking_data["patient_phone"],
                date=booking_data["date"],
                time=booking_data["time"],
                service=booking_data["service"],
                duration=str(duration),
            )
        except Exception as exc:
            logger.error(f"[{self.call_sid}] Sheet append failed: {exc}")

        # 2. Notifications (run in thread pool — Twilio SDK is synchronous)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, notification_service.send_sms_confirmation, booking_data)
        await loop.run_in_executor(None, notification_service.send_whatsapp_notification, booking_data)

        # 3. Stream Claude's confirmation message
        confirmation_buffer = ""
        async for text_chunk in claude_service.stream_after_booking(
            booking_data, self.history, self.context
        ):
            confirmation_buffer += text_chunk
            sentence, confirmation_buffer = extract_sentence(confirmation_buffer)
            if sentence:
                await self._speak_text(sentence)

        if confirmation_buffer:
            await self._speak_text(flush(confirmation_buffer))

    # ──────────────────────────────────────────────────────────────────────────
    # Deepgram lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    async def _start_listening(self) -> None:
        self._deepgram = deepgram_service.DeepgramConnection(
            on_speech_final=self._on_transcript
        )
        try:
            await self._deepgram.connect()
            self.state = State.LISTENING
        except Exception as exc:
            logger.error(f"[{self.call_sid}] Deepgram connect failed: {exc}")

    async def _stop_listening(self) -> None:
        if self._deepgram:
            try:
                await self._deepgram.close()
            except Exception:
                pass
            self._deepgram = None

    async def _on_transcript(self, transcript: str) -> None:
        """Deepgram callback — fires on speech_final."""
        if self.state == State.SPEAKING:
            # Patient interrupted — cancel current audio
            self._interrupted = True
            await self._send_clear()
            logger.info(f"[{self.call_sid}] Interrupted by patient: {transcript!r}")

        await self._transcript_queue.put(transcript)

    # ──────────────────────────────────────────────────────────────────────────
    # Audio helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _speak_text(self, text: str) -> None:
        """Convert text → μ-law audio → stream to Twilio."""
        text = _clean_for_tts(text)
        if not text:
            return
        logger.debug(f"[{self.call_sid}] Speaking: {text!r}")
        async for audio_chunk in elevenlabs_service.stream_tts(text):
            if self._interrupted:
                break
            await self._send_audio(audio_chunk)

    async def _send_audio(self, audio: bytes) -> None:
        payload = base64.b64encode(audio).decode("utf-8")
        await self.twilio_ws.send_json(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload},
            }
        )

    async def _send_clear(self) -> None:
        """Tell Twilio to flush its audio buffer (interrupt playback)."""
        if self.stream_sid:
            await self.twilio_ws.send_json(
                {"event": "clear", "streamSid": self.stream_sid}
            )
