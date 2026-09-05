import os
from dataclasses import dataclass, field


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "y"}


def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    owner_id: int = env_int("OWNER_ID")

    database_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./signals.db"
    )

    join_chat_id: str = os.getenv("JOIN_CHAT_ID", "")
    join_required: bool = env_bool("JOIN_REQUIRED", True)

    min_score: float = float(os.getenv("MIN_SIGNAL_SCORE", "75"))
    min_probability: float = float(os.getenv("MIN_PROBABILITY", "75"))

    scan_interval: int = env_int("SCAN_INTERVAL", 30)

    timezone: str = os.getenv("TIMEZONE", "Europe/Moscow")

    pairs: list[str] = field(default_factory=lambda: [
        "EUR/USD",
        "GBP/USD",
        "USD/JPY",
        "USD/CHF",
        "AUD/USD",
        "USD/CAD",
        "NZD/USD",
        "EUR/GBP",
        "EUR/JPY",
        "GBP/JPY",
    ])

    timeframes: list[int] = field(default_factory=lambda: [
        1, 2, 3, 5, 10, 15, 20
    ])

    yahoo_symbols: dict[str, str] = field(default_factory=lambda: {
        "EUR/USD": "EURUSD=X",
        "GBP/USD": "GBPUSD=X",
        "USD/JPY": "JPY=X",
        "USD/CHF": "CHF=X",
        "AUD/USD": "AUDUSD=X",
        "USD/CAD": "CAD=X",
        "NZD/USD": "NZDUSD=X",
        "EUR/GBP": "EURGBP=X",
        "EUR/JPY": "EURJPY=X",
        "GBP/JPY": "GBPJPY=X",
    })

    def validate(self):
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN is not configured")

        if not self.owner_id:
            raise RuntimeError("OWNER_ID is not configured")


config = Config()
config.validate()
