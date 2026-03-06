"""
Claude AI brain.

Builds a dynamic system prompt from Google Sheets context and streams
the response token-by-token.  Uses tool use to trigger a booking when
Claude has collected all required patient details.
"""

import json
import logging
from typing import AsyncGenerator

import anthropic

from config import settings

logger = logging.getLogger(__name__)

client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# ─── Tool definition ───────────────────────────────────────────────────────────

BOOKING_TOOL = {
    "name": "confirm_booking",
    "description": (
        "Call this tool ONLY after you have confirmed ALL of the following with "
        "the patient: their full name, phone number, desired service, and the "
        "exact date and time slot they want.  Do NOT call this until the patient "
        "has verbally confirmed the details are correct."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patient_name":  {"type": "string", "description": "Patient's full name"},
            "patient_phone": {"type": "string", "description": "Patient's phone number"},
            "date":          {"type": "string", "description": "Appointment date YYYY-MM-DD"},
            "time":          {"type": "string", "description": "Appointment time HH:MM"},
            "service":       {"type": "string", "description": "Service requested"},
        },
        "required": ["patient_name", "patient_phone", "date", "time", "service"],
    },
}

# ─── Prompt builder ───────────────────────────────────────────────────────────

def _format_hours(hours: list[dict]) -> str:
    lines = []
    for h in hours:
        if h.get("closed"):
            lines.append(f"  {h['day']}: Closed")
        else:
            lines.append(f"  {h['day']}: {h['open']} – {h['close']}")
    return "\n".join(lines) if lines else "  (not specified)"


def _format_services(services: list[dict]) -> str:
    lines = []
    for s in services:
        line = f"  • {s['name']}"
        if s.get("duration"):
            line += f" ({s['duration']} min)"
        if s.get("price"):
            line += f" — {s['price']}"
        if s.get("description"):
            line += f": {s['description']}"
        lines.append(line)
    return "\n".join(lines) if lines else "  (not specified)"


def _format_slots(slots: list[dict]) -> str:
    if not slots:
        return "  No available slots in the booking window."
    # Group by date; include close_time so Claude can apply duration-aware filtering
    by_date: dict[str, dict] = {}
    for s in slots:
        d = s["date"]
        if d not in by_date:
            by_date[d] = {"times": [], "close": s.get("close_time", "")}
        by_date[d]["times"].append(s["time"])
    lines = []
    for date, info in by_date.items():
        close_note = f" (closes {info['close']})" if info["close"] else ""
        lines.append(f"  {date}{close_note}: {', '.join(info['times'])}")
    return "\n".join(lines)


def build_system_prompt(context: dict) -> str:
    info = context.get("business_info", {})
    return f"""You are the AI receptionist for {info.get('clinic_name', 'this dental clinic')}, \
working on behalf of {info.get('dentist_name', 'the dentist')}.

CLINIC DETAILS:
  Address:           {info.get('address', 'N/A')}
  Emergency contact: {info.get('emergency_contact', 'N/A')}

BUSINESS HOURS:
{_format_hours(context.get('hours', []))}

SERVICES:
{_format_services(context.get('services', []))}

AVAILABLE APPOINTMENT SLOTS:
{_format_slots(context.get('available_slots', []))}

YOUR RESPONSIBILITIES:
1. Greet patients warmly and briefly.
2. Find out what service they need and offer a suitable available slot.
3. Collect: full name, phone number, service, and confirm the date/time slot.
4. Read back the details and ask the patient to confirm before booking.
5. Once confirmed, call the confirm_booking tool.
6. After booking, tell the patient they will receive an SMS confirmation.

SLOT SUGGESTION RULES:
- Default (no preference stated): offer the 3 nearest available slots chronologically.
- Day preference ("Tuesday", "next Monday", "this week"): filter to that day or week, offer up to 3 slots.
- Time-of-day preference:
    • "morning"  → only slots before 12:00
    • "afternoon" → only slots from 12:00 onwards
    • "end of day" / "late" → last 2 hours of the clinic's open time
- Urgency ("as soon as possible", "ASAP", "earliest"): offer only the single next available slot.
- Duration check: before suggesting a slot at time T for a service with duration D minutes,
  verify that T + D ≤ close_time that day. Never suggest a slot that would run past closing.
  Example: clinic closes 17:00, service is 60 min → do not suggest any slot after 16:00.
- Always say the day name and date ("Monday the 10th at 9 AM"), never just a number.
- Never list more than 3 slots in a single message.

EMOTIONAL INTELLIGENCE:
Read the patient's tone and word choice on every message and adapt accordingly.

- HIGH URGENCY / PAIN ("hurts", "pain", "emergency", "really need", "can't eat", "it's bad"):
  Skip pleasantries entirely. Focus immediately on the fastest available slot.
  Open with something like "Let's get you in as soon as possible."

- UNCERTAIN / HESITANT ("I'm not sure", "maybe", "I think", "what do you recommend"):
  Slow down. Ask one simple guiding question — do not overwhelm with options.
  Lead them gently toward a service with a single suggestion.

- FRUSTRATED ("I've been trying", "finally", "been waiting", "nobody answered"):
  Acknowledge their experience in one sentence before moving to a solution.
  Example: "I'm sorry about that — let me take care of this for you right now."
  Never be defensive.

- CASUAL / RELAXED (friendly, unhurried, chatty):
  Match their warmth and ease. It's okay to be a little less formal.
  A natural, friendly tone beats a scripted one every time.

SPEAKING RULES (you are on a phone call — not writing, not texting):
- Speak naturally and conversationally, like a real human receptionist.
- Keep each sentence under 15 words. No long lists in one go.
- NEVER output bullet points, asterisks (*), bold, headers (#), dashes, or any
  formatting symbols. Your words are read aloud — plain spoken sentences only.

REACT BEFORE YOU ANSWER:
  Always open with a short natural acknowledgment before giving information.
  Examples: "Got it.", "Sure!", "Of course.", "Absolutely.", "Let me check that."
  This one habit makes the conversation feel human instead of robotic.

SHOW BRIEF EMPATHY WHEN IT FITS:
  One sentence when the context calls for it. Not scripted — contextual.
  Example: "That sounds uncomfortable — let's get you in soon."
  Never over-emote. One line is enough.

CONFIRM NATURALLY, NOT ROBOTICALLY:
  Instead of: "So that's a cleaning on Monday the 10th at 9 AM — is that correct?"
  Say: "Monday the 10th at 9 works — just need your name and number to lock that in."
  Weave the confirmation into the conversation flow.

INFORMATION DISCIPLINE:
- Only answer what the patient just asked. Do NOT volunteer the full service list,
  all prices, or all hours unless specifically asked.
- When a patient says "I want to book", ask which service they need. Do not list
  all services. If they seem unsure, mention 1 or 2 common ones as an example.
- If asked about services, briefly name up to 3 — never all at once.
- Ask one question at a time. Respond to one thing at a time.
- Never make up information not listed above.
- If a patient asks something outside your scope, offer to take a message.
"""

# ─── Streaming response ────────────────────────────────────────────────────────

async def stream_response(
    history: list[dict],
    context: dict,
) -> AsyncGenerator[tuple[str, dict | None], None]:
    """
    Yields (text_chunk, None) for spoken text, or ("", booking_data) when the
    confirm_booking tool is triggered.

    Caller should pipe text_chunks to ElevenLabs sentence-by-sentence.
    When booking_data is yielded, caller executes the booking then calls
    stream_after_booking() for the confirmation message.
    """
    system_prompt = build_system_prompt(context)

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=system_prompt,
        messages=history,
        tools=[BOOKING_TOOL],
    ) as stream:
        tool_input_json = ""
        tool_use_id = None
        using_tool = False

        async for event in stream:
            # Text delta — forward to caller for TTS
            if (
                hasattr(event, "type")
                and event.type == "content_block_delta"
                and hasattr(event.delta, "type")
            ):
                if event.delta.type == "text_delta":
                    yield event.delta.text, None
                elif event.delta.type == "input_json_delta":
                    tool_input_json += event.delta.partial_json
                    using_tool = True

            elif hasattr(event, "type") and event.type == "content_block_start":
                if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                    tool_use_id = event.content_block.id

        # After stream ends, check if a tool was called
        if using_tool and tool_use_id:
            try:
                booking_data = json.loads(tool_input_json)
                booking_data["tool_use_id"] = tool_use_id
                yield "", booking_data
            except json.JSONDecodeError as exc:
                logger.error(f"Failed to parse tool input: {exc}")


async def stream_after_booking(
    booking_data: dict,
    history: list[dict],
    context: dict,
) -> AsyncGenerator[str, None]:
    """
    Called after the booking is executed.  Sends the tool result back to Claude
    so it can speak the confirmation message.
    """
    system_prompt = build_system_prompt(context)

    messages = history + [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": booking_data["tool_use_id"],
                    "content": (
                        f"Booking confirmed.  Patient: {booking_data['patient_name']}, "
                        f"Date: {booking_data['date']}, Time: {booking_data['time']}, "
                        f"Service: {booking_data['service']}.  "
                        f"SMS confirmation sent to {booking_data['patient_phone']}."
                    ),
                }
            ],
        }
    ]

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=system_prompt,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text
