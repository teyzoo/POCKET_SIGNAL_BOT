from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import config

logger = logging.getLogger("pocket_market")


# ============================================================
# BINARY OPTIONS TOOLS V2
# ============================================================

try:
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
except Exception as exc:
    PocketOptionAsync = None
    PO_IMPORT_ERROR = exc

try:
    from BinaryOptionsToolsV2.pocketoption.tools.login import login_async
except Exception as exc:
    login_async = None
    LOGIN_IMPORT_ERROR = exc


# ============================================================
# SETTINGS
# ============================================================

LOGIN_TIMEOUT = int(
    getattr(config, "PO_LOGIN_TIMEOUT", 60)
)

CONNECT_TIMEOUT = int(
    getattr(config, "MARKET_CONNECT_TIMEOUT", 30)
)

CANDLE_TIMEOUT = int(
    getattr(config, "MARKET_CANDLE_TIMEOUT", 20)
)

CANDLE_LIMIT = int(
    getattr(config, "MARKET_CANDLE_LIMIT", 120)
)

CACHE_SECONDS = float(
    getattr(config, "MARKET_CACHE_SECONDS", 2)
)

AUTO_LOGIN = bool(
    getattr(config, "PO_AUTO_LOGIN", True)
)

DEMO = bool(
    getattr(config, "PO_DEMO", True)
)


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAMES = {
    1: 60,
    2: 120,
    3: 180,
    5: 300,
    10: 600,
    15: 900,
    20: 1200,
}


# ============================================================
# EXCEPTIONS
# ============================================================

class MarketError(RuntimeError):
    pass


# ============================================================
# MARKET CLIENT
# ============================================================

class PocketMarket:

    def __init__(self) -> None:

        self.client: Any = None

        self.ssid: Optional[str] = None

        self.connected = False

        self.last_connect_error: Optional[str] = None
        self.last_data_error: Optional[str] = None

        self._connect_lock = asyncio.Lock()

        self._cache: dict[
            tuple[str, int],
            tuple[float, list[dict[str, Any]]],
        ] = {}

    # ========================================================
    # CONFIG
    # ========================================================

    @staticmethod
    def _email() -> str:

        return str(
            getattr(
                config,
                "PO_EMAIL",
                getattr(config, "po_email", ""),
            )
            or ""
        ).strip()

    @staticmethod
    def _password() -> str:

        return str(
            getattr(
                config,
                "PO_PASSWORD",
                getattr(config, "po_password", ""),
            )
            or ""
        )

    # ========================================================
    # LOGIN
    # ========================================================

    async def _login(self) -> str:

        if login_async is None:

            raise MarketError(
                "BinaryOptionsToolsV2 login_async "
                f"не импортирован: {LOGIN_IMPORT_ERROR}"
            )

        email = self._email()
        password = self._password()

        if not email:

            raise MarketError(
                "PO_EMAIL отсутствует в Render Environment."
            )

        if not password:

            raise MarketError(
                "PO_PASSWORD отсутствует в Render Environment."
            )

        logger.info(
            "🔐 Авторизация Pocket Option..."
        )

        try:

            ssid = await asyncio.wait_for(
                login_async(
                    email,
                    password,
                    demo=DEMO,
                    backend="playwright",
                    headless=True,
                    timeout=LOGIN_TIMEOUT,
                ),
                timeout=LOGIN_TIMEOUT + 15,
            )

        except asyncio.TimeoutError as exc:

            raise MarketError(
                "Таймаут авторизации Pocket Option."
            ) from exc

        except Exception as exc:

            raise MarketError(
                "Ошибка авторизации Pocket Option: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not ssid:

            raise MarketError(
                "Pocket Option не вернул SSID после авторизации."
            )

        logger.info(
            "✅ Авторизация Pocket Option успешна."
        )

        return str(ssid)

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:

        async with self._connect_lock:

            if (
                self.connected
                and self.client is not None
            ):
                return True

            self.connected = False
            self.last_connect_error = None

            if PocketOptionAsync is None:

                self.last_connect_error = (
                    "BinaryOptionsToolsV2 не импортирован: "
                    f"{PO_IMPORT_ERROR}"
                )

                logger.error(
                    self.last_connect_error
                )

                return False

            if not AUTO_LOGIN:

                self.last_connect_error = (
                    "PO_AUTO_LOGIN=false. "
                    "Установи PO_AUTO_LOGIN=true."
                )

                logger.error(
                    self.last_connect_error
                )

                return False

            try:

                logger.info(
                    "🔌 Подключение к Pocket Option..."
                )

                # ------------------------------------------------
                # LOGIN
                # ------------------------------------------------

                self.ssid = await asyncio.wait_for(
                    self._login(),
                    timeout=LOGIN_TIMEOUT + 20,
                )

                # ------------------------------------------------
                # CLIENT
                # ------------------------------------------------

                self.client = PocketOptionAsync(
                    self.ssid
                )

                # ------------------------------------------------
                # WAIT FOR WEBSOCKET
                # ------------------------------------------------

                await asyncio.sleep(3)

                # ------------------------------------------------
                # OPTIONAL SERVER TIME CHECK
                # ------------------------------------------------

                server_time = getattr(
                    self.client,
                    "server_time",
                    None,
                )

                if callable(server_time):

                    try:

                        await asyncio.wait_for(
                            server_time(),
                            timeout=CONNECT_TIMEOUT,
                        )

                    except Exception as exc:

                        logger.warning(
                            "server_time не ответил: %s",
                            exc,
                        )

                self.connected = True

                logger.info(
                    "✅ Pocket Option подключён."
                )

                return True

            except asyncio.TimeoutError:

                self.last_connect_error = (
                    "Таймаут подключения к Pocket Option."
                )

            except Exception as exc:

                self.last_connect_error = (
                    f"{type(exc).__name__}: {exc}"
                )

            self.connected = False

            try:

                await self._shutdown_client()

            except Exception:
                pass

            self.ssid = None

            logger.error(
                "❌ Pocket Option connection failed: %s",
                self.last_connect_error,
            )

            return False

    # ========================================================
    # SHUTDOWN
    # ========================================================

    async def _shutdown_client(self) -> None:

        if self.client is None:
            return

        shutdown = getattr(
            self.client,
            "shutdown",
            None,
        )

        if callable(shutdown):

            try:

                result = shutdown()

                if asyncio.iscoroutine(result):

                    await asyncio.wait_for(
                        result,
                        timeout=5,
                    )

            except Exception:
                pass

        self.client = None

    async def close(self) -> None:

        self.connected = False

        await self._shutdown_client()

        self.ssid = None

    # ========================================================
    # RECONNECT
    # ========================================================

    async def reconnect(self) -> bool:

        logger.warning(
            "🔄 Переподключение к Pocket Option..."
        )

        await self.close()

        return await self.connect()

    # ========================================================
    # TIMEFRAME
    # ========================================================

    @staticmethod
    def _seconds(
        timeframe: int,
    ) -> int:

        timeframe = int(timeframe)

        if timeframe in TIMEFRAMES:

            return TIMEFRAMES[timeframe]

        if timeframe in TIMEFRAMES.values():

            return timeframe

        raise MarketError(
            f"Неподдерживаемый таймфрейм: {timeframe}"
        )

    # ========================================================
    # CANDLE NORMALIZATION
    # ========================================================

    @staticmethod
    def _normalize(
        candle: Any,
    ) -> Optional[dict[str, Any]]:

        if candle is None:
            return None

        # ----------------------------------------------------
        # DICT
        # ----------------------------------------------------

        if isinstance(candle, dict):

            timestamp = (
                candle.get("time")
                or candle.get("timestamp")
                or candle.get("ts")
            )

            open_price = candle.get("open")
            high_price = candle.get("high")
            low_price = candle.get("low")
            close_price = candle.get("close")

            if timestamp is None:
                return None

            if any(
                value is None
                for value in (
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                )
            ):
                return None

            try:

                return {
                    "time": int(float(timestamp)),
                    "open": float(open_price),
                    "high": float(high_price),
                    "low": float(low_price),
                    "close": float(close_price),
                }

            except (
                TypeError,
                ValueError,
            ):

                return None

        # ----------------------------------------------------
        # OBJECT
        # ----------------------------------------------------

        try:

            timestamp = getattr(
                candle,
                "time",
                getattr(
                    candle,
                    "timestamp",
                    None,
                ),
            )

            open_price = getattr(
                candle,
                "open",
                None,
            )

            high_price = getattr(
                candle,
                "high",
                None,
            )

            low_price = getattr(
                candle,
                "low",
                None,
            )

            close_price = getattr(
                candle,
                "close",
                None,
            )

            if timestamp is None:
                return None

            if any(
                value is None
                for value in (
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                )
            ):
                return None

            return {
                "time": int(float(timestamp)),
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
            }

        except (
            TypeError,
            ValueError,
        ):

            return None

    # ========================================================
    # CLEAN CANDLES
    # ========================================================

    @classmethod
    def _clean(
        cls,
        candles: Any,
    ) -> list[dict[str, Any]]:

        if not candles:
            return []

        if isinstance(candles, dict):

            for key in (
                "candles",
                "data",
                "history",
                "result",
            ):

                if key in candles:

                    candles = candles[key]

                    break

        if not isinstance(
            candles,
            (list, tuple),
        ):
            return []

        unique: dict[
            int,
            dict[str, Any],
        ] = {}

        for item in candles:

            normalized = cls._normalize(
                item
            )

            if normalized is None:
                continue

            unique[
                normalized["time"]
            ] = normalized

        result = list(
            unique.values()
        )

        result.sort(
            key=lambda x: x["time"]
        )

        return result

    # ========================================================
    # RAW POCKET OPTION CANDLES
    # ========================================================

    async def _raw_candles(
        self,
        pair: str,
        period: int,
        lookback: int,
    ) -> Any:

        if self.client is None:

            raise MarketError(
                "Pocket Option client не создан."
            )

        # ----------------------------------------------------
        # LIVE CANDLES
        # ----------------------------------------------------

        live = getattr(
            self.client,
            "get_candles_live",
            None,
        )

        if callable(live):

            try:

                hours = max(
                    0.1,
                    lookback / 3600,
                )

                iterator = live(
                    asset=pair,
                    period=period,
                    hours=hours,
                    max_rows=max(
                        CANDLE_LIMIT,
                        120,
                    ),
                )

                result = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=CANDLE_TIMEOUT,
                )

                if isinstance(
                    result,
                    tuple,
                ):

                    closed = result[0]

                else:

                    closed = result

                if closed:

                    return closed

            except asyncio.TimeoutError:

                raise

            except Exception as exc:

                logger.warning(
                    "get_candles_live error %s: %s",
                    pair,
                    exc,
                )

        # ----------------------------------------------------
        # HISTORICAL FALLBACK
        # ----------------------------------------------------

        historical = getattr(
            self.client,
            "get_candles",
            None,
        )

        if not callable(historical):

            raise MarketError(
                "BinaryOptionsToolsV2 "
                "не имеет метода получения свечей."
            )

        return await asyncio.wait_for(
            historical(
                pair,
                period,
                lookback,
            ),
            timeout=CANDLE_TIMEOUT,
        )

    # ========================================================
    # GET CANDLES
    # ========================================================

    async def get_candles(
        self,
        pair: str,
        timeframe: int = 1,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:

        pair = str(pair).strip()

        if not pair:

            raise MarketError(
                "Пустое название пары."
            )

        if limit is None:

            limit = CANDLE_LIMIT

        limit = max(
            30,
            int(limit),
        )

        period = self._seconds(
            timeframe
        )

        lookback = max(
            period * (limit + 10),
            3600,
        )

        cache_key = (
            pair,
            period,
        )

        cached = self._cache.get(
            cache_key
        )

        if cached:

            created, data = cached

            if (
                time.monotonic() - created
                <= CACHE_SECONDS
                and len(data) >= 30
            ):

                return data[-limit:]

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        if (
            not self.connected
            or self.client is None
        ):

            connected = await self.connect()

            if not connected:

                raise MarketError(
                    self.last_connect_error
                    or "Pocket Option недоступен."
                )

        # ----------------------------------------------------
        # GET DATA
        # ----------------------------------------------------

        try:

            raw = await self._raw_candles(
                pair,
                period,
                lookback,
            )

            candles = self._clean(
                raw
            )

            if len(candles) < 30:

                raise MarketError(
                    f"Pocket Option вернул "
                    f"только {len(candles)} свечей "
                    f"для {pair}."
                )

            candles = candles[-limit:]

            self._cache[
                cache_key
            ] = (
                time.monotonic(),
                candles,
            )

            self.last_data_error = None

            logger.info(
                "📊 POCKET OPTION | "
                "%s | %sm | candles=%d | close=%s",
                pair,
                timeframe,
                len(candles),
                candles[-1]["close"],
            )

            return candles

        except asyncio.TimeoutError as exc:

            self.connected = False

            self.last_data_error = (
                f"Таймаут получения свечей "
                f"{pair}."
            )

            logger.error(
                self.last_data_error
            )

            raise MarketError(
                self.last_data_error
            ) from exc

        except MarketError:

            raise

        except Exception as exc:

            self.connected = False

            self.last_data_error = (
                f"Ошибка свечей {pair}: "
                f"{type(exc).__name__}: {exc}"
            )

            logger.error(
                self.last_data_error
            )

            raise MarketError(
                self.last_data_error
            ) from exc

    # ========================================================
    # COMPATIBILITY: CANDLES
    # ========================================================

    async def candles(
        self,
        pair: str,
        period: int = 60,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:

        period = int(period)

        if period in TIMEFRAMES:

            timeframe = period

        elif period in TIMEFRAMES.values():

            timeframe = next(
                key
                for key, value
                in TIMEFRAMES.items()
                if value == period
            )

        else:

            raise MarketError(
                f"Неподдерживаемый период: {period}"
            )

        return await self.get_candles(
            pair,
            timeframe,
            limit,
        )

    # ========================================================
    # HEALTH CHECK
    # ========================================================

    async def health_check(
        self,
        pair: str = "EURUSD_otc",
    ) -> bool:

        try:

            candles = await self.get_candles(
                pair,
                1,
                30,
            )

            return len(candles) >= 30

        except Exception as exc:

            logger.error(
                "❌ Market health check: %s",
                exc,
            )

            return False


# ============================================================
# GLOBAL INSTANCE
# ============================================================

market = PocketMarket()


# ============================================================
# MODULE FUNCTIONS
# ============================================================

async def connect() -> bool:

    return await market.connect()


async def close() -> None:

    await market.close()


async def reconnect() -> bool:

    return await market.reconnect()


async def get_candles(
    pair: str,
    timeframe: int = 1,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:

    return await market.get_candles(
        pair,
        timeframe,
        limit,
    )


async def candles(
    pair: str,
    period: int = 60,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:

    return await market.candles(
        pair,
        period,
        limit,
    )


async def health_check(
    pair: str = "EURUSD_otc",
) -> bool:

    return await market.health_check(
        pair
    )


__all__ = [
    "MarketError",
    "PocketMarket",
    "market",
    "connect",
    "close",
    "reconnect",
    "get_candles",
    "candles",
    "health_check",
]