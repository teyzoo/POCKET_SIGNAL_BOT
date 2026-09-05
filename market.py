from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp

from config import config


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class MarketClient:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, tuple[float, list[Candle]]] = {}

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "User-Agent": "Mozilla/5.0"
                },
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int = 200,
    ) -> list[Candle]:

        await self.start()

        symbol = config.yahoo_symbols.get(pair)

        if not symbol:
            return []

        cache_key = f"{symbol}:{minutes}:{limit}"

        cached = self.cache.get(cache_key)

        if cached:
            timestamp, data = cached

            if time.time() - timestamp < 20:
                return data

        interval = self._yahoo_interval(minutes)

        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}"
        )

        params = {
            "range": "5d",
            "interval": interval,
            "includePrePost": "true",
            "events": "div,splits",
        }

        try:
            async with self.session.get(
                url,
                params=params,
            ) as response:

                if response.status != 200:
                    return []

                data = await response.json()

            result = data.get("chart", {}).get("result")

            if not result:
                return []

            result = result[0]

            timestamps = result.get("timestamp", [])
            quote = (
                result.get("indicators", {})
                .get("quote", [{}])[0]
            )

            opens = quote.get("open", [])
            highs = quote.get("high", [])
            lows = quote.get("low", [])
            closes = quote.get("close", [])
            volumes = quote.get("volume", [])

            candles: list[Candle] = []

            for i, timestamp in enumerate(timestamps):
                try:
                    o = opens[i]
                    h = highs[i]
                    l = lows[i]
                    c = closes[i]

                    if None in (o, h, l, c):
                        continue

                    candles.append(
                        Candle(
                            timestamp=int(timestamp),
                            open=float(o),
                            high=float(h),
                            low=float(l),
                            close=float(c),
                            volume=float(
                                volumes[i] or 0
                            )
                            if i < len(volumes)
                            else 0.0,
                        )
                    )

                except (IndexError, TypeError, ValueError):
                    continue

            candles = candles[-limit:]

            self.cache[cache_key] = (
                time.time(),
                candles,
            )

            return candles

        except Exception:
            return []

    @staticmethod
    def _yahoo_interval(minutes: int) -> str:
        if minutes <= 1:
            return "1m"

        if minutes <= 2:
            return "2m"

        if minutes <= 5:
            return "5m"

        if minutes <= 15:
            return "15m"

        return "30m"


market = MarketClient()
