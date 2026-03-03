"""
Run this before starting the server to verify every service is reachable.

  python test_setup.py

Each check prints ✅ or ❌ with a reason.
"""

import asyncio
import os
import sys

# Load .env before importing config
from dotenv import load_dotenv
load_dotenv()

PASS = "✅"
FAIL = "❌"


# ── 1. Google Sheets ──────────────────────────────────────────────────────────
def check_google_sheets():
    print("\n── Google Sheets ──")
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "google-credentials.json")
    if not os.path.exists(creds_path):
        print(f"{FAIL} {creds_path} not found — download your service account JSON and place it here")
        return False

    try:
        from services.sheets_service import get_business_info, get_hours, get_services, get_available_slots
        info = get_business_info()
        print(f"{PASS} Config sheet readable — keys found: {list(info.keys()) or '(empty, add rows)'}")
        slots = get_available_slots(info, get_hours(), get_services())
        print(f"{PASS} Availability sheet readable — {len(slots)} available slot(s)")
        return True
    except Exception as e:
        print(f"{FAIL} {e}")
        return False


# ── 2. Anthropic / Claude ─────────────────────────────────────────────────────
async def check_claude():
    print("\n── Claude (Anthropic) ──")
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=20,
            messages=[{"role": "user", "content": "Say: OK"}],
        )
        reply = msg.content[0].text.strip()
        print(f"{PASS} Claude responded: {reply!r}")
        return True
    except Exception as e:
        print(f"{FAIL} {e}")
        return False


# ── 3. ElevenLabs ─────────────────────────────────────────────────────────────
async def check_elevenlabs():
    print("\n── ElevenLabs ──")
    try:
        import httpx
        voice_id = os.getenv("ELEVENLABS_VOICE_ID")
        api_key  = os.getenv("ELEVENLABS_API_KEY")
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            "?output_format=ulaw_8000&optimize_streaming_latency=4"
        )
        headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
        payload = {"text": "Hello.", "model_id": "eleven_turbo_v2_5"}

        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as r:
                if r.status_code == 200:
                    first_chunk = await r.aread()
                    print(f"{PASS} ElevenLabs streaming OK — {len(first_chunk)} bytes received")
                    return True
                else:
                    body = await r.aread()
                    print(f"{FAIL} HTTP {r.status_code}: {body[:200]}")
                    return False
    except Exception as e:
        print(f"{FAIL} {e}")
        return False


# ── 4. Deepgram ───────────────────────────────────────────────────────────────
async def check_deepgram():
    print("\n── Deepgram ──")
    try:
        from deepgram import DeepgramClient
        client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
        # Simple REST balance check to verify the key works
        import httpx
        async with httpx.AsyncClient() as http:
            r = await http.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {os.getenv('DEEPGRAM_API_KEY')}"},
            )
        if r.status_code == 200:
            print(f"{PASS} Deepgram API key valid")
            return True
        else:
            print(f"{FAIL} HTTP {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"{FAIL} {e}")
        return False


# ── 5. Twilio ─────────────────────────────────────────────────────────────────
def check_twilio():
    print("\n── Twilio ──")
    try:
        from twilio.rest import Client
        client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        account = client.api.accounts(os.getenv("TWILIO_ACCOUNT_SID")).fetch()
        print(f"{PASS} Twilio account: {account.friendly_name} ({account.status})")

        # Check TwiML App exists
        app_sid = os.getenv("TWILIO_TWIML_APP_SID")
        app = client.applications(app_sid).fetch()
        print(f"{PASS} TwiML App: {app.friendly_name!r} — Voice URL: {app.voice_url!r}")

        expected_url = os.getenv("SERVER_URL", "") + "/twilio/voice"
        if app.voice_url != expected_url:
            print(f"  ⚠️  Voice URL mismatch!")
            print(f"     TwiML App has: {app.voice_url!r}")
            print(f"     Expected:      {expected_url!r}")
            print(f"     Update it at: console.twilio.com → Voice → TwiML Apps")
        return True
    except Exception as e:
        print(f"{FAIL} {e}")
        return False


# ── 6. ngrok reachability ─────────────────────────────────────────────────────
async def check_ngrok():
    print("\n── ngrok / Server URL ──")
    server_url = os.getenv("SERVER_URL", "")
    if not server_url or "xxxx" in server_url:
        print(f"{FAIL} SERVER_URL not set in .env")
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(server_url + "/")
        if r.status_code == 200:
            print(f"{PASS} {server_url} is reachable (server is running)")
            return True
        else:
            print(f"⚠️  {server_url} returned HTTP {r.status_code} — is the server running?")
            return False
    except Exception as e:
        print(f"⚠️  {server_url} not reachable — start the server first, then re-run this check")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 55)
    print("  Dental AI Receptionist — Setup Check")
    print("=" * 55)

    results = {}
    results["Google Sheets"] = check_google_sheets()
    results["Claude"]        = await check_claude()
    results["ElevenLabs"]    = await check_elevenlabs()
    results["Deepgram"]      = await check_deepgram()
    results["Twilio"]        = check_twilio()
    results["ngrok/Server"]  = await check_ngrok()

    print("\n" + "=" * 55)
    print("  Summary")
    print("=" * 55)
    all_ok = True
    for name, ok in results.items():
        icon = PASS if ok else FAIL
        print(f"  {icon}  {name}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n🎉  All checks passed — ready to test!")
        print("   Open: http://localhost:8000/test-client")
    else:
        print("\n⚠️  Fix the failing checks above, then re-run.")

    print()


if __name__ == "__main__":
    asyncio.run(main())
