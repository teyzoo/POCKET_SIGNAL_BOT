from __future__ import annotations

import os
from dataclasses import dataclass, field


def get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default))
    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def normalize_database_url(url: str) -> str:
    """
    Render PostgreSQL обычно отдаёт:
        postgresql://...

    Async SQLAlchemy должен использовать:
        postgresql+asyncpg://...
    """

    url = url.strip()

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://"):]

    return url


@dataclass
class Config:

    # =========================
    # TELEGRAM
    # =========================

    bot_token: str = os.getenv(
        "BOT_TOKEN",
        "",
    )

    owner_id: int = get_int(
        "OWNER_ID",
        0,
    )

    # =========================
    # DATABASE
    # =========================

    database_url: str = normalize_database_url(
        os.getenv(
            "DATABASE_URL",
            "sqlite+aiosqlite:///./pocket_signal.db",
        )
    )

    # =========================
    # ACCESS
    # =========================

    join_required: bool = get_bool(
        "JOIN_REQUIRED",
        False,
    )

    # =========================
    # SIGNAL FILTER
    # =========================

    min_signal_score: float = get_float(
        "MIN_SIGNAL_SCORE",
        75.0,
    )

    min_probability: float = get_float(
        "MIN_PROBABILITY",
        75.0,
    )

    # =========================
    # SCANNER
    # =========================

    scan_interval: int = max(
        10,
        get_int(
            "SCAN_INTERVAL",
            30,
        ),
    )

    timezone: str = os.getenv(
        "TIMEZONE",
        "Europe/Moscow",
    )

    # =========================
    # POCKET OPTION
    # =========================

    po_email: str = os.getenv(
        "PO_EMAIL",
        "",
    )

    po_password: str = os.getenv(
        "PO_PASSWORD",
        "",
    )

    po_ssid: str = os.getenv(
        "PO_SSID",
        "",
    )

    po_auto_login: bool = get_bool(
        "PO_AUTO_LOGIN",
        True,
    )

    po_demo: bool = get_bool(
        "PO_DEMO",
        True,
    )

    po_login_url: str = os.getenv(
        "PO_LOGIN_URL",
        "https://pocketoption.com/en/login/",
    )

    # =========================
    # TIMEFRAMES
    # =========================

    timeframes: list[int] = field(
        default_factory=lambda: [
            1,
            2,
            3,
            5,
            10,
            15,
            20,
        ]
    )

    # =========================
    # OTC PAIRS
    # =========================

    pairs: list[str] = field(
        default_factory=lambda: [
            "EUR/USD OTC",
            "GBP/USD OTC",
            "USD/JPY OTC",
            "USD/CHF OTC",
            "AUD/USD OTC",
            "USD/CAD OTC",
            "NZD/USD OTC",
            "EUR/GBP OTC",
            "EUR/JPY OTC",
            "GBP/JPY OTC",
            "AUD/JPY OTC",
            "AUD/CAD OTC",
            "AUD/CHF OTC",
            "AUD/NZD OTC",
            "CAD/CHF OTC",
            "CAD/JPY OTC",
            "CHF/JPY OTC",
            "EUR/CHF OTC",
            "EUR/NZD OTC",
            "GBP/AUD OTC",
            "NZD/JPY OTC",
        ]
    )

    # =========================
    # POCKET OPTION SYMBOLS
    # =========================

    otc_symbols: dict[str, str] = field(
        default_factory=lambda: {
            "EUR/USD OTC": "EURUSD_otc",
            "GBP/USD OTC": "GBPUSD_otc",
            "USD/JPY OTC": "USDJPY_otc",
            "USD/CHF OTC": "USDCHF_otc",
            "AUD/USD OTC": "AUDUSD_otc",
            "USD/CAD OTC": "USDCAD_otc",
            "NZD/USD OTC": "NZDUSD_otc",
            "EUR/GBP OTC": "EURGBP_otc",
            "EUR/JPY OTC": "EURJPY_otc",
            "GBP/JPY OTC": "GBPJPY_otc",
            "AUD/JPY OTC": "AUDJPY_otc",
            "AUD/CAD OTC": "AUDCAD_otc",
            "AUD/CHF OTC": "AUDCHF_otc",
            "AUD/NZD OTC": "AUDNZD_otc",
            "CAD/CHF OTC": "CADCHF_otc",
            "CAD/JPY OTC": "CADJPY_otc",
            "CHF/JPY OTC": "CHFJPY_otc",
            "EUR/CHF OTC": "EURCHF_otc",
            "EUR/NZD OTC": "EURNZD_otc",
            "GBP/AUD OTC": "GBPAUD_otc",
            "NZD/JPY OTC": "NZDJPY_otc",
        }
    )


config = Config()


if not config.bot_token:
    raise RuntimeError(
        "Переменная BOT_TOKEN не задана."
    )
