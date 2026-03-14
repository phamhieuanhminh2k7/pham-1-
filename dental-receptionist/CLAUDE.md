# Dental AI Receptionist — CLAUDE.md

## What This Project Does

AI phone receptionist for a dental clinic. Patients call a Twilio number (or use the browser test client at `/test-client`), speak naturally, and the AI books appointments by reading/writing Google Sheets.

---

## Tech Stack

| Layer | Tool | Version | Why |
|---|---|---|---|
| Backend | Python + FastAPI | 3.11 / 0.135.1 | Async-first, native WebSocket support |
| ASGI server | Uvicorn | 0.41.0 | Production-grade, pairs with FastAPI |
| Real-time audio | Twilio Media Streams (WebSocket) | 9.10.2 | Bidirectional audio without REST polling |
| STT | Deepgram nova-3 | SDK 3.11.0 | Streaming transcripts in real-time; **pinned to v3** (v4 SDK has breaking API changes) |
| AI brain | Claude claude-sonnet-4-6 | anthropic 0.84.0 | Streaming + tool use for structured bookings |
| TTS | ElevenLabs eleven_turbo_v2 | httpx 0.28.1 | Fastest model, ulaw_8000 output — no conversion needed |
| Availability | Google Sheets A | google-api-python-client 2.190.0 | Booked appointments only; available slots generated dynamically |
| Config | Google Sheets B | same | Hours, Services, Business Info — client-editable |
| Notifications | Twilio SMS + WhatsApp sandbox | twilio 9.10.2 | Patient SMS + dentist WhatsApp on every booking |

---

## Low-Latency Architecture (target < 1 s end-to-end)

```
Patient stops speaking
  └─ Deepgram speech_final fires immediately (transcript was streaming in real-time)
       └─ Claude starts generating (streaming, first token ~200 ms)
            └─ Sentence boundary detected (~300 ms into Claude's response)
                 └─ ElevenLabs HTTP stream starts → first audio chunk ~200 ms later
                      └─ Audio plays in patient's ear  ← total ~600–800 ms ✅
```

**Key rule**: Never wait for Claude's full response before starting TTS.
`sentence_detector.extract_sentence()` fires ElevenLabs for each sentence as it arrives.

---

## File Map

```
dental-receptionist/
├── main.py                    FastAPI app — all HTTP + WebSocket endpoints
├── config.py                  Pydantic BaseSettings (reads .env)
├── requirements.txt           Pinned dependencies
├── .env.example               Template — copy to .env and fill in all keys
├── test_setup.py              Pre-flight validation script (not a test framework)
├── services/
│   ├── call_session.py        ★ Core orchestrator — one instance per active call
│   ├── claude_service.py      Claude streaming + system prompt builder + tool definition
│   ├── deepgram_service.py    Deepgram SDK v3 WebSocket wrapper (STT)
│   ├── elevenlabs_service.py  ElevenLabs HTTP streaming TTS
│   ├── sheets_service.py      Google Sheets read/write (both spreadsheets)
│   ├── notification_service.py  Twilio SMS + WhatsApp
│   └── __init__.py
├── utils/
│   └── sentence_detector.py  Regex sentence splitter for streaming TTS
└── static/
    └── index.html             Browser test client (Twilio Voice SDK v2.13.1)
```

---

## HTTP & WebSocket Endpoints (main.py)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Health check → `{"status": "ok"}` |
| `POST` | `/twilio/voice` | Twilio webhook — returns TwiML opening a Media Streams WebSocket |
| `POST` | `/twilio/status` | Call-status callback (logging only, returns 204) |
| `GET` | `/twilio/token` | Short-lived Twilio access token for browser Voice SDK |
| `WS` | `/ws/stream` | Twilio Media Streams WebSocket — one connection per active call |
| `GET` | `/test-client` | Serves `static/index.html` browser test UI |

**WebSocket flow**: Twilio connects → sends `"connected"` → sends `"start"` (contains `streamSid`) → sends `"media"` frames → sends `"stop"`.

---

## State Machine (CallSession)

Each call is one `CallSession` instance, driven by two concurrent `asyncio.Task`s:

```
GREETING → LISTENING → THINKING → SPEAKING → LISTENING → …
                ↑                       |
                └───── (interrupted) ───┘
```

| State | What's happening |
|---|---|
| `GREETING` | Initial state; fires `__GREET__` sentinel after stream start |
| `LISTENING` | Deepgram is running; waiting for `speech_final` transcript |
| `THINKING` | Transcript received; Claude streaming has started |
| `SPEAKING` | ElevenLabs audio streaming to Twilio |

**Interruption**: Deepgram runs continuously even during `SPEAKING`. If `speech_final` fires while `state == SPEAKING`, `_interrupted = True` is set, `_send_clear()` flushes Twilio's audio buffer, and the new transcript is queued immediately.

---

## Concurrency Model

```
CallSession.run()
  ├── asyncio.gather(
  │     _twilio_receiver()       ← reads Twilio WS frames; forwards μ-law audio to Deepgram
  │     _response_handler()      ← waits on _transcript_queue; drives Claude + TTS
  │   )
  │
  ├── _transcript_queue: asyncio.Queue[str]
  │     "__GREET__"    → triggers _greet()
  │     <transcript>   → triggers _respond()
  │     None           → shutdown sentinel
  │
  └── Booking notifications run in executor pool (Twilio SDK is synchronous):
        loop.run_in_executor(None, notification_service.send_sms_confirmation, ...)
        loop.run_in_executor(None, notification_service.send_whatsapp_notification, ...)
```

---

## Call Flow (step by step)

1. Patient calls Twilio number / browser client connects
2. Twilio POSTs to `/twilio/voice` → server returns TwiML with `<Connect><Stream url="wss://HOST/ws/stream">`
3. Twilio opens WebSocket to `/ws/stream`
4. `CallSession.run()` loads Google Sheets context (`get_full_context()`), then starts two tasks
5. Twilio sends `"start"` message → `stream_sid` stored → `"__GREET__"` put in queue
6. `_response_handler` receives `"__GREET__"` → `_greet()` synthesises a hardcoded greeting using clinic/dentist names from context → speaks it → starts Deepgram listening
7. Patient speaks → Deepgram streams transcripts → on `speech_final` → `_on_transcript()` → queued
8. `_respond(transcript)` streams Claude → sentence-detect → ElevenLabs → Twilio audio
9. If Claude calls `confirm_booking` tool → `_handle_booking()` → Sheet append → SMS → WhatsApp → `stream_after_booking()` for confirmation speech
10. Back to `LISTENING`

---

## Claude Integration (claude_service.py)

### Booking Tool

Claude uses a single tool `confirm_booking` with required fields:
- `patient_name` — full name
- `patient_phone` — phone number
- `date` — `YYYY-MM-DD`
- `time` — `HH:MM`
- `service` — service name

Claude is instructed not to call the tool until the patient has verbally confirmed all details.

### Streaming Protocol

`stream_response()` yields `(text_chunk, None)` for spoken text or `("", booking_data)` when a tool call is detected. The caller (CallSession) pipes text chunks through `extract_sentence()` and only triggers `stream_after_booking()` after the booking is executed.

### System Prompt

`build_system_prompt(context)` is called on every Claude request and dynamically injects:
- Clinic name, dentist name, address, emergency contact
- Business hours (formatted per-day)
- Services with duration, price, and description
- Available appointment slots (grouped by date with `close_time` for duration-aware filtering)
- Speaking rules, emotional intelligence guidelines, slot suggestion rules, information discipline

**Model**: `claude-sonnet-4-6` | **max_tokens**: 400 (responses), 200 (post-booking confirmation)

### Speaking Rules Claude Must Follow

- No markdown, bullets, asterisks, headers, or formatting of any kind
- Short sentences (under 15 words)
- One question at a time
- Acknowledge before answering (`"Got it."`, `"Sure!"`, etc.)
- Never volunteer all services/hours unless asked
- Adapt tone to patient's emotional state (urgency, hesitation, frustration, casual)

---

## TTS Pipeline (elevenlabs_service.py)

- **Endpoint**: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=ulaw_8000`
- **Model**: `eleven_turbo_v2` (available on all plans including free tier)
- **Output format**: `ulaw_8000` — μ-law 8 kHz mono, exactly what Twilio Media Streams expects; no conversion needed
- **Streaming**: chunks of 512 bytes yielded as they arrive via `httpx` async streaming
- **Voice settings**: stability=0.4, similarity_boost=0.8, style=0.0, use_speaker_boost=True

Before calling ElevenLabs, `_clean_for_tts()` strips markdown symbols that would be read aloud:
- `*`, `**` → removed
- `#` headers → removed
- `` ` `` backticks → removed
- `[text](url)` links → `text` only

---

## STT Pipeline (deepgram_service.py)

- **Model**: `nova-3`
- **Encoding**: `mulaw` at 8000 Hz, 1 channel (matches Twilio output exactly)
- **Config**: `punctuate=True`, `interim_results=True`, `utterance_end_ms="1000"`, `vad_events=True`
- **SDK**: Deepgram v3 (`client.listen.asyncwebsocket.v("1")`) — **do not upgrade to v4**, breaking API changes
- `speech_final=True` is the only transcript event that triggers a response

---

## Sentence Detector (utils/sentence_detector.py)

Controls when ElevenLabs is called during Claude streaming.

| Rule | Condition | Action |
|---|---|---|
| Hard boundary | `.`, `!`, `?` followed by space, buffer ≥ 25 chars | Split immediately |
| Soft boundary | `,`, `;` followed by space, buffer ≥ 60 chars | Split (catches long spoken clauses) |
| Flush | End of Claude stream | Return whatever remains |

---

## Google Sheets Layout

### Spreadsheet A — Appointments (`GOOGLE_SHEETS_AVAILABILITY_ID`)

Sheet name: **Appointments** — stores **only booked appointments** (available slots are generated dynamically).

| Col A: Date | Col B: Time | Col C: Duration | Col D: Patient Name | Col E: Patient Phone | Col F: Service |
|---|---|---|---|---|---|
| 2026-03-10 | 09:00 | 60 | John Doe | +1... | Cleaning |

- Date: `YYYY-MM-DD`
- Time: `HH:MM` (24-hour)
- Duration: integer minutes (pulled from the service's duration at booking time)
- Rows start at row 2 (row 1 is headers, not read)

### Spreadsheet B — Config (`GOOGLE_SHEETS_CONFIG_ID`)

**Sheet: `business_info`** (key/value, Col A = key, Col B = value)

| Key | Description |
|---|---|
| `clinic_name` | Clinic display name |
| `dentist_name` | Dentist's name |
| `address` | Physical address |
| `emergency_contact` | Emergency phone number |
| `max_concurrent` | Simultaneous bookings per time slot (default: `1`) |
| `booking_window_days` | How many days ahead patients can book (default: `7`) |

**Sheet: `hours`** (Col A–D, row 2+)

| A: Day | B: Open (HH:MM) | C: Close (HH:MM) | D: Closed (TRUE/FALSE) |
|---|---|---|---|
| Monday | 09:00 | 17:00 | FALSE |
| Sunday | | | TRUE |

**Sheet: `services`** (Col A–D, row 2+)

| A: Service | B: Duration (min) | C: Price | D: Description |
|---|---|---|---|
| Cleaning | 60 | $120 | Full cleaning and checkup |

---

## Available Slot Generation Algorithm

`get_available_slots()` in `sheets_service.py` builds the slot list dynamically each call:

1. **Slot interval** = minimum service duration across all services (auto-derived)
2. **Iterate** over the next `booking_window_days` days (default: 7), skip today
3. **Skip** closed days or days without hours config
4. **Generate** candidate start times from `open` to `close` at `slot_interval` steps
5. **Check overlaps**: for each candidate time, count existing bookings where `b_start ≤ slot_time < b_start + b_duration`
6. **Allow** slot if `overlap_count < max_concurrent`
7. **Attach** `close_time` to each slot so Claude can filter out slots where `time + service_duration > close_time`

---

## Configuration (config.py)

All settings via Pydantic `BaseSettings`, loaded from `.env`:

```python
# Twilio
twilio_account_sid, twilio_auth_token, twilio_phone_number
twilio_api_key_sid, twilio_api_key_secret, twilio_twiml_app_sid
twilio_whatsapp_from  # default: "whatsapp:+14155238886"
dentist_whatsapp      # dentist's WhatsApp number, e.g. "whatsapp:+84xxxxxxxxx"

# AI services
deepgram_api_key
anthropic_api_key
elevenlabs_api_key
elevenlabs_voice_id   # default: "21m00Tcm4TlvDq8ikWAM" (Rachel — free tier)

# Google Sheets
google_sheets_availability_id
google_sheets_config_id
google_credentials_path  # default: "google-credentials.json"

# Server
server_url  # e.g. https://your-vps.com (no trailing slash)
```

---

## Environment Variables (.env)

Copy `.env.example` → `.env` and fill in:

```
# Twilio
TWILIO_ACCOUNT_SID        from console.twilio.com
TWILIO_AUTH_TOKEN         from console.twilio.com
TWILIO_PHONE_NUMBER       +1xxxxxxxxxx  (US number you purchased)
TWILIO_API_KEY_SID        create at console.twilio.com → API Keys
TWILIO_API_KEY_SECRET     same place
TWILIO_TWIML_APP_SID      create a TwiML App pointing to SERVER_URL/twilio/voice
TWILIO_WHATSAPP_FROM      whatsapp:+14155238886  (sandbox default)
DENTIST_WHATSAPP          whatsapp:+84xxxxxxxxx  (your number)

# AI
DEEPGRAM_API_KEY          deepgram.com → API Keys
ANTHROPIC_API_KEY         console.anthropic.com
ELEVENLABS_API_KEY        elevenlabs.io → Profile → API Key
ELEVENLABS_VOICE_ID       pick from elevenlabs.io/voices

# Google
GOOGLE_SHEETS_AVAILABILITY_ID   the spreadsheet ID from the URL
GOOGLE_SHEETS_CONFIG_ID         the spreadsheet ID from the URL
GOOGLE_CREDENTIALS_PATH         google-credentials.json  (service account JSON)

# Server
SERVER_URL                https://your-vps-domain.com  (no trailing slash)
```

---

## First-Time Setup Checklist

### 1. Python environment

```bash
cd dental-receptionist
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Google Cloud service account

1. [console.cloud.google.com](https://console.cloud.google.com) → New project → Enable **Google Sheets API**
2. IAM → Service Accounts → Create → Download JSON → save as `google-credentials.json` in project root
3. Share both Google Sheets with the service account email (Editor role)
4. Create both spreadsheets with the correct sheet names and column headers (see layout above)

### 3. Twilio setup

1. Buy a US phone number in Twilio Console
2. Create an API Key (Standard) → note SID + Secret
3. Create a TwiML App:
   - Voice Request URL: `https://YOUR_SERVER_URL/twilio/voice` (POST)
   - Note the App SID
4. Enable WhatsApp Sandbox: Twilio Console → Messaging → Try WhatsApp
5. Send the join code from your WhatsApp to activate the sandbox

### 4. Validate setup

```bash
python test_setup.py
```

This checks: Google Sheets connectivity, Claude API, ElevenLabs TTS, Deepgram API key, Twilio account + TwiML App, and `SERVER_URL` reachability.

### 5. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000/test-client` in Chrome → click **Call AI Receptionist**.

---

## Notifications (notification_service.py)

Both functions are called synchronously in a thread pool executor (Twilio SDK blocks).

**SMS to patient** (`send_sms_confirmation`):
```
Hi {name}! Your appointment is confirmed:
  Date:    YYYY-MM-DD
  Time:    HH:MM
  Service: ...
Reply CANCEL to cancel.  See you soon!
```
Sent from `TWILIO_PHONE_NUMBER` to `booking["patient_phone"]`.

**WhatsApp to dentist** (`send_whatsapp_notification`):
```
📅 New Booking Alert
Patient: ...  Phone: ...  Date: ...  Time: ...  Service: ...
```
Sent from `TWILIO_WHATSAPP_FROM` to `DENTIST_WHATSAPP`.

---

## Customising the AI

**No code changes needed** — edit Spreadsheet B (Config):
- Change hours → AI only offers slots in those hours
- Add/remove services → AI quotes updated prices and descriptions
- Change clinic/dentist name → greeting and responses update automatically
- Adjust `max_concurrent` → allow parallel bookings per slot
- Adjust `booking_window_days` → expand/shrink how far ahead patients can book

**To change AI tone or speaking rules**: edit `build_system_prompt()` in `services/claude_service.py` — specifically the "SPEAKING RULES", "EMOTIONAL INTELLIGENCE", and "SLOT SUGGESTION RULES" sections.

---

## Demo Checklist

- [ ] `uvicorn main:app --reload` running on VPS
- [ ] Google Sheets populated with services and business hours
- [ ] Browser: `localhost:8000/test-client` loaded in Chrome
- [ ] Microphone permission granted
- [ ] Walk-through script:
  1. Click Call → AI greets with clinic name
  2. Say "I need a cleaning next Tuesday"
  3. AI offers up to 3 slots for that day
  4. Patient picks one → AI asks for name + phone
  5. AI reads back details → patient confirms
  6. Booking confirmed → show Sheet A updated + SMS sent

---

## Common Issues

| Problem | Fix |
|---|---|
| `pydantic_settings` import error | `pip install pydantic-settings` |
| Deepgram not connecting | Check `DEEPGRAM_API_KEY` in `.env`; ensure using SDK v3 (not v4) |
| No audio from ElevenLabs | Verify `ELEVENLABS_VOICE_ID` is a valid voice ID from elevenlabs.io/voices |
| ElevenLabs 401 | `ELEVENLABS_API_KEY` is wrong or expired |
| Twilio 11200 webhook error | `SERVER_URL` in TwiML App is wrong or server is not running |
| Google Sheets 403 | Service account email not shared on the spreadsheet with Editor role |
| WhatsApp not delivered | Must join sandbox first (send the join code from your phone to the sandbox number) |
| Browser client "Device not ready" | Check browser console — usually a CORS or token issue; verify `TWILIO_TWIML_APP_SID` |
| Greeting fires before Sheets loads | Context is loaded synchronously in `run()` before `asyncio.gather()` — check Sheets connectivity |
| Slots show as empty in Claude | Verify `services` sheet has `Duration` column populated; slot interval = min service duration |
| Booking not written to sheet | Check `GOOGLE_SHEETS_AVAILABILITY_ID`; verify sheet name is exactly `Appointments` |

---

## Key Conventions for AI Assistants

### Adding a new service field to the system prompt

1. Add the column to Spreadsheet B `services` sheet
2. Update `get_services()` in `sheets_service.py` to read the new column
3. Update `_format_services()` in `claude_service.py` to include it in the prompt

### Adding a new Claude tool

1. Define the tool dict (following `BOOKING_TOOL` pattern) in `claude_service.py`
2. Add it to the `tools=[...]` list in `stream_response()`
3. Handle the tool output in `CallSession._respond()` (check `tool_data` alongside `booking_data`)

### Changing Claude model

Update `model=` in both `stream_response()` and `stream_after_booking()` in `claude_service.py`.

### Never upgrade deepgram-sdk past v3

The codebase uses `client.listen.asyncwebsocket.v("1")` — this is a v3 API. v4 has breaking changes.

### All config via .env only

Never hardcode API keys or URLs. All secrets flow through `config.py` → `settings` singleton.

### Async everywhere

All I/O is async. The only exceptions are Twilio SDK calls (sync) which must run via `loop.run_in_executor(None, fn, args)`.

### Google Sheets service is cached

`_get_service()` uses `@lru_cache(maxsize=1)` — the Sheets API client is created once. Context is loaded fresh per call in `CallSession.run()`.

---

## Deployment

Server runs on a VPS with a fixed public URL. Required steps:
1. Set `SERVER_URL` in `.env` to the VPS domain (e.g. `https://yourdomain.com`)
2. Point the Twilio TwiML App Voice Request URL to `SERVER_URL/twilio/voice` (POST)
3. Ensure port 8000 (or your chosen port) is open
4. Run with: `uvicorn main:app --host 0.0.0.0 --port 8000`

For development with a local server, use [ngrok](https://ngrok.com):
```bash
ngrok http 8000
# Copy the https URL → set as SERVER_URL in .env
```
