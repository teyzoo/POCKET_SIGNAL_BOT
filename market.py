from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from config import config


logger = logging.getLogger("pocket_market")


@dataclass(slots=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class PocketMarket:

    def __init__(self):
        self.client = None
        self.ssid: str | None = None
        self.lock = asyncio.Lock()

    async def auto_login(self) -> str:

        if not config.po_email or not config.po_password:
            raise RuntimeError(
                "PO_EMAIL или PO_PASSWORD не заданы."
            )

        from playwright.async_api import async_playwright

        logger.info(
            "Пробую обычный вход Pocket Option..."
        )

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=True
            )

            context = await browser.new_context()
            page = await context.new_page()

            try:

                await page.goto(
                    config.po_login_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                await page.wait_for_timeout(3000)

                email_input = page.locator(
                    'input[type="email"], '
                    'input[name="email"], '
                    'input[name="login"], '
                    'input[placeholder*="email" i]'
                ).first

                password_input = page.locator(
                    'input[type="password"], '
                    'input[name="password"]'
                ).first

                if await email_input.count() == 0:
                    raise RuntimeError(
                        "Поле E-mail Pocket Option не найдено."
                    )

                if await password_input.count() == 0:
                    raise RuntimeError(
                        "Поле пароля Pocket Option не найдено."
                    )

                await email_input.fill(
                    config.po_email
                )

                await password_input.fill(
                    config.po_password
                )

                submit = page.locator(
                    'button[type="submit"], '
                    'input[type="submit"]'
                ).first

                if await submit.count() == 0:
                    raise RuntimeError(
                        "Кнопка входа Pocket Option не найдена."
                    )

                await submit.click()

                await page.wait_for_timeout(8000)

                cookies = await context.cookies()

                ssid = next(
                    (
                        str(c.get("value"))
                        for c in cookies
                        if str(
                            c.get("name", "")
                        ).lower() == "ssid"
                    ),
                    None,
                )

                if not ssid:
                    raise RuntimeError(
                        "Pocket Option не выдал SSID после входа. "
                        "Возможна CAPTCHA, 2FA или дополнительная проверка."
                    )

                logger.info(
                    "Сессия Pocket Option получена."
                )

                return ssid

            finally:
                await browser.close()

    async def connect(self):

        async with self.lock:

            if self.client is not None:
                return

            ssid = config.po_ssid.strip()

            if (
                not ssid
                and config.po_auto_login
            ):
                ssid = await self.auto_login()

            if not ssid:
                raise RuntimeError(
                    "Нет сессии Pocket Option. "
                    "Задай PO_SSID либо PO_EMAIL + PO_PASSWORD."
                )

            from BinaryOptionsToolsV2.pocketoption import (
                PocketOptionAsync
            )

            self.client = PocketOptionAsync(
                ssid
            )

            self.ssid = ssid

            await asyncio.sleep(5)

            logger.info(
                "Pocket Option OTC клиент подключён."
            )

    @staticmethod
    def _timestamp(value) -> datetime:

        if isinstance(value, datetime):

            if value.tzinfo is None:
                return value.replace(
                    tzinfo=timezone.utc
                )

            return value.astimezone(
                timezone.utc
            )

        if isinstance(value, str):

            text = value.strip()

            try:

                dt = datetime.fromisoformat(
                    text.replace(
                        "Z",
                        "+00:00"
                    )
                )

                if dt.tzinfo is None:
                    dt = dt.replace(
                        tzinfo=timezone.utc
                    )

                return dt.astimezone(
                    timezone.utc
                )

            except ValueError:
                value = float(text)

        return datetime.fromtimestamp(
            float(value),
            tz=timezone.utc
        )

    @staticmethod
    def _read(
        item,
        name,
        default=None
    ):

        if isinstance(item, dict):
            return item.get(
                name,
                default
            )

        return getattr(
            item,
            name,
            default
        )

    def _parse_candle(
        self,
        item
    ) -> Candle | None:

        try:

            timestamp = self._read(
                item,
                "time"
            )

            if timestamp is None:
                timestamp = self._read(
                    item,
                    "timestamp"
                )

            if timestamp is None:
                timestamp = self._read(
                    item,
                    "from"
                )

            if timestamp is None:
                return None

            values = [
                self._read(item, "open"),
                self._read(item, "high"),
                self._read(item, "low"),
                self._read(item, "close"),
            ]

            if any(
                value is None
                for value in values
            ):
                return None

            candle = Candle(
                time=self._timestamp(
                    timestamp
                ),
                open=float(values[0]),
                high=float(values[1]),
                low=float(values[2]),
                close=float(values[3]),
                volume=float(
                    self._read(
                        item,
                        "volume",
                        0
                    ) or 0
                ),
            )

            prices = (
                candle.open,
                candle.high,
                candle.low,
                candle.close,
            )

            if not all(
                x == x
                and abs(x) != float("inf")
                for x in prices
            ):
                return None

            if candle.high < max(
                candle.open,
                candle.close
            ):
                return None

            if candle.low > min(
                candle.open,
                candle.close
            ):
                return None

            if candle.high < candle.low:
                return None

            return candle

        except Exception:
            return None

    async def _get_raw_candles(
        self,
        symbol: str,
        limit: int
    ):

        live = getattr(
            self.client,
            "get_candles_live",
            None
        )

        if live is not None:

            try:

                stream = live(
                    symbol,
                    period=60,
                    hours=max(
                        2.0,
                        limit / 60.0
                    ),
                    max_rows=max(
                        limit,
                        200
                    ),
                )

                closed, forming = await asyncio.wait_for(
                    anext(stream),
                    timeout=25
                )

                logger.info(
                    "OTC %s: получено закрытых свечей=%s, forming=%s",
                    symbol,
                    len(closed or []),
                    bool(forming),
                )

                if closed:
                    return closed

            except Exception:
                logger.exception(
                    "get_candles_live ошибка для %s",
                    symbol
                )

        get_method = getattr(
            self.client,
            "get_candles",
            None
        )

        if get_method is None:
            raise RuntimeError(
                "PocketOptionAsync не содержит метода получения свечей."
            )

        try:

            return await asyncio.wait_for(
                get_method(
                    symbol,
                    60,
                    max(
                        3600,
                        limit * 60
                    )
                ),
                timeout=20
            )

        except Exception as exc:

            raise RuntimeError(
                f"Pocket Option не смог получить свечи "
                f"для {symbol}: {exc}"
            ) from exc

    async def candles(
        self,
        pair: str,
        minutes: int = 5,
        limit: int = 200
    ) -> list[Candle]:

        await self.connect()

        symbol = config.otc_symbols.get(
            pair
        )

        if not symbol:
            raise ValueError(
                f"Неизвестная OTC-пара: {pair}"
            )

        limit = max(
            60,
            min(
                int(limit),
                1000
            )
        )

        logger.info(
            "ПРОВЕРКА OTC РЫНКА: %s -> %s",
            pair,
            symbol
        )

        raw = await self._get_raw_candles(
            symbol,
            limit
        )

        if not raw:
            raise RuntimeError(
                f"Pocket Option не вернул свечи для {pair}."
            )

        if isinstance(raw, dict):

            for key in (
                "candles",
                "data",
                "result"
            ):

                if key in raw:
                    raw = raw[key]
                    break

        try:
            items = list(raw)

        except TypeError as exc:

            raise RuntimeError(
                f"Неизвестный формат свечей для {pair}."
            ) from exc

        parsed = []

        for item in items:

            candle = self._parse_candle(
                item
            )

            if candle is not None:
                parsed.append(
                    candle
                )

        unique = {
            candle.time: candle
            for candle in parsed
        }

        result = sorted(
            unique.values(),
            key=lambda candle: candle.time
        )[-limit:]

        if len(result) < 60:

            raise RuntimeError(
                f"Получено только {len(result)} "
                f"корректных свечей для {pair}; "
                f"нужно минимум 60."
            )

        age = (
            datetime.now(timezone.utc)
            - result[-1].time
        ).total_seconds()

        logger.info(
            "OTC %s: %s свечей, последняя=%s, возраст=%.1f сек.",
            pair,
            len(result),
            result[-1].time.isoformat(),
            age,
        )

        if age > 180:

            raise RuntimeError(
                f"OTC-данные для {pair} устарели: "
                f"последняя свеча была {age:.0f} сек. назад."
            )

        return result

    async def close(self):

        if self.client is None:
            return

        try:

            shutdown = getattr(
                self.client,
                "shutdown",
                None
            )

            if shutdown is not None:

                value = shutdown()

                if asyncio.iscoroutine(
                    value
                ):
                    await value

        except Exception:

            logger.exception(
                "Ошибка закрытия Pocket Option"
            )

        finally:

            self.client = None
            self.ssid = None


market = PocketMarket()
