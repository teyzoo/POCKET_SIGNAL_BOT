from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# ============================================================
# ENV HELPERS
# ============================================================

def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip()


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = _env(name)
        if value:
            return value

    return default


def _env_int(name: str, default: int = 0) -> int:
    value = _env(name)

    if not value:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float = 0.0) -> float:
    value = _env(name)

    if not value:
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)

    if not value:
        return default

    return value.lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def normalize_database_url(value: str) -> str:
    value = value.strip()

    if not value:
        return ""

    if value.startswith("postgresql+asyncpg://"):
        return value

    prefixes = (
        "postgresql://",
        "postgres://",
        "postgresql+psycopg2://",
    )

    for prefix in prefixes:
        if value.startswith(prefix):
            return (
                "postgresql+asyncpg://"
                + value.split("://", 1)[1]
            )

    return value


# ============================================================
# OTC PAIRS
# ============================================================

PAIRS = [
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
]


OTC_SYMBOLS = {
    display_name: symbol
    for display_name, symbol in PAIRS
}

OTC_DISPLAY_NAMES = {
    symbol: display_name
    for display_name, symbol in PAIRS
}


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAMES = [
    1,
    2,
    3,
    5,
    10,
    15,
    20,
]


# ============================================================
# TELEGRAM
# ============================================================

BOT_TOKEN = _env("BOT_TOKEN")

OWNER_ID_RAW = _env("OWNER_ID")

OWNER_ID: Optional[int] = None

if OWNER_ID_RAW:
    try:
        OWNER_ID = int(OWNER_ID_RAW)
    except ValueError:
        OWNER_ID = None


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = normalize_database_url(
    _env("DATABASE_URL")
)


# ============================================================
# SIGNAL SETTINGS
# ============================================================

JOIN_REQUIRED = _env_bool(
    "JOIN_REQUIRED",
    False,
)

MIN_SIGNAL_SCORE = max(
    0.0,
    min(
        100.0,
        _env_float(
            "MIN_SIGNAL_SCORE",
            75.0,
        ),
    ),
)

MIN_PROBABILITY = max(
    0.0,
    min(
        100.0,
        _env_float(
            "MIN_PROBABILITY",
            75.0,
        ),
    ),
)

SCAN_INTERVAL = max(
    10,
    _env_int(
        "SCAN_INTERVAL",
        60,
    ),
)

TIMEZONE = _env(
    "TIMEZONE",
    "Europe/Moscow",
)


# ============================================================
# POCKET OPTION
# ============================================================

# Основные имена.
#
# Дополнительно поддерживаются:
# POCKET_OPTION_EMAIL
# POCKET_OPTION_PASSWORD
# POCKET_OPTION_SSID

PO_EMAIL = _env_first(
    "PO_EMAIL",
    "POCKET_OPTION_EMAIL",
)

PO_PASSWORD = _env_first(
    "PO_PASSWORD",
    "POCKET_OPTION_PASSWORD",
)

PO_SSID = _env_first(
    "PO_SSID",
    "POCKET_OPTION_SSID",
)

PO_AUTO_LOGIN = _env_bool(
    "PO_AUTO_LOGIN",
    True,
)

PO_DEMO = _env_bool(
    "PO_DEMO",
    True,
)

PO_LOGIN_URL = _env(
    "PO_LOGIN_URL",
    "https://pocketoption.com/en/login/",
)


# ============================================================
# ALIASES
# ============================================================

pairs = PAIRS
timeframes = TIMEFRAMES

otc_symbols = OTC_SYMBOLS
otc_display_names = OTC_DISPLAY_NAMES

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

ANY_PAIR = "ANY"


# ============================================================
# CONFIG OBJECT
# ============================================================

@dataclass(frozen=True)
class Config:

    BOT_TOKEN: str
    OWNER_ID: Optional[int]
    DATABASE_URL: str

    JOIN_REQUIRED: bool

    MIN_SIGNAL_SCORE: float
    MIN_PROBABILITY: float

    SCAN_INTERVAL: int
    TIMEZONE: str

    PO_EMAIL: str
    PO_PASSWORD: str
    PO_SSID: str

    PO_AUTO_LOGIN: bool
    PO_DEMO: bool
    PO_LOGIN_URL: str

    PAIRS: list
    TIMEFRAMES: list

    OTC_SYMBOLS: dict
    OTC_DISPLAY_NAMES: dict

    ANY_PAIR: str

    @property
    def bot_token(self):
        return self.BOT_TOKEN

    @property
    def owner_id(self):
        return self.OWNER_ID

    @property
    def database_url(self):
        return self.DATABASE_URL

    @property
    def join_required(self):
        return self.JOIN_REQUIRED

    @property
    def min_signal_score(self):
        return self.MIN_SIGNAL_SCORE

    @property
    def min_probability(self):
        return self.MIN_PROBABILITY

    @property
    def scan_interval(self):
        return self.SCAN_INTERVAL

    @property
    def timezone(self):
        return self.TIMEZONE

    @property
    def po_email(self):
        return self.PO_EMAIL

    @property
    def po_password(self):
        return self.PO_PASSWORD

    @property
    def po_ssid(self):
        return self.PO_SSID

    @property
    def po_auto_login(self):
        return self.PO_AUTO_LOGIN

    @property
    def po_demo(self):
        return self.PO_DEMO

    @property
    def po_login_url(self):
        return self.PO_LOGIN_URL

    @property
    def pairs(self):
        return self.PAIRS

    @property
    def timeframes(self):
        return self.TIMEFRAMES

    @property
    def otc_symbols(self):
        return self.OTC_SYMBOLS

    @property
    def otc_display_names(self):
        return self.OTC_DISPLAY_NAMES


# ============================================================
# GLOBAL CONFIG
# ============================================================

config = Config(
    BOT_TOKEN=BOT_TOKEN,
    OWNER_ID=OWNER_ID,
    DATABASE_URL=DATABASE_URL,

    JOIN_REQUIRED=JOIN_REQUIRED,

    MIN_SIGNAL_SCORE=MIN_SIGNAL_SCORE,
    MIN_PROBABILITY=MIN_PROBABILITY,

    SCAN_INTERVAL=SCAN_INTERVAL,
    TIMEZONE=TIMEZONE,

    PO_EMAIL=PO_EMAIL,
    PO_PASSWORD=PO_PASSWORD,
    PO_SSID=PO_SSID,

    PO_AUTO_LOGIN=PO_AUTO_LOGIN,
    PO_DEMO=PO_DEMO,
    PO_LOGIN_URL=PO_LOGIN_URL,

    PAIRS=PAIRS,
    TIMEFRAMES=TIMEFRAMES,

    OTC_SYMBOLS=OTC_SYMBOLS,
    OTC_DISPLAY_NAMES=OTC_DISPLAY_NAMES,

    ANY_PAIR=ANY_PAIR,
)


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured in Render."
    )

if BOT_TOKEN == ":":
    raise RuntimeError(
        "BOT_TOKEN is invalid."
    )

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured."
    )

if not DATABASE_URL.startswith(
    "postgresql+asyncpg://"
):
    raise RuntimeError(
        "DATABASE_URL must be a PostgreSQL "
        "URL compatible with asyncpg."
    )


# ============================================================
# POCKET OPTION VALIDATION
# ============================================================

if not PO_SSID and not (
    PO_EMAIL and PO_PASSWORD
):
    raise RuntimeError(
        "Pocket Option authentication is not configured. "
        "Set PO_SSID or PO_EMAIL + PO_PASSWORD "
        "in Render Environment Variables."
    )
