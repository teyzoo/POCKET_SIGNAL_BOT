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

        self.ssid = None

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
                headless=True
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
                    'input[name="login"]'
                ).first

                password_input = page.locator(
                    'input[type="password"], '
                    'input[name="password"]'
                ).first

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

                await submit.click()

                await page.wait_for_timeout(
                    8000
                )

                cookies = (
                    await context.cookies()
                )

                ssid = None

                for cookie in cookies:

                    if (
                        cookie["name"]
                        .lower()
                        == "ssid"
                    ):

                        ssid = cookie["value"]

                        break

                if not ssid:

                    raise RuntimeError(
                        "Pocket Option не выдал SSID после входа. "
                        "Возможна CAPTCHA, дополнительная проверка "
                        "или изменение страницы входа."
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

                ssid = (
                    await self.auto_login()
                )

            if not ssid:

                raise RuntimeError(
                    "Нет сессии Pocket Option. "
                    "Укажи PO_EMAIL/PO_PASSWORD "
                    "или PO_SSID."
                )

            from BinaryOptionsToolsV2.pocketoption import (
                PocketOptionAsync,
            )

            self.client = (
                PocketOptionAsync(ssid)
            )

            self.ssid = ssid

            await asyncio.sleep(5)

            logger.info(
                "Pocket Option OTC клиент подключён."
            )

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

        # Берём 1-минутные OTC свечи.
        # timeframe используется как срок экспирации.
        raw = await self.client.get_candles(
            symbol,
            60,
            max(
                3600,
                limit * 60,
            ),
        )

        if not raw:

            return []

        result: list[Candle] = []

        for item in raw[-limit:]:

            try:

                timestamp = item.get(
                    "time",
                    item.get(
                        "timestamp"
                    ),
                )

                if isinstance(
                    timestamp,
                    str,
                ):

                    dt = datetime.fromisoformat(
                        timestamp.replace(
                            "Z",
                            "+00:00",
                        )
                    )

                else:

                    dt = datetime.fromtimestamp(
                        float(timestamp),
                        tz=timezone.utc,
                    )

                result.append(
                    Candle(
                        time=dt,
                        open=float(
                            item["open"]
                        ),
                        high=float(
                            item["high"]
                        ),
                        low=float(
                            item["low"]
                        ),
                        close=float(
                            item["close"]
                        ),
                        volume=float(
                            item.get(
                                "volume",
                                0,
                            )
                            or 0
                        ),
                    )
                )

            except Exception:

                continue

        return result

    async def close(self):

        if self.client is None:

            return

        try:

            await self.client.shutdown()

        except Exception:

            logger.exception(
                "Ошибка закрытия Pocket Option"
            )

        self.client = None


market = PocketMarket()
