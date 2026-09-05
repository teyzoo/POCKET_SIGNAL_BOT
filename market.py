from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from config import config


logger = logging.getLogger(
    "pocket_market"
)


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

        if (
            not config.po_email
            or not config.po_password
        ):

            raise RuntimeError(
                "PO_EMAIL или PO_PASSWORD не заданы."
            )

        from playwright.async_api import (
            async_playwright,
        )

        logger.info(
            "Пробую обычный вход Pocket Option..."
        )

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=True,
            )

            context = (
                await browser.new_context()
            )

            page = await context.new_page()

            try:

                await page.goto(
                    config.po_login_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                await page.wait_for_timeout(
                    3000
                )

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
                        "Поле E-mail на странице Pocket Option "
                        "не найдено."
                    )

                if await password_input.count() == 0:
                    raise RuntimeError(
                        "Поле пароля на странице Pocket Option "
                        "не найдено."
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
                        "Кнопка входа Pocket Option "
                        "не найдена."
                    )

                await submit.click()

                await page.wait_for_timeout(
                    8000
                )

                cookies = (
                    await context.cookies()
                )

                ssid = None

                for cookie in cookies:

                    name = str(
                        cookie.get(
                            "name",
                            "",
                        )
                    ).lower()

                    if name == "ssid":

                        ssid = cookie.get(
                            "value"
                        )

                        break

                if not ssid:

                    raise RuntimeError(
                        "Pocket Option не выдал SSID после входа. "
                        "Возможна CAPTCHA, 2FA, дополнительная "
                        "проверка или изменение страницы входа."
                    )

                logger.info(
                    "Сессия Pocket Option получена."
                )

                return str(ssid)

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

                ssid = (
                    await self.auto_login()
                )

            if not ssid:

                raise RuntimeError(
                    "Нет сессии Pocket Option. "
                    "Задай PO_SSID либо "
                    "PO_EMAIL + PO_PASSWORD."
                )

            from BinaryOptionsToolsV2.pocketoption import (
                PocketOptionAsync,
            )

            self.client = (
                PocketOptionAsync(
                    ssid
                )
            )

            self.ssid = ssid

            await asyncio.sleep(5)

            logger.info(
                "Pocket Option OTC клиент подключён."
            )

    @staticmethod
    def _timestamp(value) -> datetime:

        if isinstance(
            value,
            datetime,
        ):

            if value.tzinfo is None:

                return value.replace(
                    tzinfo=timezone.utc
                )

            return value.astimezone(
                timezone.utc
            )

        if isinstance(
            value,
            str,
        ):

            text = value.strip()

            try:

                dt = datetime.fromisoformat(
                    text.replace(
                        "Z",
                        "+00:00",
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

                return datetime.fromtimestamp(
                    float(text),
                    tz=timezone.utc,
                )

        return datetime.fromtimestamp(
            float(value),
            tz=timezone.utc,
        )

    @staticmethod
    def _read(item, name, default=None):

        if isinstance(
            item,
            dict,
        ):

            return item.get(
                name,
                default,
            )

        return getattr(
            item,
            name,
            default,
        )

    def _parse_candle(
        self,
        item,
    ) -> Candle | None:

        try:

            timestamp = self._read(
                item,
                "time",
                None,
            )

            if timestamp is None:

                timestamp = self._read(
                    item,
                    "timestamp",
                    None,
                )

            if timestamp is None:

                timestamp = self._read(
                    item,
                    "from",
                    None,
                )

            if timestamp is None:

                return None

            open_price = self._read(
                item,
                "open",
                None,
            )

            high_price = self._read(
                item,
                "high",
                None,
            )

            low_price = self._read(
                item,
                "low",
                None,
            )

            close_price = self._read(
                item,
                "close",
                None,
            )

            if any(
                x is None
                for x in (
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                )
            ):

                return None

            volume = self._read(
                item,
                "volume",
                0,
            )

            return Candle(
                time=self._timestamp(
                    timestamp
                ),
                open=float(
                    open_price
                ),
                high=float(
                    high_price
                ),
                low=float(
                    low_price
                ),
                close=float(
                    close_price
                ),
                volume=float(
                    volume or 0
                ),
            )

        except Exception:

            return None

    async def candles(
        self,
        pair: str,
        minutes: int = 5,
        limit: int = 200,
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
                1000,
            ),
        )

        raw = None

        # Сначала используем live-метод,
        # если он есть в установленной версии библиотеки.
        live_method = getattr(
            self.client,
            "get_candles_live",
            None,
        )

        if live_method is not None:

            try:

                raw = await live_method(
                    symbol,
                    60,
                )

            except TypeError:

                try:

                    raw = await live_method(
                        symbol,
                        period=60,
                    )

                except Exception:

                    logger.exception(
                        "Ошибка get_candles_live для %s",
                        symbol,
                    )

            except Exception:

                logger.exception(
                    "Ошибка get_candles_live для %s",
                    symbol,
                )

        # Fallback для версий библиотеки,
        # где доступен старый get_candles.
        if not raw:

            get_method = getattr(
                self.client,
                "get_candles",
                None,
            )

            if get_method is None:

                raise RuntimeError(
                    "У PocketOptionAsync нет метода "
                    "получения свечей."
                )

            raw = await get_method(
                symbol,
                60,
                max(
                    3600,
                    limit * 60,
                ),
            )

        if not raw:

            return []

        # Некоторые версии могут вернуть dict.
        if isinstance(
            raw,
            dict,
        ):

            for key in (
                "candles",
                "data",
                "result",
            ):

                if key in raw:

                    raw = raw[key]

                    break

        if not isinstance(
            raw,
            (list, tuple),
        ):

            try:

                raw = list(raw)

            except TypeError:

                return []

        result: list[Candle] = []

        for item in raw:

            candle = self._parse_candle(
                item
            )

            if candle is not None:

                result.append(
                    candle
                )

        result.sort(
            key=lambda x: x.time
        )

        # Удаляем дубликаты по времени.
        unique: dict[
            datetime,
            Candle,
        ] = {}

        for candle in result:

            unique[
                candle.time
            ] = candle

        result = list(
            unique.values()
        )

        result.sort(
            key=lambda x: x.time
        )

        return result[-limit:]

    async def close(self):

        if self.client is None:

            return

        try:

            shutdown = getattr(
                self.client,
                "shutdown",
                None,
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
