from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# HELPERS
# ============================================================

def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip()


def _env_int(
    name: str,
    default: int = 0,
) -> int:
    value = _env(name)

    if not value:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_float(
    name: str,
    default: float = 0.0,
) -> float:
    value = _env(name)

    if not value:
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_bool(
    name: str,
    default: bool = False,
) -> bool:
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


def normalize_database_url(
    value: str,
) -> str:
    """
    Render PostgreSQL URL compatibility.

    postgres://...
        -> postgresql+asyncpg://...

    postgresql://...
        -> postgresql+asyncpg://...

    postgresql+psycopg2://...
        -> postgresql+asyncpg://...
    """

    value = value.strip()

    if not value:
        return ""

    if value.startswith(
        "postgresql+asyncpg://"
    ):
        return value

    if value.startswith(
        "postgresql+psycopg2://"
    ):
        return (
            "postgresql+asyncpg://"
            + value.split(
                "://",
                1,
            )[1]
        )

    if value.startswith(
        "postgresql://"
    ):
        return (
            "postgresql+asyncpg://"
            + value.split(
                "://",
                1,
            )[1]
        )

    if value.startswith(
        "postgres://"
    ):
        return (
            "postgresql+asyncpg://"
            + value.split(
                "://",
                1,
            )[1]
        )

    return value


# ============================================================
# OTC PAIRS
# ============================================================

PAIRS = [
    (
        "EUR/USD OTC",
        "EURUSD_otc",
    ),
    (
        "GBP/USD OTC",
        "GBPUSD_otc",
    ),
    (
        "USD/JPY OTC",
        "USDJPY_otc",
    ),
    (
        "USD/CHF OTC",
        "USDCHF_otc",
    ),
    (
        "AUD/USD OTC",
        "AUDUSD_otc",
    ),
    (
        "USD/CAD OTC",
        "USDCAD_otc",
    ),
    (
        "NZD/USD OTC",
        "NZDUSD_otc",
    ),
    (
        "EUR/GBP OTC",
        "EURGBP_otc",
    ),
    (
        "EUR/JPY OTC",
        "EURJPY_otc",
    ),
    (
        "GBP/JPY OTC",
        "GBPJPY_otc",
    ),
    (
        "AUD/JPY OTC",
        "AUDJPY_otc",
    ),
    (
        "AUD/CAD OTC",
        "AUDCAD_otc",
    ),
    (
        "AUD/CHF OTC",
        "AUDCHF_otc",
    ),
    (
        "AUD/NZD OTC",
        "AUDNZD_otc",
    ),
    (
        "CAD/CHF OTC",
        "CADCHF_otc",
    ),
    (
        "CAD/JPY OTC",
        "CADJPY_otc",
    ),
    (
        "CHF/JPY OTC",
        "CHFJPY_otc",
    ),
    (
        "EUR/CHF OTC",
        "EURCHF_otc",
    ),
    (
        "EUR/NZD OTC",
        "EURNZD_otc",
    ),
    (
        "GBP/AUD OTC",
        "GBPAUD_otc",
    ),
    (
        "NZD/JPY OTC",
        "NZDJPY_otc",
    ),
]


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
# MAIN SETTINGS
# ============================================================

BOT_TOKEN = _env(
    "BOT_TOKEN"
)

OWNER_ID_RAW = _env(
    "OWNER_ID"
)

OWNER_ID: Optional[int]

if OWNER_ID_RAW:
    try:
        OWNER_ID = int(
            OWNER_ID_RAW
        )
    except ValueError:
        OWNER_ID = None
else:
    OWNER_ID = None


DATABASE_URL = normalize_database_url(
    _env("DATABASE_URL")
)


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

PO_EMAIL = _env(
    "PO_EMAIL"
)

PO_PASSWORD = _env(
    "PO_PASSWORD"
)

PO_SSID = _env(
    "PO_SSID"
)

PO_AUTO_LOGIN = _env_bool(
    "PO_AUTO_LOGIN",
    False,
)

PO_DEMO = _env_bool(
    "PO_DEMO",
    True,
)

PO_LOGIN_URL = _env(
    "PO_LOGIN_URL",
    "https://pocketoption.com/",
)


# ============================================================
# ALIASES
# ============================================================

pairs = PAIRS
timeframes = TIMEFRAMES

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
# COMPATIBILITY CONFIG OBJECT
# ============================================================

@dataclass(frozen=True)
class Config:
    """
    Compatibility object.

    Старые модули проекта могут делать:

        from config import config

    Новые модули могут использовать:

        import config
        config.BOT_TOKEN
    """

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

    ANY_PAIR: str

    # --------------------------------------------------------
    # Lowercase compatibility
    # --------------------------------------------------------

    @property
    def bot_token(self) -> str:
        return self.BOT_TOKEN

    @property
    def owner_id(self) -> Optional[int]:
        return self.OWNER_ID

    @property
    def database_url(self) -> str:
        return self.DATABASE_URL

    @property
    def join_required(self) -> bool:
        return self.JOIN_REQUIRED

    @property
    def min_signal_score(self) -> float:
        return self.MIN_SIGNAL_SCORE

    @property
    def min_probability(self) -> float:
        return self.MIN_PROBABILITY

    @property
    def scan_interval(self) -> int:
        return self.SCAN_INTERVAL

    @property
    def timezone(self) -> str:
        return self.TIMEZONE

    @property
    def po_email(self) -> str:
        return self.PO_EMAIL

    @property
    def po_password(self) -> str:
        return self.PO_PASSWORD

    @property
    def po_ssid(self) -> str:
        return self.PO_SSID

    @property
    def po_auto_login(self) -> bool:
        return self.PO_AUTO_LOGIN

    @property
    def po_demo(self) -> bool:
        return self.PO_DEMO

    @property
    def po_login_url(self) -> str:
        return self.PO_LOGIN_URL

    @property
    def pairs(self) -> list:
        return self.PAIRS

    @property
    def timeframes(self) -> list:
        return self.TIMEFRAMES


# ============================================================
# GLOBAL CONFIG INSTANCE
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

    ANY_PAIR=ANY_PAIR,
)


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured. "
        "Set BOT_TOKEN in Render Environment Variables."
    )


if BOT_TOKEN == ":":
    raise RuntimeError(
        "BOT_TOKEN is invalid. "
        "Set the real Telegram bot token "
        "in Render Environment Variables."
    )


if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. "
        "Set the Render PostgreSQL Internal Database URL "
        "in Environment Variables."
    )


if (
    not DATABASE_URL.startswith(
        "postgresql+asyncpg://"
    )
):
    raise RuntimeError(
        "DATABASE_URL is not a valid PostgreSQL URL "
        "for asyncpg."
    )
