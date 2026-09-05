from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any

from config import config


logger = logging.getLogger(__name__)


# ============================================================
# CANDLE
# ============================================================

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ============================================================
# MARKET CLIENT
# ============================================================

class MarketClient:
    """
    Pocket Option OTC market-data client.

    IMPORTANT:
    This class is deliberately signal-only.
    It does not place trades.

    It expects an unofficial Pocket Option WebSocket client
    to be available when PO_SSID is configured.

    Supported external client styles:
        - get_historical_candles(...)
        - get_candles(...)
        - get_candles_dataframe(...)
    """

    def __init__(self):
        self.client: Any | None = None

        self.started = False

        self.cache: dict[
            str,
            tuple[float, list[Candle]]
        ] = {}

        self.cache_ttl = 5.0

        self._lock = asyncio.Lock()

    # ========================================================
    # START
    # ========================================================

    async def start(self) -> None:
        if self.started:
            return

        self.started = True

        if not config.po_ssid:
            logger.warning(
                "PO_SSID is not configured. "
                "Real Pocket Option OTC candles are unavailable."
            )
            return

        await self._connect_pocket_option()

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self) -> None:
        client = self.client

        if client is None:
            self.started = False
            return

        try:
            method = getattr(
                client,
                "disconnect",
                None,
            )

            if method is None:
                method = getattr(
                    client,
                    "disconnect_websocket",
                    None,
                )

            if method is not None:
                result = method()

                if inspect.isawaitable(result):
                    await result

        except Exception as exc:
            logger.warning(
                "Pocket Option disconnect error: %s",
                exc,
            )

        self.client = None
        self.started = False

    # ========================================================
    # CONNECT
    # ========================================================

    async def _connect_pocket_option(self) -> None:
        """
        Try supported unofficial Pocket Option clients.

        We intentionally do not hard-code a single third-party
        package because their APIs change frequently.
        """

        client = None

        # ----------------------------------------------------
        # New async-style client
        # ----------------------------------------------------

        try:
            from pocketoptionapi_async import (
                AsyncPocketOptionClient,
            )

            client = AsyncPocketOptionClient(
                config.po_ssid,
                is_demo=config.po_demo,
                enable_logging=False,
            )

            result = client.connect()

            if inspect.isawaitable(result):
                await result

            self.client = client

            logger.info(
                "Connected to Pocket Option OTC feed "
                "using AsyncPocketOptionClient"
            )

            return

        except ImportError:
            pass

        except Exception as exc:
            logger.warning(
                "Async Pocket Option client connection failed: %s",
                exc,
            )

        # ----------------------------------------------------
        # Stable-style client
        # ----------------------------------------------------

        try:
            from pocketoptionapi.stable_api import (
                PocketOption,
            )

            client = PocketOption(
                ssid=config.po_ssid,
                demo=config.po_demo,
            )

            result = client.connect()

            if inspect.isawaitable(result):
                await result

            self.client = client

            logger.info(
                "Connected to Pocket Option OTC feed "
                "using stable PocketOption client"
            )

            return

        except ImportError:
            pass

        except Exception as exc:
            logger.warning(
                "Stable Pocket Option client connection failed: %s",
                exc,
            )

        # ----------------------------------------------------
        # No client
        # ----------------------------------------------------

        self.client = None

        logger.error(
            "No compatible Pocket Option API client is installed. "
            "Install a compatible Pocket Option data client and "
            "configure PO_SSID in Render."
        )

    # ========================================================
    # CANDLES
    # ========================================================

    async def candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int = 200,
    ) -> list[Candle]:

        await self.start()

        if not config.is_otc_pair(pair):
            logger.warning(
                "Rejected non-OTC pair: %s",
                pair,
            )
            return []

        symbol = config.pocket_symbol(pair)

        if not symbol:
            logger.warning(
                "No Pocket Option symbol for: %s",
                pair,
            )
            return []

        cache_key = (
            f"{symbol}:{minutes}:{limit}"
        )

        cached = self.cache.get(cache_key)

        if cached:
            created, candles = cached

            if time.time() - created < self.cache_ttl:
                return candles

        if self.client is None:
            logger.warning(
                "No Pocket Option client available for %s",
                symbol,
            )
            return []

        async with self._lock:

            # Another task could have filled the cache.
            cached = self.cache.get(cache_key)

            if cached:
                created, candles = cached

                if time.time() - created < self.cache_ttl:
                    return candles

            try:
                candles = await self._get_pocket_candles(
                    symbol=symbol,
                    minutes=minutes,
                    limit=limit,
                )

                candles = self._clean_candles(
                    candles,
                    limit,
                )

                if candles:
                    self.cache[cache_key] = (
                        time.time(),
                        candles,
                    )

                return candles

            except Exception as exc:
                logger.exception(
                    "Failed to load OTC candles for %s: %s",
                    symbol,
                    exc,
                )

                return []

    # ========================================================
    # POCKET CANDLES
    # ========================================================

    async def _get_pocket_candles(
        self,
        symbol: str,
        minutes: int,
        limit: int,
    ) -> list[Candle]:

        period = int(minutes * 60)

        client = self.client

        # ----------------------------------------------------
        # get_historical_candles
        # ----------------------------------------------------

        method = getattr(
            client,
            "get_historical_candles",
            None,
        )

        if method is not None:

            result = method(
                symbol,
                period=period,
                offset=max(
                    9000,
                    limit * 20,
                ),
                count_request=1,
            )

            if inspect.isawaitable(result):
                result = await result

            return self._normalize_candles(
                result
            )

        # ----------------------------------------------------
        # get_candles
        # ----------------------------------------------------

        method = getattr(
            client,
            "get_candles",
            None,
        )

        if method is not None:

            result = method(
                symbol,
                period,
                limit,
            )

            if inspect.isawaitable(result):
                result = await result

            return self._normalize_candles(
                result
            )

        # ----------------------------------------------------
        # get_candles_dataframe
        # ----------------------------------------------------

        method = getattr(
            client,
            "get_candles_dataframe",
            None,
        )

        if method is not None:

            timeframe = f"{minutes}m"

            result = method(
                asset=symbol,
                timeframe=timeframe,
                count=limit,
            )

            if inspect.isawaitable(result):
                result = await result

            return self._normalize_dataframe(
                result
            )

        raise RuntimeError(
            "Installed Pocket Option client does not expose "
            "a supported historical candle method."
        )

    # ========================================================
    # NORMALIZE
    # ========================================================

    def _normalize_candles(
        self,
        data: Any,
    ) -> list[Candle]:

        if data is None:
            return []

        if hasattr(data, "to_dict"):
            return self._normalize_dataframe(data)

        if isinstance(data, dict):

            for key in (
                "candles",
                "data",
                "result",
                "history",
            ):
                if key in data:
                    data = data[key]
                    break

        if not isinstance(data, (list, tuple)):
            return []

        output: list[Candle] = []

        for item in data:

            try:

                if isinstance(item, Candle):
                    output.append(item)
                    continue

                if isinstance(item, dict):

                    timestamp = (
                        item.get("timestamp")
                        or item.get("time")
                        or item.get("from")
                        or item.get("at")
                    )

                    open_price = (
                        item.get("open")
                        or item.get("o")
                    )

                    high_price = (
                        item.get("high")
                        or item.get("h")
                    )

                    low_price = (
                        item.get("low")
                        or item.get("l")
                    )

                    close_price = (
                        item.get("close")
                        or item.get("c")
                    )

                    volume = (
                        item.get("volume")
                        or item.get("v")
                        or 0
                    )

                else:

                    timestamp = getattr(
                        item,
                        "timestamp",
                        getattr(item, "time", None),
                    )

                    open_price = getattr(
                        item,
                        "open",
                        None,
                    )

                    high_price = getattr(
                        item,
                        "high",
                        None,
                    )

                    low_price = getattr(
                        item,
                        "low",
                        None,
                    )

                    close_price = getattr(
                        item,
                        "close",
                        None,
                    )

                    volume = getattr(
                        item,
                        "volume",
                        0,
                    )

                if None in (
                    timestamp,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                ):
                    continue

                timestamp = int(float(timestamp))

                # Some APIs return milliseconds.
                if timestamp > 10_000_000_000:
                    timestamp //= 1000

                output.append(
                    Candle(
                        timestamp=timestamp,
                        open=float(open_price),
                        high=float(high_price),
                        low=float(low_price),
                        close=float(close_price),
                        volume=float(volume or 0),
                    )
                )

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                continue

        return output

    # ========================================================
    # DATAFRAME NORMALIZATION
    # ========================================================

    def _normalize_dataframe(
        self,
        dataframe: Any,
    ) -> list[Candle]:

        if dataframe is None:
            return []

        try:
            rows = dataframe.to_dict(
                orient="records"
            )
        except Exception:
            return []

        return self._normalize_candles(rows)

    # ========================================================
    # CLEAN
    # ========================================================

    @staticmethod
    def _clean_candles(
        candles: list[Candle],
        limit: int,
    ) -> list[Candle]:

        if not candles:
            return []

        unique: dict[int, Candle] = {}

        for candle in candles:

            if candle.close <= 0:
                continue

            if candle.high <= 0:
                continue

            if candle.low <= 0:
                continue

            if candle.high < candle.low:
                continue

            unique[candle.timestamp] = candle

        result = sorted(
            unique.values(),
            key=lambda x: x.timestamp,
        )

        return result[-limit:]


# ============================================================
# GLOBAL MARKET CLIENT
# ============================================================

market = MarketClient()
