"""
Twilio SMS + WhatsApp notifications.

SMS  → confirmation to the patient after booking.
WhatsApp → alert to the dentist (Twilio sandbox number).
"""

import logging

from twilio.rest import Client

from config import settings

logger = logging.getLogger(__name__)

_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)


def send_sms_confirmation(booking: dict) -> None:
    """Send SMS to the patient confirming their appointment."""
    body = (
        f"Hi {booking['patient_name']}! Your appointment is confirmed:\n"
        f"  Date:    {booking['date']}\n"
        f"  Time:    {booking['time']}\n"
        f"  Service: {booking['service']}\n"
        f"Reply CANCEL to cancel.  See you soon!"
    )
    try:
        msg = _client.messages.create(
            body=body,
            from_=settings.twilio_phone_number,
            to=booking["patient_phone"],
        )
        logger.info(f"SMS sent to {booking['patient_phone']} — SID {msg.sid}")
    except Exception as exc:
        logger.error(f"SMS failed: {exc}")


def send_whatsapp_notification(booking: dict) -> None:
    """Send WhatsApp message to the dentist about the new booking."""
    body = (
        f"📅 *New Booking Alert*\n"
        f"Patient: {booking['patient_name']}\n"
        f"Phone:   {booking['patient_phone']}\n"
        f"Date:    {booking['date']}\n"
        f"Time:    {booking['time']}\n"
        f"Service: {booking['service']}"
    )
    try:
        msg = _client.messages.create(
            body=body,
            from_=settings.twilio_whatsapp_from,
            to=settings.dentist_whatsapp,
        )
        logger.info(f"WhatsApp sent — SID {msg.sid}")
    except Exception as exc:
        logger.error(f"WhatsApp failed: {exc}")
