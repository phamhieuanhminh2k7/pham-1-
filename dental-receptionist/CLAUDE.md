# Dental AI Receptionist — CLAUDE.md

## What This Project Does
AI phone receptionist for a dental clinic.  Patients call a Twilio number (or use the browser test client), speak naturally, and the AI books appointments by reading/writing a Google Sheet.

## Tech Stack
| Layer | Tool | Why |
|---|---|---|
| Backend | Python 3.11 + FastAPI | Async-first, great WS support |
| Real-time audio | Twilio Media Streams (WebSocket) | Bidirectional audio without REST polling |
| STT | Deepgram nova-2-phonecall | Streams transcripts AS patient speaks → near-zero latency by the time they stop |
| AI brain | Claude claude-sonnet-4-6 | Streaming + tool use for structured booking |
| TTS | ElevenLabs eleven_turbo_v2_5 | Fastest model, ulaw_8000 output = no conversion needed |
| Availability | Google Sheets A | Date/Time/Status rows — AI reads & updates |
| Config | Google Sheets B | Hours, Services, Staff — client editable |
| Notifications | Twilio SMS + WhatsApp sandbox | Patient SMS + dentist WhatsApp on every booking |

## Low-Latency Architecture (target < 1 s)

```
Patient stops speaking
  └─ Deepgram speech_final fires immediately (transcript was streaming in real-time)
       └─ Claude starts generating (streaming, first token ~200 ms)
            └─ Sentence boundary detected (~300 ms)
                 └─ ElevenLabs HTTP stream starts → first audio chunk ~200 ms later
                      └─ Audio plays in patient's ear  ← total ~600–800 ms ✅
```

**Key rule**: Never wait for Claude's full response before starting TTS.
`sentence_detector.extract_sentence()` fires ElevenLabs for each sentence as it arrives.

## File Map
```
dental-receptionist/
├── main.py                    FastAPI app, all HTTP + WS endpoints
├── config.py                  Pydantic settings (reads .env)
├── requirements.txt
├── .env.example               Copy → .env and fill in all keys
├── services/
│   ├── call_session.py        ★ Core orchestrator — one instance per call
│   ├── deepgram_service.py    Deepgram SDK WebSocket wrapper
│   ├── claude_service.py      Claude streaming + system prompt builder
│   ├── elevenlabs_service.py  ElevenLabs HTTP streaming TTS
│   ├── sheets_service.py      Google Sheets read/write (both spreadsheets)
│   └── notification_service.py  Twilio SMS + WhatsApp
├── utils/
│   └── sentence_detector.py  Regex sentence splitter for streaming TTS
└── static/
    └── index.html             Browser test client (Twilio Voice SDK)
```

## Google Sheets Layout

### Spreadsheet A — Availability  (`GOOGLE_SHEETS_AVAILABILITY_ID`)
Sheet name: **Appointments**
| A: Date | B: Time | C: Duration | D: Status | E: Patient Name | F: Patient Phone | G: Service |
|---|---|---|---|---|---|---|
| 2026-03-10 | 09:00 | 60min | Available | | | |
| 2026-03-10 | 10:00 | 30min | Booked | John Doe | +1... | Cleaning |

### Spreadsheet B — Config  (`GOOGLE_SHEETS_CONFIG_ID`)
Three tabs — all client-editable:

**business_info** (key → value)
| A: Key | B: Value |
|---|---|
| clinic_name | Dr. Smith Dental |
| dentist_name | Dr. Smith |
| address | 123 Main St, Austin TX |
| emergency_contact | +1 512 000 0000 |

**hours**
| A: Day | B: Open | C: Close | D: Closed |
|---|---|---|---|
| Monday | 09:00 | 17:00 | FALSE |
| Sunday | | | TRUE |

**services**
| A: Service | B: Duration (min) | C: Price | D: Description |
|---|---|---|---|
| Cleaning | 60 | $120 | Full cleaning and checkup |
| Whitening | 90 | $300 | In-chair laser whitening |

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
GOOGLE_SHEETS_AVAILABILITY_ID   1OLbY1hFO1jZtnyY2lXC3YuEV41MbdyaXrtsO5K-lag8
GOOGLE_SHEETS_CONFIG_ID         1mHKfp1T2ExVBgQ4nO4cBmU8VdWU7HgsSwKo7TM8eBMc
GOOGLE_CREDENTIALS_PATH         google-credentials.json  (service account)

# Server
SERVER_URL                https://your-vps-domain.com  (no trailing slash)
```

## First-Time Setup Checklist

### 1. Python environment
```bash
cd dental-receptionist
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Google Cloud service account
1. console.cloud.google.com → New project → Enable **Google Sheets API**
2. IAM → Service Accounts → Create → Download JSON → save as `google-credentials.json`
3. Share both Google Sheets with the service account email (Editor role)

### 3. Twilio setup
1. Buy a US phone number in Twilio Console
2. Create an API Key (Standard) → note SID + Secret
3. Create a TwiML App:
   - Voice Request URL: `https://YOUR_SERVER_URL/twilio/voice`  (POST)
   - Note the App SID
4. Enable WhatsApp Sandbox: Twilio Console → Messaging → Try WhatsApp
5. Send the join code from your WhatsApp to activate the sandbox

### 4. Run
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000/test-client` in Chrome → click **Call AI Receptionist**.

## Call Flow (step by step)
1. Patient calls Twilio number / browser client connects
2. Twilio POSTs to `/twilio/voice` → server returns TwiML with `<Connect><Stream>`
3. Twilio opens WebSocket to `/ws/stream`
4. `CallSession.run()` starts two concurrent tasks:
   - `_twilio_receiver` — reads Twilio WS messages, forwards audio bytes to Deepgram
   - `_response_handler` — waits for transcripts, generates responses
5. On stream start → `__GREET__` sentinel → greeting spoken → Deepgram starts
6. Patient speaks → Deepgram streams transcripts → on `speech_final` → queue
7. `_respond()` streams Claude → sentence-detect → ElevenLabs → Twilio audio
8. On booking tool call → sheet update → SMS → WhatsApp → confirmation message
9. Back to listening

## Customising the AI
Edit **Spreadsheet B** (Config) — no code changes needed:
- Change hours → AI will only offer slots in those hours
- Add/remove services → AI will quote updated prices and descriptions
- Change clinic name/dentist name → greeting and responses update automatically

To change the AI's tone or add rules: edit `build_system_prompt()` in
`services/claude_service.py` (the "SPEAKING RULES" section).

## Demo Checklist (Zoom call with US client)
- [ ] `uvicorn main:app --reload` running on VPS
- [ ] Google Sheets populated with test slots
- [ ] Browser: `localhost:8000/test-client` loaded in Chrome
- [ ] Microphone permission granted
- [ ] Walk-through script:
  1. Click Call → AI greets
  2. Say "I need a cleaning next Tuesday"
  3. AI offers slots, patient picks one
  4. AI confirms name + phone
  5. Booking confirmed → show Sheet A updated + SMS sent

## Common Issues
| Problem | Fix |
|---|---|
| `pydantic_settings` import error | `pip install pydantic-settings` |
| Deepgram not connecting | Check `DEEPGRAM_API_KEY` in .env |
| No audio from ElevenLabs | Verify `ELEVENLABS_VOICE_ID` is a valid voice ID |
| Twilio 11200 webhook error | SERVER_URL in TwiML App is wrong or server not running |
| Google Sheets 403 | Service account not shared on the spreadsheet |
| WhatsApp not delivered | Must join sandbox first (send join code from your phone) |
| Browser client: "Device not ready" | Check browser console — usually a CORS or token issue |

## Deployment
Server runs on VPS with a fixed URL. Set `SERVER_URL` in `.env` to the VPS domain and point the Twilio TwiML App Voice URL to `SERVER_URL/twilio/voice`.
