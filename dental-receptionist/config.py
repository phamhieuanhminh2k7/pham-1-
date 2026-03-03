from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    twilio_api_key_sid: str
    twilio_api_key_secret: str
    twilio_twiml_app_sid: str
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    dentist_whatsapp: str

    # AI services
    deepgram_api_key: str
    anthropic_api_key: str
    elevenlabs_api_key: str
    # Free-tier default: "Rachel" — a clear, professional voice available on all plans.
    # Override in .env with any voice ID from elevenlabs.io/voices
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"

    # Google Sheets
    google_sheets_availability_id: str
    google_sheets_config_id: str
    google_credentials_path: str = "google-credentials.json"

    # Server
    server_url: str  # e.g. https://xxxx.ngrok-free.app

    class Config:
        env_file = ".env"


settings = Settings()
