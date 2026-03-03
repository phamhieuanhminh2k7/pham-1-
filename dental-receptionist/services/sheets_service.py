"""
Google Sheets service.

Spreadsheet A — Appointments  (GOOGLE_SHEETS_AVAILABILITY_ID)
  Sheet "Appointments" — ONLY booked appointments.  Available slots are generated
  dynamically from business hours minus what is already booked.
    Col A: Date          (YYYY-MM-DD)
    Col B: Time          (HH:MM, 24-hour)
    Col C: Duration      (minutes, number — pulled from the service booked)
    Col D: Patient Name
    Col E: Patient Phone
    Col F: Service

Spreadsheet B — Config  (GOOGLE_SHEETS_CONFIG_ID)
  Sheet "business_info":  key/value (Col A = key, Col B = value)
    clinic_name, dentist_name, address, emergency_contact
    max_concurrent       — simultaneous appointments per time slot (default 1)
    booking_window_days  — how many days ahead patients can book (default 7)
    NOTE: slot_duration removed — each service defines its own duration.
  Sheet "hours":
    Col A: Day  Col B: Open (HH:MM)  Col C: Close (HH:MM)  Col D: Closed (TRUE/FALSE)
  Sheet "services":
    Col A: Service  Col B: Duration (min)  Col C: Price  Col D: Description
"""

import logging
from datetime import date as date_type, timedelta, datetime
from functools import lru_cache
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@lru_cache(maxsize=1)
def _get_service():
    creds = Credentials.from_service_account_file(
        settings.google_credentials_path, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds).spreadsheets()


# ─── Appointments sheet (Sheet A) ─────────────────────────────────────────────

def get_booked_slots() -> list[dict]:
    """Read all existing booked appointments from Sheet A (date, time, duration)."""
    svc = _get_service()
    result = (
        svc.values()
        .get(
            spreadsheetId=settings.google_sheets_availability_id,
            range="Appointments!A2:F",
        )
        .execute()
    )
    rows = result.get("values", [])
    booked = []
    for row in rows:
        if len(row) >= 2:
            try:
                duration = int(row[2]) if len(row) > 2 and row[2].strip().isdigit() else 30
            except (ValueError, TypeError):
                duration = 30
            booked.append({
                "date": row[0].strip(),
                "time": row[1].strip(),
                "duration": duration,
            })
    return booked


def _to_minutes(time_str: str) -> int:
    """Convert 'HH:MM' to total minutes since midnight."""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def _slot_overlaps_booking(slot_start_min: int, booking: dict) -> bool:
    """Return True if an existing booking occupies the given slot start time."""
    try:
        b_start = _to_minutes(booking["time"])
        b_end = b_start + booking["duration"]
        # A booking at 09:00 for 60 min blocks 09:00 → 09:59 (not 10:00)
        return b_start <= slot_start_min < b_end
    except (ValueError, KeyError, TypeError):
        return False


def get_available_slots(
    business_info: dict,
    hours: list[dict],
    services: list[dict],
) -> list[dict]:
    """
    Dynamically generate available appointment start times.

    Slot interval = shortest service duration (auto-derived, no manual setting needed).
    A slot is blocked when the number of bookings that overlap it reaches max_concurrent.
    Includes close_time per slot so Claude can filter by service duration.
    """
    # Derive slot interval from the shortest service duration
    durations = []
    for s in services:
        try:
            durations.append(int(s["duration"]))
        except (ValueError, TypeError, KeyError):
            pass
    slot_interval = min(durations) if durations else 30

    try:
        max_concurrent = int(business_info.get("max_concurrent", 1))
        days_ahead = int(business_info.get("booking_window_days", 7))
    except (ValueError, TypeError):
        max_concurrent, days_ahead = 1, 7

    booked = get_booked_slots()
    available = []
    today = date_type.today()

    for day_offset in range(1, days_ahead + 1):
        check_date = today + timedelta(days=day_offset)
        day_name = check_date.strftime("%A")

        day_hours = next(
            (h for h in hours if h["day"].strip().lower() == day_name.lower()),
            None,
        )
        if not day_hours or day_hours.get("closed"):
            continue

        try:
            open_min = _to_minutes(day_hours["open"])
            close_min = _to_minutes(day_hours["close"])
        except (ValueError, KeyError):
            continue

        date_str = check_date.strftime("%Y-%m-%d")
        day_booked = [b for b in booked if b["date"] == date_str]

        current = open_min
        while current < close_min:
            time_str = f"{current // 60:02d}:{current % 60:02d}"

            overlap_count = sum(
                1 for b in day_booked if _slot_overlaps_booking(current, b)
            )

            if overlap_count < max_concurrent:
                available.append({
                    "date": date_str,
                    "time": time_str,
                    "close_time": day_hours["close"],  # Claude uses this for duration-aware filtering
                })

            current += slot_interval

    return available


def book_appointment(
    patient_name: str,
    patient_phone: str,
    date: str,
    time: str,
    service: str,
    duration: str,
) -> None:
    """Append a new booked appointment row to Sheet A."""
    svc = _get_service()
    svc.values().append(
        spreadsheetId=settings.google_sheets_availability_id,
        range="Appointments!A:F",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[date, time, duration, patient_name, patient_phone, service]]},
    ).execute()
    logger.info(f"Booked: {patient_name} on {date} at {time} for {service} ({duration} min)")


# ─── Config sheet (Sheet B) ────────────────────────────────────────────────────

def _read_range(spreadsheet_id: str, range_: str) -> list[list[Any]]:
    svc = _get_service()
    result = svc.values().get(spreadsheetId=spreadsheet_id, range=range_).execute()
    return result.get("values", [])


def get_business_info() -> dict:
    rows = _read_range(settings.google_sheets_config_id, "business_info!A2:B")
    return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}


def get_hours() -> list[dict]:
    rows = _read_range(settings.google_sheets_config_id, "hours!A2:D")
    hours = []
    for row in rows:
        if len(row) >= 3:
            hours.append({
                "day": row[0],
                "open": row[1],
                "close": row[2],
                "closed": row[3].strip().upper() == "TRUE" if len(row) > 3 else False,
            })
    return hours


def get_services() -> list[dict]:
    rows = _read_range(settings.google_sheets_config_id, "services!A2:D")
    services = []
    for row in rows:
        if len(row) >= 1:
            services.append({
                "name": row[0],
                "duration": row[1] if len(row) > 1 else "",
                "price": row[2] if len(row) > 2 else "",
                "description": row[3] if len(row) > 3 else "",
            })
    return services


def get_full_context() -> dict:
    """Fetch everything the AI needs in one call."""
    business_info = get_business_info()
    hours = get_hours()
    services = get_services()
    return {
        "business_info": business_info,
        "hours": hours,
        "services": services,
        "available_slots": get_available_slots(business_info, hours, services),
    }
