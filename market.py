from __future__ import annotations

import asyncio
import json
import logging

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

import config


logger = logging.getLogger("pocket_market")


# ============================================================
# SETTINGS
# ============================================================

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
MAX_BARS_PER_REQUEST = 1000

DEFAULT_CANDLE_LIMIT = 1600

CACHE_SECONDS = max(
    5,
    int(
        getattr(
            config,
            "MARKET_CACHE_SECONDS",
            20,
        )
    ),
)


# ============================================================
# CANDLE
# ============================================================

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


# ============================================================
# HELPERS
# ============================================================

def _float(
    value: Any,
) -> Optional[float]:

    try:
        return float(value)
    except (
        TypeError,
        ValueError,
    ):
        return None


def _datetime(
    value: Any,
) -> Optional[datetime]:

    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):

        dt = value

    else:

        text = str(
            value
        ).strip()

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

                timestamp = float(
                    value
                )

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


def _clean_pair(
    pair: str,
) -> str:

    value = str(
        pair
    ).strip().upper()

    value = value.replace(
        "_OTC",
        "",
    )

    value = value.replace(
        " OTC",
        "",
    )

    value = value.replace(
        "/",
        "",
    )

    value = value.replace(
        "-",
        "",
    )

    value = value.replace(
        "_",
        "",
    )

    return value


def _twelve_symbol(
    pair: str,
) -> str:

    clean = _clean_pair(
        pair
    )

    if len(clean) == 6:

        return (
            f"{clean[:3]}/"
            f"{clean[3:]}"
        )

    return clean


def _unique_candles(
    candles: list[Candle],
) -> list[Candle]:

    result: dict[int, Candle] = {}

    for candle in candles:

        key = int(
            candle.time.timestamp()
        )

        result[key] = candle

    return sorted(
        result.values(),
        key=lambda item: item.time,
    )


# ============================================================
# MARKET
# ============================================================

class PocketMarket:

    def __init__(self) -> None:

        self.client: Optional[
            aiohttp.ClientSession
        ] = None

        self.connected = False

        self.provider: Optional[
            str
        ] = None

        self._lock = asyncio.Lock()

        self._cache: dict[
            tuple[str, int],
            tuple[
                float,
                list[Candle],
            ],
        ] = {}


    # ========================================================
    # HTTP CLIENT
    # ========================================================

    async def _ensure_client(
        self,
    ) -> aiohttp.ClientSession:

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
                    "POCKET_SIGNAL_BOT/3.0",
                "Accept":
                    "application/json",
            },
        )

        return self.client


    # ========================================================
    # HTTP JSON
    # ========================================================

    async def _get_json(
        self,
        url: str,
        params: Optional[
            dict[str, Any]
        ] = None,
        timeout_seconds: float = HTTP_TIMEOUT,
    ) -> Any:

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
                            f"{text[:500]}"
                        )

                    try:

                        return json.loads(
                            text
                        )

                    except Exception as exc:

                        raise RuntimeError(
                            "Некорректный JSON "
                            "от market provider: "
                            f"{text[:500]}"
                        ) from exc

        except asyncio.TimeoutError:

            raise RuntimeError(
                f"Таймаут источника рынка "
                f"({timeout_seconds:.0f} сек)"
            )


    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(
        self,
    ) -> bool:

        async with self._lock:

            if (
                self.connected
                and self.client is not None
                and not self.client.closed
            ):

                return True

            logger.info(
                "[MARKET] Проверка источника рынка..."
            )

            # ------------------------------------------------
            # BIQUOTE
            # ------------------------------------------------

            try:

                logger.info(
                    "[MARKET] Проверяю BiQuote..."
                )

                data = await self._get_json(
                    f"{BIQUOTE_BASE_URL}/api/EURUSD",
                    timeout_seconds=8,
                )

                if isinstance(
                    data,
                    dict,
                ):

                    price = (
                        data.get("last")
                        or data.get("bid")
                        or data.get("ask")
                    )

                    if _float(price) is not None:

                        self.connected = True
                        self.provider = (
                            "biquote"
                        )

                        logger.info(
                            "[MARKET] "
                            "✅ BiQuote подключён"
                        )

                        return True

                    logger.warning(
                        "[MARKET] "
                        "BiQuote ответил, "
                        "но цена отсутствует"
                    )

            except Exception as exc:

                logger.warning(
                    "[MARKET] "
                    "BiQuote недоступен: %s",
                    exc,
                )

            # ------------------------------------------------
            # TWELVE DATA
            # ------------------------------------------------

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
                            "symbol":
                                "EUR/USD",
                            "interval":
                                "1min",
                            "outputsize":
                                2,
                            "apikey":
                                api_key,
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
                        "[MARKET] "
                        "Twelve Data "
                        "недоступен: %s",
                        exc,
                    )

            self.connected = False
            self.provider = None

            logger.error(
                "[MARKET] ❌ "
                "Источник рынка недоступен"
            )

            return False


    # ========================================================
    # BIQUOTE CANDLES
    # ========================================================

    async def _biquote_candles(
        self,
        pair: str,
        limit: int,
    ) -> list[Candle]:

        symbol = _clean_pair(
            pair
        )

        if not symbol:

            raise ValueError(
                f"Некорректная пара: {pair}"
            )

        target = min(
            max(
                int(limit),
                MIN_CANDLES,
            ),
            2000,
        )

        collected: list[Candle] = []

        # ----------------------------------------------------
        # FIRST REQUEST
        # ----------------------------------------------------

        data = await self._get_json(
            f"{BIQUOTE_BASE_URL}/api/"
            f"{quote(symbol)}/ohlc",
            params={
                "interval": "1m",
                "limit": min(
                    MAX_BARS_PER_REQUEST,
                    target,
                ),
            },
            timeout_seconds=12,
        )

        bars = (
            data.get("bars", [])
            if isinstance(
                data,
                dict,
            )
            else []
        )

        for item in bars:

            if not isinstance(
                item,
                dict,
            ):
                continue

            if item.get(
                "isOpen"
            ) is True:
                continue

            dt = _datetime(
                item.get(
                    "openTime"
                )
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

            if (
                dt is None
                or o is None
                or h is None
                or l is None
                or c is None
            ):
                continue

            volume = (
                _float(
                    item.get(
                        "tickVolume"
                    )
                )
                or _float(
                    item.get(
                        "volume"
                    )
                )
                or 0.0
            )

            collected.append(
                Candle(
                    time=dt,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=volume,
                )
            )

        collected = _unique_candles(
            collected
        )

        # ----------------------------------------------------
        # SECOND REQUEST
        # ----------------------------------------------------

        if (
            len(collected) < target
            and collected
        ):

            oldest = collected[0].time

            to_time = (
                oldest
                - timedelta(
                    seconds=1
                )
            )

            try:

                data2 = await self._get_json(
                    f"{BIQUOTE_BASE_URL}/api/"
                    f"{quote(symbol)}/ohlc",
                    params={
                        "interval": "1m",
                        "limit":
                            MAX_BARS_PER_REQUEST,
                        "to":
                            to_time.isoformat()
                            .replace(
                                "+00:00",
                                "Z",
                            ),
                    },
                    timeout_seconds=12,
                )

                bars2 = (
                    data2.get("bars", [])
                    if isinstance(
                        data2,
                        dict,
                    )
                    else []
                )

                for item in bars2:

                    if not isinstance(
                        item,
                        dict,
                    ):
                        continue

                    if item.get(
                        "isOpen"
                    ) is True:
                        continue

                    dt = _datetime(
                        item.get(
                            "openTime"
                        )
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

                    if (
                        dt is None
                        or o is None
                        or h is None
                        or l is None
                        or c is None
                    ):
                        continue

                    volume = (
                        _float(
                            item.get(
                                "tickVolume"
                            )
                        )
                        or _float(
                            item.get(
                                "volume"
                            )
                        )
                        or 0.0
                    )

                    collected.append(
                        Candle(
                            time=dt,
                            open=o,
                            high=h,
                            low=l,
                            close=c,
                            volume=volume,
                        )
                    )

            except Exception as exc:

                logger.warning(
                    "[BIQUOTE] "
                    "Второй запрос %s: %s",
                    symbol,
                    exc,
                )

        collected = _unique_candles(
            collected
        )

        if len(collected) > target:

            collected = collected[
                -target:
            ]

        return collected


    # ========================================================
    # TWELVE DATA CANDLES
    # ========================================================

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
                "TWELVE_DATA_API_KEY "
                "не задан."
            )

        symbol = _twelve_symbol(
            pair
        )

        outputsize = min(
            max(
                int(limit),
                MIN_CANDLES,
            ),
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

        if not isinstance(
            data,
            dict,
        ):

            raise RuntimeError(
                "Twelve Data "
                "вернул некорректный ответ."
            )

        if data.get(
            "status"
        ) == "error":

            raise RuntimeError(
                str(
                    data.get(
                        "message",
                        "Twelve Data error",
                    )
                )
            )

        values = data.get(
            "values",
            [],
        )

        result: list[Candle] = []

        for item in values:

            if not isinstance(
                item,
                dict,
            ):
                continue

            dt = _datetime(
                item.get(
                    "datetime"
                )
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

            if (
                dt is None
                or o is None
                or h is None
                or l is None
                or c is None
            ):
                continue

            volume = (
                _float(
                    item.get("volume")
                )
                or 0.0
            )

            result.append(
                Candle(
                    time=dt,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=volume,
                )
            )

        return _unique_candles(
            result
        )[-outputsize:]


    # ========================================================
    # CANDLES
    # ========================================================

    async def candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int = DEFAULT_CANDLE_LIMIT,
    ) -> list[Candle]:

        if not self.connected:

            connected = await self.connect()

            if not connected:

                raise RuntimeError(
                    "Рыночный источник "
                    "недоступен."
                )

        cache_key = (
            _clean_pair(pair),
            1,
        )

        loop = (
            asyncio.get_running_loop()
        )

        now = loop.time()

        cached = self._cache.get(
            cache_key
        )

        if cached:

            cache_time, cached_candles = (
                cached
            )

            if (
                now - cache_time
                < CACHE_SECONDS
                and len(
                    cached_candles
                ) >= MIN_CANDLES
            ):

                return list(
                    cached_candles
                )

        try:

            if self.provider == "biquote":

                result = (
                    await self._biquote_candles(
                        pair,
                        limit,
                    )
                )

            elif (
                self.provider
                == "twelve_data"
            ):

                result = (
                    await self._twelve_candles(
                        pair,
                        limit,
                    )
                )

            else:

                result = []

            # ------------------------------------------------
            # FALLBACK
            # ------------------------------------------------

            if len(result) < MIN_CANDLES:

                logger.warning(
                    "[MARKET] %s: "
                    "недостаточно свечей "
                    "от %s: %s",
                    pair,
                    self.provider,
                    len(result),
                )

                api_key = getattr(
                    config,
                    "TWELVE_DATA_API_KEY",
                    "",
                )

                if (
                    self.provider
                    != "twelve_data"
                    and api_key
                ):

                    try:

                        result = (
                            await self._twelve_candles(
                                pair,
                                limit,
                            )
                        )

                        if result:

                            self.provider = (
                                "twelve_data"
                            )

                    except Exception as exc:

                        logger.warning(
                            "[MARKET] "
                            "Fallback %s: %s",
                            pair,
                            exc,
                        )

            if len(result) < MIN_CANDLES:

                raise RuntimeError(
                    f"Недостаточно свечей "
                    f"для {pair}: "
                    f"{len(result)}"
                )

            result = _unique_candles(
                result
            )

            self._cache[
                cache_key
            ] = (
                now,
                result,
            )

            return list(
                result
            )

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.exception(
                "[MARKET] "
                "Ошибка свечей %s: %s",
                pair,
                exc,
            )

            raise


    # ========================================================
    # ALIASES
    # ========================================================

    async def get_candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int = DEFAULT_CANDLE_LIMIT,
    ) -> list[Candle]:

        return await self.candles(
            pair,
            minutes=minutes,
            limit=limit,
        )


    # ========================================================
    # STATUS
    # ========================================================

    def is_connected(
        self,
    ) -> bool:

        return bool(
            self.connected
            and self.client is not None
            and not self.client.closed
        )


    # ========================================================
    # BALANCE
    # ========================================================

    async def balance(
        self,
    ) -> Optional[float]:

        return None
