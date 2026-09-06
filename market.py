from __future__ import annotations

import asyncio
import json
import logging

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

import config


logger = logging.getLogger("pocket_market")


BIQUOTE_BASE_URL = getattr(
    config,
    "BIQUOTE_BASE_URL",
    "https://biquote.io",
).rstrip("/")

TWELVE_DATA_BASE_URL = (
    "https://api.twelvedata.com"
)

HTTP_TIMEOUT = 12
CONNECT_TIMEOUT = 8

MIN_CANDLES = 60
DEFAULT_CANDLE_LIMIT = 1600


@dataclass(slots=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def timestamp(self) -> float:
        return self.time.timestamp()


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (
        TypeError,
        ValueError,
    ):
        return None


def _datetime(value: Any) -> Optional[datetime]:

    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value

    else:
        text = str(value).strip()

        if not text:
            return None

        try:
            if text.endswith("Z"):
                text = (
                    text[:-1]
                    + "+00:00"
                )

            dt = datetime.fromisoformat(
                text
            )

        except ValueError:

            try:
                timestamp = float(value)

                if timestamp > 10_000_000_000:
                    timestamp /= 1000.0

                dt = datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                )

            except Exception:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(
        timezone.utc
    )


def _clean_pair(pair: str) -> str:

    value = str(pair).strip().upper()

    for suffix in (
        "_OTC",
        " OTC",
        "-OTC",
    ):
        value = value.replace(
            suffix,
            "",
        )

    for char in (
        "/",
        "-",
        "_",
    ):
        value = value.replace(
            char,
            "",
        )

    return value


def _twelve_symbol(pair: str) -> str:

    clean = _clean_pair(pair)

    if len(clean) == 6:
        return (
            f"{clean[:3]}/"
            f"{clean[3:]}"
        )

    return clean


def _unique_candles(
    candles: list[Candle],
) -> list[Candle]:

    result = {}

    for candle in candles:
        result[
            int(candle.time.timestamp())
        ] = candle

    return sorted(
        result.values(),
        key=lambda x: x.time,
    )


class PocketMarket:

    def __init__(self):

        self.client: Optional[
            aiohttp.ClientSession
        ] = None

        self.connected = False
        self.provider = None

        self._lock = asyncio.Lock()

        self._cache = {}

    async def _ensure_client(self):

        if (
            self.client is not None
            and not self.client.closed
        ):
            return self.client

        timeout = aiohttp.ClientTimeout(
            total=HTTP_TIMEOUT,
            connect=CONNECT_TIMEOUT,
            sock_connect=CONNECT_TIMEOUT,
            sock_read=HTTP_TIMEOUT,
        )

        self.client = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent":
                    "POCKET_SIGNAL_BOT/4.0",
                "Accept":
                    "application/json",
            },
        )

        return self.client

    async def _get_json(
        self,
        url: str,
        params: Optional[
            dict[str, Any]
        ] = None,
        timeout_seconds: float = HTTP_TIMEOUT,
    ):

        client = await self._ensure_client()

        logger.info(
            "[MARKET] GET %s params=%s",
            url,
            params,
        )

        try:

            async with asyncio.timeout(
                timeout_seconds
            ):

                async with client.get(
                    url,
                    params=params,
                ) as response:

                    text = await response.text()

                    if response.status >= 400:
                        raise RuntimeError(
                            f"HTTP {response.status}: "
                            f"{text[:300]}"
                        )

                    try:
                        return json.loads(text)

                    except Exception as exc:
                        raise RuntimeError(
                            "Provider вернул "
                            "не JSON: "
                            f"{text[:300]}"
                        ) from exc

        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Таймаут источника рынка "
                f"{timeout_seconds:.0f} сек"
            )

    async def connect(self) -> bool:

        async with self._lock:

            if (
                self.connected
                and self.client is not None
                and not self.client.closed
            ):
                return True

            self.connected = False
            self.provider = None

            logger.info(
                "[MARKET] 🔌 ПРОВЕРКА РЫНКА"
            )

            # -----------------------------
            # BIQUOTE
            # -----------------------------

            try:

                logger.info(
                    "[MARKET] Проверяю BiQuote..."
                )

                data = await self._get_json(
                    f"{BIQUOTE_BASE_URL}/api/EURUSD",
                    timeout_seconds=8,
                )

                if isinstance(data, dict):

                    price = (
                        data.get("last")
                        or data.get("bid")
                        or data.get("ask")
                    )

                    if _float(price) is not None:

                        self.connected = True
                        self.provider = "biquote"

                        logger.info(
                            "[MARKET] "
                            "✅ BiQuote подключён"
                        )

                        return True

            except Exception as exc:

                logger.warning(
                    "[MARKET] BiQuote: %s",
                    exc,
                )

            # -----------------------------
            # TWELVE DATA
            # -----------------------------

            api_key = getattr(
                config,
                "TWELVE_DATA_API_KEY",
                "",
            )

            if api_key:

                try:

                    logger.info(
                        "[MARKET] "
                        "Проверяю Twelve Data..."
                    )

                    data = await self._get_json(
                        f"{TWELVE_DATA_BASE_URL}"
                        "/time_series",
                        params={
                            "symbol": "EUR/USD",
                            "interval": "1min",
                            "outputsize": 2,
                            "timezone": "UTC",
                            "apikey": api_key,
                        },
                        timeout_seconds=8,
                    )

                    values = (
                        data.get("values")
                        if isinstance(
                            data,
                            dict,
                        )
                        else None
                    )

                    if values:

                        self.connected = True
                        self.provider = (
                            "twelve_data"
                        )

                        logger.info(
                            "[MARKET] "
                            "✅ Twelve Data "
                            "подключён"
                        )

                        return True

                except Exception as exc:

                    logger.warning(
                        "[MARKET] Twelve Data: %s",
                        exc,
                    )

            logger.error(
                "[MARKET] ❌ "
                "Источник рынка недоступен"
            )

            return False

    async def _biquote_candles(
        self,
        pair: str,
        limit: int,
    ) -> list[Candle]:

        symbol = _clean_pair(pair)

        target = min(
            max(int(limit), MIN_CANDLES),
            2000,
        )

        data = await self._get_json(
            f"{BIQUOTE_BASE_URL}/api/"
            f"{quote(symbol)}/ohlc",
            params={
                "interval": "1m",
                "limit": target,
            },
            timeout_seconds=12,
        )

        bars = (
            data.get("bars", [])
            if isinstance(data, dict)
            else []
        )

        candles = []

        for item in bars:

            if not isinstance(item, dict):
                continue

            if item.get("isOpen") is True:
                continue

            dt = _datetime(
                item.get("openTime")
            )

            o = _float(
                item.get("open")
            )
            h = _float(
                item.get("high")
            )
            l = _float(
                item.get("low")
            )
            c = _float(
                item.get("close")
            )

            if any(
                x is None
                for x in (
                    dt,
                    o,
                    h,
                    l,
                    c,
                )
            ):
                continue

            volume = (
                _float(
                    item.get("tickVolume")
                )
                or _float(
                    item.get("volume")
                )
                or 0.0
            )

            candles.append(
                Candle(
                    time=dt,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=volume,
                )
            )

        return _unique_candles(candles)[
            -target:
        ]

    async def _twelve_candles(
        self,
        pair: str,
        limit: int,
    ) -> list[Candle]:

        api_key = getattr(
            config,
            "TWELVE_DATA_API_KEY",
            "",
        )

        if not api_key:
            raise RuntimeError(
                "TWELVE_DATA_API_KEY не задан"
            )

        symbol = _twelve_symbol(pair)

        outputsize = min(
            max(int(limit), MIN_CANDLES),
            5000,
        )

        data = await self._get_json(
            f"{TWELVE_DATA_BASE_URL}"
            "/time_series",
            params={
                "symbol": symbol,
                "interval": "1min",
                "outputsize": outputsize,
                "timezone": "UTC",
                "apikey": api_key,
            },
            timeout_seconds=12,
        )

        values = (
            data.get("values", [])
            if isinstance(data, dict)
            else []
        )

        candles = []

        for item in values:

            if not isinstance(item, dict):
                continue

            dt = _datetime(
                item.get("datetime")
            )

            o = _float(
                item.get("open")
            )
            h = _float(
                item.get("high")
            )
            l = _float(
                item.get("low")
            )
            c = _float(
                item.get("close")
            )

            if any(
                x is None
                for x in (
                    dt,
                    o,
                    h,
                    l,
                    c,
                )
            ):
                continue

            volume = (
                _float(
                    item.get("volume")
                )
                or 0.0
            )

            candles.append(
                Candle(
                    time=dt,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=volume,
                )
            )

        return _unique_candles(candles)[
            -outputsize:
        ]

    async def candles(
        self,
        pair: str,
        limit: int = DEFAULT_CANDLE_LIMIT,
    ) -> list[Candle]:

        if not self.connected:
            if not await self.connect():
                raise RuntimeError(
                    "Рынок не подключён"
                )

        key = (
            _clean_pair(pair),
            int(limit),
        )

        now = asyncio.get_running_loop().time()

        cached = self._cache.get(key)

        cache_seconds = max(
            5,
            int(
                getattr(
                    config,
                    "MARKET_CACHE_SECONDS",
                    20,
                )
            ),
        )

        if cached is not None:

            cached_at, cached_data = cached

            if (
                now - cached_at
                < cache_seconds
            ):
                return list(cached_data)

        try:

            if self.provider == "biquote":
                data = await self._biquote_candles(
                    pair,
                    limit,
                )

            elif self.provider == "twelve_data":
                data = await self._twelve_candles(
                    pair,
                    limit,
                )

            else:
                raise RuntimeError(
                    "Неизвестный provider"
                )

        except Exception as primary_exc:

            logger.warning(
                "[MARKET] Ошибка %s: %s",
                pair,
                primary_exc,
            )

            if (
                self.provider != "twelve_data"
                and getattr(
                    config,
                    "TWELVE_DATA_API_KEY",
                    "",
                )
            ):

                try:
                    data = await self._twelve_candles(
                        pair,
                        limit,
                    )

                    self.provider = "twelve_data"

                except Exception:
                    raise primary_exc

            else:
                raise

        if len(data) < MIN_CANDLES:

            raise RuntimeError(
                f"{pair}: получено "
                f"{len(data)} свечей, "
                f"нужно минимум {MIN_CANDLES}"
            )

        self._cache[key] = (
            now,
            list(data),
        )

        logger.info(
            "[MARKET] %s: %s свечей",
            pair,
            len(data),
        )

        return data

    async def get_candles(
        self,
        pair: str,
        timeframe: int = 1,
        limit: int = DEFAULT_CANDLE_LIMIT,
    ) -> list[Candle]:

        return await self.candles(
            pair,
            limit,
        )

    def is_connected(self) -> bool:
        return bool(
            self.connected
            and self.client is not None
            and not self.client.closed
        )

    async def balance(self):

        return None

    async def close(self):

        self.connected = False
        self.provider = None

        if (
            self.client is not None
            and not self.client.closed
        ):
            await self.client.close()

        self.client = None
