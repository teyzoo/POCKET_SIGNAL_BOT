from __future__ import annotations

import os
from dataclasses import dataclass, field


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
    }


def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    # ============================================================
    # TELEGRAM
    # ============================================================

    bot_token: str = os.getenv("BOT_TOKEN", "")
    owner_id: int = env_int("OWNER_ID")

    # ============================================================
    # DATABASE
    # ============================================================

    database_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./signals.db",
    )

    # ============================================================
    # ACCESS / SUBSCRIPTION
    # ============================================================

    join_chat_id: str = os.getenv("JOIN_CHAT_ID", "")

    join_required: bool = env_bool(
        "JOIN_REQUIRED",
        False,
    )

    # ============================================================
    # SIGNAL FILTER
    # ============================================================

    min_score: float = env_float(
        "MIN_SIGNAL_SCORE",
        75.0,
    )

    min_probability: float = env_float(
        "MIN_PROBABILITY",
        75.0,
    )

    # ============================================================
    # SCANNER
    # ============================================================

    scan_interval: int = env_int(
        "SCAN_INTERVAL",
        20,
    )

    timezone: str = os.getenv(
        "TIMEZONE",
        "Europe/Moscow",
    )

    # ============================================================
    # MARKET MODE
    # ============================================================

    # IMPORTANT:
    # This bot is intended for Pocket Option OTC.
    #
    # It does NOT use normal Forex symbols such as:
    # EUR/USD
    #
    # Internal Pocket Option symbols:
    # EURUSD_otc
    # GBPUSD_otc
    # USDJPY_otc
    #
    market_mode: str = os.getenv(
        "MARKET_MODE",
        "OTC",
    ).upper()

    # ============================================================
    # POCKET OPTION SESSION
    # ============================================================

    # Required for REAL Pocket Option OTC candle data.
    #
    # Example:
    #
    # PO_SSID=42["auth",{"session":"...","isDemo":1,...}]
    #
    # NEVER put this value directly into GitHub.
    po_ssid: str = os.getenv(
        "PO_SSID",
        "",
    )

    # Optional Pocket Option UID.
    po_uid: int = env_int(
        "PO_UID",
        0,
    )

    # Demo mode by default.
    po_demo: bool = env_bool(
        "PO_DEMO",
        True,
    )

    # ============================================================
    # OTC PAIRS
    # ============================================================

    # These are Pocket Option-style OTC symbols.
    #
    # The list intentionally contains currency OTC assets only.
    # We do not mix crypto, stocks or indices into "Any pair".
    #
    otc_pairs: list[str] = field(
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
            "EUR/RUB OTC",
            "USD/RUB OTC",
            "EUR/TRY OTC",
            "USD/MXN OTC",
            "USD/SGD OTC",
            "USD/THB OTC",
            "USD/CNH OTC",
            "USD/INR OTC",
            "USD/BRL OTC",
            "USD/PKR OTC",
            "USD/COP OTC",
            "USD/IDR OTC",
        ],
    )

    # ============================================================
    # POCKET OPTION SYMBOL MAP
    # ============================================================

    pocket_symbols: dict[str, str] = field(
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
            "EUR/RUB OTC": "EURRUB_otc",
            "USD/RUB OTC": "USDRUB_otc",
            "EUR/TRY OTC": "EURTRY_otc",
            "USD/MXN OTC": "USDMXN_otc",
            "USD/SGD OTC": "USDSGD_otc",
            "USD/THB OTC": "USDTHB_otc",
            "USD/CNH OTC": "USDCNH_otc",
            "USD/INR OTC": "USDINR_otc",
            "USD/BRL OTC": "USDBRL_otc",
            "USD/PKR OTC": "USDPKR_otc",
            "USD/COP OTC": "USDCOP_otc",
            "USD/IDR OTC": "USDIDR_otc",
        },
    )

    # ============================================================
    # TIMEFRAMES
    # ============================================================

    timeframes: list[int] = field(
        default_factory=lambda: [
            1,
            2,
            3,
            5,
            10,
            15,
            20,
        ],
    )

    # ============================================================
    # ANALYSIS
    # ============================================================

    candle_limit: int = 200

    # ============================================================
    # VALIDATION
    # ============================================================

    @property
    def pairs(self) -> list[str]:
        """
        Backwards-compatible property.

        Existing main.py can continue using:
            config.pairs
        """
        return list(self.otc_pairs)

    def pocket_symbol(self, pair: str) -> str | None:
        """
        Convert Telegram/display name into Pocket Option symbol.
        """
        return self.pocket_symbols.get(pair)

    def is_otc_pair(self, pair: str) -> bool:
        return pair in self.otc_pairs

    def validate(self) -> None:
        if not self.bot_token:
            raise RuntimeError(
                "BOT_TOKEN is not configured"
            )

        if not self.owner_id:
            raise RuntimeError(
                "OWNER_ID is not configured"
            )

        if self.market_mode != "OTC":
            raise RuntimeError(
                "MARKET_MODE must be OTC"
            )

        if not self.otc_pairs:
            raise RuntimeError(
                "No OTC pairs configured"
            )

        for pair in self.otc_pairs:
            if pair not in self.pocket_symbols:
                raise RuntimeError(
                    f"Missing Pocket Option symbol for {pair}"
                )

        if self.min_score < 0 or self.min_score > 100:
            raise RuntimeError(
                "MIN_SIGNAL_SCORE must be between 0 and 100"
            )

        if self.min_probability < 0 or self.min_probability > 100:
            raise RuntimeError(
                "MIN_PROBABILITY must be between 0 and 100"
            )

        if self.scan_interval < 1:
            raise RuntimeError(
                "SCAN_INTERVAL must be >= 1"
            )


config = Config()
config.validate()
