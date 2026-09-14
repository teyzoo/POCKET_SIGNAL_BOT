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

TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"

# Жёсткие таймауты.
HTTP_TIMEOUT = 10
CONNECT_TIMEOUT = 5
PROVIDER_TIMEOUT = 7
TOTAL_CONNECT_TIMEOUT = 16

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
    except (TypeError, ValueError):
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
                text = text[:-1] + "+00:00"

            dt = datetime.fromisoformat(text)

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
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _clean_pair(pair: str) -> str:
    value = str(pair).strip().upper()

    for suffix in (
        "_OTC",
        " OTC",
        "-OTC",
    ):
        value = value.replace(suffix, "")

    for char in (
        "/",
        "-",
        "_",
    ):
        value = value.replace(char, "")

    return value


def _twelve_symbol(pair: str) -> str:
    clean = _clean_pair(pair)

    if len(clean) == 6:
        return f"{clean[:3]}/{clean[3:]}"

    return clean


def _unique_candles(
    candles: list[Candle],
) -> list[Candle]:
    result: dict[int, Candle] = {}

    for candle in candles:
        result[int(candle.time.timestamp())] = candle

    return sorted(
        result.values(),
        key=lambda x: x.time,
    )


class PocketMarket:

    def __init__(self):
        self.client: Optional[aiohttp.ClientSession] = None

        self.connected = False
        self.provider: Optional[str] = None

        self._lock = asyncio.Lock()
        self._cache: dict[
            tuple[str, int],
            tuple[float, list[Candle]],
        ] = {}

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
                "User-Agent": "POCKET_SIGNAL_BOT/5.0",
                "Accept": "application/json",
            },
        )

        return self.client

    async def _get_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        timeout_seconds: float = HTTP_TIMEOUT,
    ):
        client = await self._ensure_client()

        logger.info(
            "[MARKET] GET %s params=%s",
            url,
            params,
        )

        try:
            async with asyncio.timeout(timeout_seconds):
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
                        data = json.loads(text)

                    except Exception as exc:
                        raise RuntimeError(
                            "Provider вернул не JSON: "
                            f"{text[:300]}"
                        ) from exc

                    return data

        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Таймаут источника рынка "
                f"({timeout_seconds:.0f} сек)"
            ) from exc

        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"Ошибка HTTP: {exc}"
            ) from exc

    async def _check_biquote(self) -> bool:
        logger.info(
            "[MARKET] Проверяю BiQuote..."
        )

        try:
            data = await self._get_json(
                f"{BIQUOTE_BASE_URL}/api/EURUSD",
                timeout_seconds=PROVIDER_TIMEOUT,
            )

            if not isinstance(data, dict):
                raise RuntimeError(
                    "BiQuote вернул неожиданный формат"
                )

            price = (
                data.get("last")
                or data.get("bid")
                or data.get("ask")
            )

            price = _float(price)

            if price is None:
                raise RuntimeError(
                    "BiQuote не вернул цену EURUSD"
                )

            logger.info(
                "[MARKET] ✅ BiQuote отвечает. "
                "EURUSD=%s",
                price,
            )

            return True

        except Exception as exc:
            logger.warning(
                "[MARKET] ❌ BiQuote: %s",
                exc,
            )
            return False

    async def _check_twelve_data(self) -> bool:
        api_key = getattr(
            config,
            "TWELVE_DATA_API_KEY",
            "",
        )

        if not api_key:
            logger.warning(
                "[MARKET] Twelve Data пропущен: "
                "TWELVE_DATA_API_KEY отсутствует"
            )
            return False

        logger.info(
            "[MARKET] Проверяю Twelve Data..."
        )

        try:
            data = await self._get_json(
                f"{TWELVE_DATA_BASE_URL}/time_series",
                params={
                    "symbol": "EUR/USD",
                    "interval": "1min",
                    "outputsize": 2,
                    "timezone": "UTC",
                    "apikey": api_key,
                },
                timeout_seconds=PROVIDER_TIMEOUT,
            )

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Twelve Data вернул неожиданный формат"
                )

            values = data.get("values")

            if not values:
                message = data.get("message")

                if message:
                    raise RuntimeError(
                        f"Twelve Data: {message}"
                    )

                raise RuntimeError(
                    "Twelve Data не вернул свечи"
                )

            logger.info(
                "[MARKET] ✅ Twelve Data отвечает"
            )

            return True

        except Exception as exc:
            logger.warning(
                "[MARKET] ❌ Twelve Data: %s",
                exc,
            )
            return False

    async def connect(self) -> bool:
        async with self._lock:

            if (
                self.connected
                and self.client is not None
                and not self.client.closed
            ):
                logger.info(
                    "[MARKET] Уже подключён: %s",
                    self.provider,
                )
                return True

            self.connected = False
            self.provider = None

            logger.info(
                "[MARKET] 🔌 ПРОВЕРКА РЫНКА"
            )

            try:
                result = await asyncio.wait_for(
                    self._connect_internal(),
                    timeout=TOTAL_CONNECT_TIMEOUT,
                )

                return result

            except asyncio.TimeoutError:
                logger.error(
                    "[MARKET] ❌ ОБЩИЙ ТАЙМАУТ "
                    "подключения к источнику рынка"
                )

                self.connected = False
                self.provider = None

                return False

            except Exception as exc:
                logger.exception(
                    "[MARKET] ❌ Ошибка подключения: %s",
                    exc,
                )

                self.connected = False
                self.provider = None

                return False

    async def _connect_internal(self) -> bool:

        # -------------------------------------------------
        # 1. BIQUOTE
        # -------------------------------------------------

        if await self._check_biquote():

            self.connected = True
            self.provider = "biquote"

            logger.info(
                "[MARKET] 🟢 Источник рынка: BiQuote"
            )

            return True

        # -------------------------------------------------
        # 2. TWELVE DATA
        # -------------------------------------------------

        if await self._check_twelve_data():

            self.connected = True
            self.provider = "twelve_data"

            logger.info(
                "[MARKET] 🟢 Источник рынка: Twelve Data"
            )

            return True

        logger.error(
            "[MARKET] 🔴 НИ ОДИН ИСТОЧНИК "
            "РЫНКА НЕ ДОСТУПЕН"
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
            timeout_seconds=HTTP_TIMEOUT,
        )

        bars = (
            data.get("bars", [])
            if isinstance(data, dict)
            else []
        )

        candles: list[Candle] = []

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

        result = _unique_candles(candles)

        return result[-target:]

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
            f"{TWELVE_DATA_BASE_URL}/time_series",
            params={
                "symbol": symbol,
                "interval": "1min",
                "outputsize": outputsize,
                "timezone": "UTC",
                "apikey": api_key,
            },
            timeout_seconds=HTTP_TIMEOUT,
        )

        if not isinstance(data, dict):
            raise RuntimeError(
                "Twelve Data вернул неправильный формат"
            )

        if data.get("status") == "error":
            raise RuntimeError(
                str(
                    data.get(
                        "message",
                        "Twelve Data error",
                    )
                )
            )

        values = data.get("values", [])

        candles: list[Candle] = []

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

        result = _unique_candles(candles)

        return result[-outputsize:]

    async def candles(
        self,
        pair: str,
        limit: int = DEFAULT_CANDLE_LIMIT,
    ) -> list[Candle]:

        # Если подключение пропало —
        # автоматически пробуем переподключиться.
        if not self.is_connected():

            logger.info(
                "[MARKET] Соединение отсутствует. "
                "Переподключение..."
            )

            if not await self.connect():
                raise RuntimeError(
                    "Источник рынка недоступен. "
                    "Проверь Render logs и API-ключ."
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
                logger.info(
                    "[MARKET] %s: использую кеш "
                    "(%s свечей)",
                    pair,
                    len(cached_data),
                )

                return list(cached_data)

        data: list[Candle] = []

        # -------------------------------------------------
        # Основной источник
        # -------------------------------------------------

        try:

            if self.provider == "biquote":

                logger.info(
                    "[MARKET] Получаю свечи %s "
                    "через BiQuote...",
                    pair,
                )

                data = await self._biquote_candles(
                    pair,
                    limit,
                )

            elif self.provider == "twelve_data":

                logger.info(
                    "[MARKET] Получаю свечи %s "
                    "через Twelve Data...",
                    pair,
                )

                data = await self._twelve_candles(
                    pair,
                    limit,
                )

            else:
                raise RuntimeError(
                    "Неизвестный источник рынка"
                )

        except Exception as primary_exc:

            logger.warning(
                "[MARKET] Ошибка %s через %s: %s",
                pair,
                self.provider,
                primary_exc,
            )

            # -------------------------------------------------
            # Fallback на Twelve Data
            # -------------------------------------------------

            if (
                self.provider != "twelve_data"
                and getattr(
                    config,
                    "TWELVE_DATA_API_KEY",
                    "",
                )
            ):

                try:

                    logger.info(
                        "[MARKET] Fallback %s -> Twelve Data",
                        pair,
                    )

                    data = await self._twelve_candles(
                        pair,
                        limit,
                    )

                    self.provider = "twelve_data"

                except Exception as fallback_exc:

                    logger.error(
                        "[MARKET] Fallback тоже не сработал: %s",
                        fallback_exc,
                    )

                    raise RuntimeError(
                        f"{pair}: источник рынка "
                        f"не смог получить свечи"
                    ) from fallback_exc

            else:
                raise RuntimeError(
                    f"{pair}: не удалось получить свечи: "
                    f"{primary_exc}"
                ) from primary_exc

        # -------------------------------------------------
        # Проверяем количество свечей
        # -------------------------------------------------

        if len(data) < MIN_CANDLES:

            raise RuntimeError(
                f"{pair}: получено только "
                f"{len(data)} свечей. "
                f"Нужно минимум {MIN_CANDLES}."
            )

        # -------------------------------------------------
        # Сохраняем кеш
        # -------------------------------------------------

        self._cache[key] = (
            now,
            list(data),
        )

        logger.info(
            "[MARKET] ✅ %s: получено %s свечей "
            "через %s",
            pair,
            len(data),
            self.provider,
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

        logger.info(
            "[MARKET] Закрываю соединение..."
        )

        self.connected = False
        self.provider = None
        self._cache.clear()

        if (
            self.client is not None
            and not self.client.closed
        ):
            try:
                await self.client.close()
            except Exception as exc:
                logger.warning(
                    "[MARKET] Ошибка закрытия: %s",
                    exc,
                )

        self.client = None

        logger.info(
            "[MARKET] Соединение закрыто"
        )