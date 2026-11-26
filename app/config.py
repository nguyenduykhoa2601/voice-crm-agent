"""Environment-driven settings.

Values are read from the process environment, with a local `.env` file loaded
first (if present) via python-dotenv. Real environment variables win over `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env", override=False)


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_live_model: str  # empty → auto-select
    gemini_text_model: str
    espo_url: str
    max_sessions: int
    headless: bool
    port: int
    # Voice / Live tuning
    live_voice: str
    vad_start_sensitivity: str  # "high" | "low"
    vad_end_sensitivity: str  # "high" | "low"
    vad_prefix_padding_ms: int
    vad_silence_duration_ms: int
    live_async_tools: bool  # NON_BLOCKING function declarations (model dependent)
    live_text_mode: str  # "auto" | "client_content" | "realtime"
    tool_timeout_s: int  # outer guard; the tool layer has its own per-step timeouts
    session_max_minutes: int
    idle_timeout_s: int  # end the session after this long without user audio/text
    metrics_path: Path

    @property
    def has_api_key(self) -> bool:
        return bool(self.gemini_api_key)


def load_settings() -> Settings:
    metrics_path = Path(_str("METRICS_PATH", "data/metrics.jsonl"))
    if not metrics_path.is_absolute():
        metrics_path = PROJECT_ROOT / metrics_path
    return Settings(
        gemini_api_key=_str("GEMINI_API_KEY") or _str("GOOGLE_API_KEY"),
        gemini_live_model=_str("GEMINI_LIVE_MODEL"),
        gemini_text_model=_str("GEMINI_TEXT_MODEL", "gemini-2.5-flash"),
        espo_url=_str("ESPO_URL", "https://demo.us.espocrm.com").rstrip("/"),
        # Render's free tier has 512 MB RAM: default to one browser context there.
        max_sessions=max(1, _int("MAX_SESSIONS", 1 if os.getenv("RENDER") else 3)),
        headless=_bool("HEADLESS", True),
        port=_int("PORT", 7860),
        live_voice=_str("LIVE_VOICE", "Kore"),
        vad_start_sensitivity=_str("VAD_START_SENSITIVITY", "high").lower(),
        vad_end_sensitivity=_str("VAD_END_SENSITIVITY", "high").lower(),
        vad_prefix_padding_ms=_int("VAD_PREFIX_PADDING_MS", 200),
        vad_silence_duration_ms=_int("VAD_SILENCE_DURATION_MS", 600),
        live_async_tools=_bool("LIVE_ASYNC_TOOLS", False),
        live_text_mode=_str("LIVE_TEXT_MODE", "auto").lower(),
        tool_timeout_s=_int("TOOL_TIMEOUT_S", 50),
        session_max_minutes=_int("SESSION_MAX_MINUTES", 15),
        idle_timeout_s=_int("IDLE_TIMEOUT_S", 180),
        metrics_path=metrics_path,
    )


settings = load_settings()
