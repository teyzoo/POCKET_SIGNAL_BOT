from __future__ import annotations

import os
from typing import Final


# ============================================================
# HELPERS
# ============================================================

def get_str(
    name: str,
    default: str | None = None,
) -> str | None:
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value if value else default


def get_bool(
    name: str,
    default: bool = False,
) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def get_int(
    name: str,
    default: int,
) -> int:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return int(value.strip())
    except ValueError:
        return default


def get_float(
    name: str,
    default: float,
) -> float:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return float(value.strip())
    except ValueError:
        return default


# ============================================================
# TELEGRAM
# ============================================================

BOT_TOKEN: str | None = get_str("BOT_TOKEN")

OWNER_ID: int | None = None

_owner_raw = get_str("OWNER_ID")

if _owner_raw:
    try:
        OWNER_ID = int(_owner_raw)
    except ValueError:
        OWNER_ID = None


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL: str | None = get_str("DATABASE_URL")


def normalize_database_url(
    url: str | None,
) -> str | None:
    """
    Приводит Render PostgreSQL URL к asyncpg.

    Поддерживает:
    postgres://
    postgresql://
    postgresql+psycopg2://
    postgresql+asyncpg://
    """

    if not url:
        return None

    url = url.strip()

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    if url.startswith("postgresql+psycopg2://"):
        url = (
            "postgresql+asyncpg://"
            + url[len("postgresql+psycopg2://"):]
        )

    elif url.startswith("postgresql://"):
        url = (
            "postgresql+asyncpg://"
            + url[len("postgresql://"):]
        )

    return url


DATABASE_URL = normalize_database_url(
    DATABASE_URL
)


# ============================================================
# ACCESS / BOT SETTINGS
# ============================================================

JOIN_REQUIRED: bool = get_bool(
    "JOIN_REQUIRED",
    False,
)


# ============================================================
# SIGNAL SETTINGS
# ============================================================

MIN_SIGNAL_SCORE: float = get_float(
    "MIN_SIGNAL_SCORE",
    75.0,
)

MIN_PROBABILITY: float = get_float(
    "MIN_PROBABILITY",
    75.0,
)

SCAN_INTERVAL: int = get_int(
    "SCAN_INTERVAL",
    60,
)


# ============================================================
# TIMEZONE
# ============================================================

TIMEZONE: str = get_str(
    "TIMEZONE",
    "Europe/Moscow",
) or "Europe/Moscow"


# ============================================================
# POCKET OPTION
# ============================================================

PO_EMAIL: str | None = get_str(
    "PO_EMAIL"
)

PO_PASSWORD: str | None = get_str(
    "PO_PASSWORD"
)

PO_SSID: str | None = get_str(
    "PO_SSID"
)

PO_AUTO_LOGIN: bool = get_bool(
    "PO_AUTO_LOGIN",
    True,
)

PO_DEMO: bool = get_bool(
    "PO_DEMO",
    True,
)

PO_LOGIN_URL: str = get_str(
    "PO_LOGIN_URL",
    "https://pocketoption.com/en/login/",
) or "https://pocketoption.com/en/login/"


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAMES: Final[tuple[int, ...]] = (
    1,
    2,
    3,
    5,
    10,
    15,
    20,
)

# Совместимость со старым main.py.
timeframes = list(TIMEFRAMES)


# ============================================================
# OTC PAIRS
# ============================================================

PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("EUR/USD OTC", "EURUSD_otc"),
    ("GBP/USD OTC", "GBPUSD_otc"),
    ("USD/JPY OTC", "USDJPY_otc"),
    ("USD/CHF OTC", "USDCHF_otc"),
    ("AUD/USD OTC", "AUDUSD_otc"),
    ("USD/CAD OTC", "USDCAD_otc"),
    ("NZD/USD OTC", "NZDUSD_otc"),
    ("EUR/GBP OTC", "EURGBP_otc"),
    ("EUR/JPY OTC", "EURJPY_otc"),
    ("GBP/JPY OTC", "GBPJPY_otc"),
    ("AUD/JPY OTC", "AUDJPY_otc"),
    ("AUD/CAD OTC", "AUDCAD_otc"),
    ("AUD/CHF OTC", "AUDCHF_otc"),
    ("AUD/NZD OTC", "AUDNZD_otc"),
    ("CAD/CHF OTC", "CADCHF_otc"),
    ("CAD/JPY OTC", "CADJPY_otc"),
    ("CHF/JPY OTC", "CHFJPY_otc"),
    ("EUR/CHF OTC", "EURCHF_otc"),
    ("EUR/NZD OTC", "EURNZD_otc"),
    ("GBP/AUD OTC", "GBPAUD_otc"),
    ("NZD/JPY OTC", "NZDJPY_otc"),
)

# Совместимость со старым кодом.
pairs = list(PAIRS)


# ============================================================
# CONSTANTS
# ============================================================

ANY_PAIR: Final[str] = "ANY"


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. "
        "Add BOT_TOKEN to Render Environment Variables."
    )


if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is missing. "
        "Add DATABASE_URL to Render Environment Variables."
    )


if MIN_SIGNAL_SCORE < 0:
    MIN_SIGNAL_SCORE = 0.0

if MIN_SIGNAL_SCORE > 100:
    MIN_SIGNAL_SCORE = 100.0


if MIN_PROBABILITY < 0:
    MIN_PROBABILITY = 0.0

if MIN_PROBABILITY > 100:
    MIN_PROBABILITY = 100.0


if SCAN_INTERVAL < 10:
    SCAN_INTERVAL = 10


# ============================================================
# LOWERCASE COMPATIBILITY VARIABLES
# ============================================================

bot_token = BOT_TOKEN
owner_id = OWNER_ID
database_url = DATABASE_URL

join_required = JOIN_REQUIRED

min_signal_score = MIN_SIGNAL_SCORE
min_probability = MIN_PROBABILITY

scan_interval = SCAN_INTERVAL

timezone = TIMEZONE

po_email = PO_EMAIL
po_password = PO_PASSWORD
po_ssid = PO_SSID
po_auto_login = PO_AUTO_LOGIN
po_demo = PO_DEMO
po_login_url = PO_LOGIN_URL
