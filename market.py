from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import config

logger = logging.getLogger("pocket_market")


# ============================================================
# BinaryOptionsToolsV2
# ============================================================

try:
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
except Exception as exc:
    PocketOptionAsync = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

try:
    from BinaryOptionsToolsV2.pocketoption.tools.login import login_async
except Exception as exc:
    login_async = None
    _LOGIN_IMPORT_ERROR = exc
else:
    _LOGIN_IMPORT_ERROR = None


# ============================================================
# SETTINGS
# ============================================================

CONNECT_TIMEOUT = float(
    getattr(config, "MARKET_CONNECT_TIMEOUT", 30)
)

CANDLE_TIMEOUT = float(
    getattr(config, "MARKET_CANDLE_TIMEOUT", 15)
)

LOGIN_TIMEOUT = float(
    getattr(config, "PO_LOGIN_TIMEOUT", 60)
)

CANDLE_LIMIT = int(
    getattr(config, "MARKET_CANDLE_LIMIT", 120)
)

DEMO = bool(
    getattr(config, "PO_DEMO", True)
)

AUTO_LOGIN = bool(
    getattr(config, "PO_AUTO_LOGIN", True)
)

# Pocket Option/BinaryOptionsToolsV2 uses seconds.
# The bot itself works with timeframe values in minutes.
SUPPORTED_SECONDS = {
    1: 60,
    2: 120,
    3: 180,
    5: 300,
    10: 600,
    15: 900,
    20: 1200,
}


class MarketError(RuntimeError):
    """Market connection/data error."""


class PocketMarket:
    """
    Pocket Option market client.

    Authentication:
        PO_EMAIL
        PO_PASSWORD
        PO_DEMO=true/false

    SSID:
        generated automatically by BinaryOptionsToolsV2.

    Important:
        This class DOES NOT place trades.
        It is used only for market data.
    """

    def __init__(self) -> None:
        self.client: Any = None
        self.ssid: Optional[str] = None

        self.connected: bool = False
        self._connect_lock = asyncio.Lock()

        self.last_connect_error: Optional[str] = None
        self.last_data_error: Optional[str] = None

        self._candle_cache: dict[
            tuple[str, int],
            tuple[float, list[dict[str, Any]]],
        ] = {}

    # ========================================================
    # CREDENTIALS
    # ========================================================

    @staticmethod
    def _get_email() -> str:
        return str(
            getattr(
                config,
                "PO_EMAIL",
                getattr(config, "po_email", ""),
            )
            or ""
        ).strip()

    @staticmethod
    def _get_password() -> str:
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
        """
        Login to Pocket Option with email/password.

        BinaryOptionsToolsV2 returns a ready-to-use auth SSID.
        The password is never logged.
        """

        if login_async is None:
            raise MarketError(
                "BinaryOptionsToolsV2 login module unavailable: "
                f"{_LOGIN_IMPORT_ERROR}"
            )

        email = self._get_email()
        password = self._get_password()

        if not email:
            raise MarketError(
                "PO_EMAIL is not configured in Render."
            )

        if not password:
            raise MarketError(
                "PO_PASSWORD is not configured in Render."
            )

        logger.info(
            "Pocket Option login started for configured account"
        )

        try:
            ssid = await asyncio.wait_for(
                login_async(
                    email,
                    password,
                    demo=DEMO,
                    backend="playwright",
                    headless=True,
                    timeout=int(LOGIN_TIMEOUT),
                ),
                timeout=LOGIN_TIMEOUT + 10,
            )
        except asyncio.TimeoutError as exc:
            raise MarketError(
                "Pocket Option login timed out."
            ) from exc
        except Exception as exc:
            # Never log credentials.
            raise MarketError(
                f"Pocket Option login failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not ssid:
            raise MarketError(
                "Pocket Option login returned an empty SSID."
            )

        logger.info(
            "Pocket Option login successful; SSID received internally"
        )

        return str(ssid)

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:
        """
        Connect to Pocket Option.

        Flow:

            Render PO_EMAIL
                    +
            Render PO_PASSWORD
                    ↓
            BinaryOptionsToolsV2 login
                    ↓
            generated SSID
                    ↓
            PocketOptionAsync
        """

        async with self._connect_lock:

            if self.connected and self.client is not None:
                return True

            self.connected = False
            self.last_connect_error = None

            if PocketOptionAsync is None:
                self.last_connect_error = (
                    "BinaryOptionsToolsV2 import failed: "
                    f"{_IMPORT_ERROR}"
                )

                logger.error(self.last_connect_error)
                return False

            if not AUTO_LOGIN:
                self.last_connect_error = (
                    "PO_AUTO_LOGIN is disabled. "
                    "Set PO_AUTO_LOGIN=true in Render."
                )

                logger.error(self.last_connect_error)
                return False

            try:
                logger.info(
                    "Connecting to Pocket Option..."
                )

                # ------------------------------------------------
                # 1. Login and obtain SSID automatically
                # ------------------------------------------------

                self.ssid = await asyncio.wait_for(
                    self._login(),
                    timeout=LOGIN_TIMEOUT + 15,
                )

                # ------------------------------------------------
                # 2. Create PocketOptionAsync client
                # ------------------------------------------------

                self.client = PocketOptionAsync(
                    self.ssid
                )

                # ------------------------------------------------
                # 3. Give WebSocket/client time to initialize
                # ------------------------------------------------

                await asyncio.sleep(3)

                # ------------------------------------------------
                # 4. Verify connection using server_time if
                #    available.
                # ------------------------------------------------

                try:
                    server_time_method = getattr(
                        self.client,
                        "server_time",
                        None,
                    )

                    if callable(server_time_method):
                        await asyncio.wait_for(
                            server_time_method(),
                            timeout=CONNECT_TIMEOUT,
                        )

                except asyncio.TimeoutError:
                    logger.warning(
                        "Pocket Option server_time check timed out; "
                        "client will still be tested with candles."
                    )

                except Exception as exc:
                    logger.warning(
                        "server_time check failed: %s",
                        exc,
                    )

                self.connected = True

                logger.info(
                    "Pocket Option connection established"
                )

                return True

            except asyncio.TimeoutError:
                self.last_connect_error = (
                    "Pocket Option connection timed out."
                )

            except Exception as exc:
                self.last_connect_error = (
                    f"{type(exc).__name__}: {exc}"
                )

            self.connected = False

            if self.client is not None:
                try:
                    shutdown = getattr(
                        self.client,
                        "shutdown",
                        None,
                    )

                    if callable(shutdown):
                        await asyncio.wait_for(
                            shutdown(),
                            timeout=5,
                        )

                except Exception:
                    pass

            self.client = None
            self.ssid = None

            logger.error(
                "Pocket Option connection failed: %s",
                self.last_connect_error,
            )

            return False

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self) -> None:
        """Close Pocket Option client."""

        client = self.client

        self.client = None
        self.connected = False
        self.ssid = None

        if client is None:
            return

        try:
            shutdown = getattr(
                client,
                "shutdown",
                None,
            )

            if callable(shutdown):
                await asyncio.wait_for(
                    shutdown(),
                    timeout=5,
                )

        except Exception as exc:
            logger.warning(
                "Pocket Option shutdown error: %s",
                exc,
            )

    # ========================================================
    # RECONNECT
    # ========================================================

    async def reconnect(self) -> bool:
        """Force a new login and connection."""

        logger.warning(
            "Forcing Pocket Option reconnect..."
        )

        await self.close()

        return await self.connect()

    # ========================================================
    # TIMEFRAME
    # ========================================================

    @staticmethod
    def _timeframe_seconds(timeframe: int) -> int:
        timeframe = int(timeframe)

        if timeframe in SUPPORTED_SECONDS:
            return SUPPORTED_SECONDS[timeframe]

        # Allow direct seconds if caller explicitly gives one.
        if timeframe in (
            60,
            120,
            180,
            300,
            600,
            900,
            1200,
        ):
            return timeframe

        raise MarketError(
            f"Unsupported timeframe: {timeframe}"
        )

    # ========================================================
    # CANDLE NORMALIZATION
    # ========================================================

    @staticmethod
    def _normalize_candle(
        candle: Any,
    ) -> Optional[dict[str, Any]]:
        """
        Convert BinaryOptionsToolsV2 candle objects/dicts
        into the format expected by signals.py.
        """

        if candle is None:
            return None

        # ----------------------------------------------------
        # Dictionary
        # ----------------------------------------------------

        if isinstance(candle, dict):
            source = candle

            try:
                timestamp = source.get(
                    "time",
                    source.get(
                        "timestamp",
                        source.get("ts"),
                    ),
                )

                open_price = source.get("open")
                high_price = source.get("high")
                low_price = source.get("low")
                close_price = source.get("close")

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

        # ----------------------------------------------------
        # Object with attributes
        # ----------------------------------------------------

        try:
            timestamp = getattr(
                candle,
                "time",
                getattr(candle, "timestamp", None),
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
    def _clean_candles(
        cls,
        candles: Any,
    ) -> list[dict[str, Any]]:
        if not candles:
            return []

        if isinstance(candles, dict):
            # Some versions may wrap candles.
            for key in (
                "candles",
                "data",
                "history",
                "result",
            ):
                if key in candles:
                    candles = candles[key]
                    break

        if not isinstance(candles, (list, tuple)):
            return []

        result: list[dict[str, Any]] = []

        for item in candles:
            normalized = cls._normalize_candle(item)

            if normalized is not None:
                result.append(normalized)

        # Remove duplicate timestamps.
        unique: dict[int, dict[str, Any]] = {}

        for candle in result:
            unique[candle["time"]] = candle

        result = list(unique.values())

        # Oldest -> newest.
        result.sort(
            key=lambda item: item["time"]
        )

        return result

    # ========================================================
    # RAW CANDLES
    # ========================================================

    async def _get_raw_candles(
        self,
        pair: str,
        period_seconds: int,
        lookback_seconds: int,
    ) -> Any:
        """
        Get candles directly from Pocket Option.

        First tries get_candles_live() because the library
        documents it as the live/gap-free method.

        Falls back to get_candles() for compatibility.
        """

        if self.client is None:
            raise MarketError(
                "Pocket Option client is not initialized."
            )

        # ----------------------------------------------------
        # Preferred: get_candles_live
        # ----------------------------------------------------

        live_method = getattr(
            self.client,
            "get_candles_live",
            None,
        )

        if callable(live_method):
            try:
                hours = max(
                    0.1,
                    lookback_seconds / 3600.0,
                )

                iterator = live_method(
                    asset=pair,
                    period=period_seconds,
                    hours=hours,
                    max_rows=max(
                        CANDLE_LIMIT,
                        100,
                    ),
                )

                closed, _forming = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=CANDLE_TIMEOUT,
                )

                if closed:
                    return closed

            except asyncio.TimeoutError:
                raise

            except Exception as exc:
                logger.warning(
                    "get_candles_live failed for %s: %s",
                    pair,
                    exc,
                )

        # ----------------------------------------------------
        # Compatibility fallback
        # ----------------------------------------------------

        historical_method = getattr(
            self.client,
            "get_candles",
            None,
        )

        if callable(historical_method):
            return await asyncio.wait_for(
                historical_method(
                    pair,
                    period_seconds,
                    lookback_seconds,
                ),
                timeout=CANDLE_TIMEOUT,
            )

        raise MarketError(
            "BinaryOptionsToolsV2 client has no candle method."
        )

    # ========================================================
    # PUBLIC CANDLES
    # ========================================================

    async def get_candles(
        self,
        pair: str,
        timeframe: int = 1,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        Return normalized Pocket Option candles.

        timeframe:
            minutes, e.g. 1 / 2 / 3 / 5 / 10 / 15 / 20

        limit:
            requested number of candles.
        """

        pair = str(pair).strip()

        if not pair:
            raise MarketError(
                "Empty Pocket Option pair."
            )

        if limit is None:
            limit = CANDLE_LIMIT

        limit = max(
            30,
            int(limit),
        )

        period_seconds = self._timeframe_seconds(
            int(timeframe)
        )

        # Request enough history for indicators.
        lookback_seconds = max(
            period_seconds * (limit + 10),
            3600,
        )

        cache_key = (
            pair,
            period_seconds,
        )

        # Short cache to avoid opening/fetching the same
        # market repeatedly during one scan.
        cache_seconds = float(
            getattr(
                config,
                "MARKET_CACHE_SECONDS",
                2,
            )
        )

        cached = self._candle_cache.get(
            cache_key
        )

        if cached:
            cached_at, cached_data = cached

            if (
                time.monotonic() - cached_at
                <= cache_seconds
                and len(cached_data) >= min(
                    limit,
                    30,
                )
            ):
                return cached_data[-limit:]

        # ----------------------------------------------------
        # Ensure connection
        # ----------------------------------------------------

        if not self.connected or self.client is None:
            ok = await self.connect()

            if not ok:
                raise MarketError(
                    self.last_connect_error
                    or "Pocket Option connection failed."
                )

        # ----------------------------------------------------
        # Fetch
        # ----------------------------------------------------

        try:
            raw = await self._get_raw_candles(
                pair,
                period_seconds,
                lookback_seconds,
            )

            candles = self._clean_candles(
                raw
            )

            if len(candles) < 30:
                raise MarketError(
                    f"Pocket Option returned only "
                    f"{len(candles)} candles for {pair}."
                )

            # Keep requested amount.
            candles = candles[-limit:]

            # Save cache.
            self._candle_cache[
                cache_key
            ] = (
                time.monotonic(),
                candles,
            )

            self.last_data_error = None

            logger.info(
                "Pocket Option candles: %s | "
                "timeframe=%sm | count=%d | last=%s",
                pair,
                timeframe,
                len(candles),
                candles[-1]["close"],
            )

            return candles

        except asyncio.TimeoutError as exc:
            self.last_data_error = (
                f"Timeout while getting candles for {pair}"
            )

            logger.error(
                self.last_data_error
            )

            # Connection may have become stale.
            self.connected = False

            raise MarketError(
                self.last_data_error
            ) from exc

        except MarketError:
            raise

        except Exception as exc:
            self.last_data_error = (
                f"Candle request failed for {pair}: "
                f"{type(exc).__name__}: {exc}"
            )

            logger.error(
                self.last_data_error
            )

            self.connected = False

            raise MarketError(
                self.last_data_error
            ) from exc

    # ========================================================
    # COMPATIBILITY ALIASES
    # ========================================================

    async def candles(
        self,
        pair: str,
        period: int = 60,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        Compatibility method.

        Existing code may call:
            market.candles("EURUSD_otc", 60)

        or:
            market.candles("EURUSD_otc", 1)
        """

        period = int(period)

        if period in SUPPORTED_SECONDS:
            timeframe = period

        elif period in SUPPORTED_SECONDS.values():
            timeframe = next(
                key
                for key, value in SUPPORTED_SECONDS.items()
                if value == period
            )

        else:
            raise MarketError(
                f"Unsupported candle period: {period}"
            )

        return await self.get_candles(
            pair,
            timeframe,
            limit,
        )

    async def history(
        self,
        pair: str,
        timeframe: int = 1,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Alias for get_candles()."""

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
        """
        Check actual Pocket Option market data.
        """

        try:
            candles = await self.get_candles(
                pair,
                1,
                30,
            )

            return len(candles) >= 30

        except Exception as exc:
            logger.error(
                "Pocket Option health check failed: %s",
                exc,
            )

            return False


# ============================================================
# GLOBAL MARKET INSTANCE
# ============================================================

market = PocketMarket()


# ============================================================
# MODULE-LEVEL COMPATIBILITY FUNCTIONS
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


async def history(
    pair: str,
    timeframe: int = 1,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    return await market.history(
        pair,
        timeframe,
        limit,
    )


async def health_check(
    pair: str = "EURUSD_otc",
) -> bool:
    return await market.health_check(pair)


# ============================================================
# OPTIONAL OBJECT-STYLE API
# ============================================================

class Market:
    """
    Compatibility wrapper for code that imports Market().
    """

    def __init__(self) -> None:
        self._market = market

    async def connect(self) -> bool:
        return await self._market.connect()

    async def close(self) -> None:
        await self._market.close()

    async def reconnect(self) -> bool:
        return await self._market.reconnect()

    async def get_candles(
        self,
        pair: str,
        timeframe: int = 1,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        return await self._market.get_candles(
            pair,
            timeframe,
            limit,
        )

    async def candles(
        self,
        pair: str,
        period: int = 60,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        return await self._market.candles(
            pair,
            period,
            limit,
        )

    async def history(
        self,
        pair: str,
        timeframe: int = 1,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        return await self._market.history(
            pair,
            timeframe,
            limit,
        )

    async def health_check(
        self,
        pair: str = "EURUSD_otc",
    ) -> bool:
        return await self._market.health_check(pair)


__all__ = [
    "Market",
    "MarketError",
    "PocketMarket",
    "market",
    "connect",
    "close",
    "reconnect",
    "get_candles",
    "candles",
    "history",
    "health_check",
]

Что сделать сейчас

В GitHub Web открой:

"market.py" → Edit → выдели всё → вставь код выше → Commit changes.

В Render должны остаться:

PO_EMAIL = твой email Pocket Option
PO_PASSWORD = твой пароль Pocket Option
PO_AUTO_LOGIN = true
PO_DEMO = true

"PO_SSID" не нужен.

И ещё: этот вариант использует Playwright для автоматического получения SSID при входе, поэтому твой уже установленный Playwright здесь как раз пригодится. Сам "BinaryOptionsToolsV2" документирует такой login backend и затем передачу полученного SSID в "PocketOptionAsync".

Важно: я специально не добавлял никаких операций покупки/продажи — "market.py" только авторизуется и получает котировки/свечи. OTC теперь будет запрашиваться именно через Pocket Option-клиент, а не через BiQuote/Twelve Data.

После этого не запускай сразу повторный сигнал. Сначала посмотрим Render Logs после деплоя — по ним будет видно, прошёл ли "PO_EMAIL/PO_PASSWORD" → login → SSID → WebSocket → "EURUSD_otc".