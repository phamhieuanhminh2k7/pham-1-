"""
Dental AI Receptionist — FastAPI entry point.

Endpoints:
  GET  /                  Health check
  POST /twilio/voice      Initial TwiML — connects Twilio to Media Streams WS
  POST /twilio/status     Call-status callback (logging only)
  GET  /twilio/token      Access token for browser test client
  WS   /ws/stream         Twilio Media Streams WebSocket handler
  GET  /test-client       Browser-based test client UI
"""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from config import settings
from services.call_session import CallSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Active sessions  {call_sid: CallSession}
_sessions: dict[str, CallSession] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Dental AI Receptionist starting up")
    yield
    logger.info("Shutting down")


app = FastAPI(title="Dental AI Receptionist", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
async def health():
    return {"status": "ok", "service": "Dental AI Receptionist"}


# ─── Twilio voice webhook ──────────────────────────────────────────────────────

@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """
    Twilio calls this when someone dials the Twilio number (or the browser
    client connects).  We respond with TwiML that opens a Media Streams
    WebSocket back to this server.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    logger.info(f"Incoming call — CallSid: {call_sid}")

    response = VoiceResponse()
    connect = Connect()
    # Twilio will open a WebSocket to this URL
    stream = Stream(url=f"wss://{request.headers['host']}/ws/stream")
    stream.parameter(name="CallSid", value=call_sid)
    connect.append(stream)
    response.append(connect)

    return PlainTextResponse(str(response), media_type="text/xml")


@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    logger.info(
        f"Call status — CallSid: {form.get('CallSid')} "
        f"Status: {form.get('CallStatus')}"
    )
    return PlainTextResponse("", status_code=204)


# ─── Access token for browser test client ──────────────────────────────────────

@app.get("/twilio/token")
async def get_token():
    """
    Generate a short-lived Twilio access token for the browser Voice SDK.
    The browser uses this to make outbound calls that route through Twilio
    to our /twilio/voice webhook.
    """
    identity = f"browser-{uuid.uuid4().hex[:8]}"
    token = AccessToken(
        settings.twilio_account_sid,
        settings.twilio_api_key_sid,
        settings.twilio_api_key_secret,
        identity=identity,
        ttl=3600,
    )
    grant = VoiceGrant(outgoing_application_sid=settings.twilio_twiml_app_sid)
    token.add_grant(grant)
    return {"token": token.to_jwt(), "identity": identity}


# ─── Twilio Media Streams WebSocket ────────────────────────────────────────────

@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    """
    One WebSocket connection per active call.
    Twilio opens this immediately after /twilio/voice returns TwiML.
    """
    await ws.accept()

    # Twilio sends a "connected" message first, then "start" with the CallSid
    call_sid = "pending"
    session: CallSession | None = None

    try:
        session = CallSession(call_sid=call_sid, twilio_ws=ws)
        await session.run()
    except WebSocketDisconnect:
        logger.info(f"[{call_sid}] WebSocket disconnected")
    except Exception as exc:
        logger.error(f"[{call_sid}] Session error: {exc}", exc_info=True)
    finally:
        _sessions.pop(call_sid, None)


# ─── Browser test client ───────────────────────────────────────────────────────

@app.get("/test-client", response_class=HTMLResponse)
async def test_client():
    with open("static/index.html") as f:
        return f.read()
