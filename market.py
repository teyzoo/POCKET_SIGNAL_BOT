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

    # ========================================================
    # LOGIN
    # ========================================================

    async def auto_login(self) -> str:

        if not config.po_email:
            raise RuntimeError(
                "PO_EMAIL не задан."
            )

        if not config.po_password:
            raise RuntimeError(
                "PO_PASSWORD не задан."
            )

        logger.info(
            "Запускаю автоматическую авторизацию Pocket Option..."
        )

        try:
            from BinaryOptionsToolsV2.pocketoption.tools.login import (
                login,
            )
        except Exception as exc:
            raise RuntimeError(
                "Не удалось импортировать Pocket Option login: "
                f"{exc}"
            ) from exc

        try:
            ssid = await asyncio.to_thread(
                login,
                config.po_email,
                config.po_password,
                demo=config.po_demo,
                backend="auto",
                headless=True,
                timeout=60,
            )

        except Exception as exc:
            logger.exception(
                "Pocket Option automatic login failed"
            )

            raise RuntimeError(
                "Не удалось авторизоваться в Pocket Option: "
                f"{exc}"
            ) from exc

        if not ssid:
            raise RuntimeError(
                "Pocket Option login не вернул SSID."
            )

        logger.info(
            "Pocket Option SSID успешно получен."
        )

        return str(ssid)

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self):

        async with self.lock:

            if self.client is not None:
                return

            ssid = (
                config.po_ssid.strip()
                if config.po_ssid
                else ""
            )

            if not ssid:

                if not config.po_auto_login:
                    raise RuntimeError(
                        "PO_SSID не задан, "
                        "а PO_AUTO_LOGIN выключен."
                    )

                ssid = await self.auto_login()

            if not ssid:
                raise RuntimeError(
                    "Не удалось получить Pocket Option SSID."
                )

            try:
                from BinaryOptionsToolsV2.pocketoption import (
                    PocketOptionAsync,
                )
            except Exception as exc:
                raise RuntimeError(
                    "BinaryOptionsToolsV2 не импортируется: "
                    f"{exc}"
                ) from exc

            try:

                logger.info(
                    "Создаю PocketOptionAsync клиент..."
                )

                client = PocketOptionAsync(
                    ssid
                )

                self.client = client
                self.ssid = ssid

                # Небольшая инициализация WebSocket.
                await asyncio.sleep(5)

                logger.info(
                    "Pocket Option клиент создан."
                )

            except Exception as exc:

                self.client = None
                self.ssid = None

                logger.exception(
                    "Ошибка создания Pocket Option клиента: %s",
                    exc,
                )

                raise RuntimeError(
                    "Не удалось создать Pocket Option клиент: "
                    f"{exc}"
                ) from exc

    # ========================================================
    # TIME
    # ========================================================

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
                value = float(text)

        number = float(value)

        if number > 10_000_000_000:
            number /= 1000.0

        return datetime.fromtimestamp(
            number,
            tz=timezone.utc,
        )

    # ========================================================
    # READ
    # ========================================================

    @staticmethod
    def _read(
        item,
        name,
        default=None,
    ):

        if isinstance(item, dict):
            return item.get(
                name,
                default,
            )

        return getattr(
            item,
            name,
            default,
        )

    # ========================================================
    # PARSE
    # ========================================================

    def _parse_candle(
        self,
        item,
    ) -> Candle | None:

        try:

            timestamp = self._read(
                item,
                "time",
            )

            if timestamp is None:
                timestamp = self._read(
                    item,
                    "timestamp",
                )

            if timestamp is None:
                timestamp = self._read(
                    item,
                    "from",
                )

            if timestamp is None:
                return None

            open_price = self._read(
                item,
                "open",
            )

            high_price = self._read(
                item,
                "high",
            )

            low_price = self._read(
                item,
                "low",
            )

            close_price = self._read(
                item,
                "close",
            )

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

            volume = self._read(
                item,
                "volume",
                0,
            )

            candle = Candle(
                time=self._timestamp(
                    timestamp
                ),
                open=float(open_price),
                high=float(high_price),
                low=float(low_price),
                close=float(close_price),
                volume=float(volume or 0),
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
                candle.close,
            ):
                return None

            if candle.low > min(
                candle.open,
                candle.close,
            ):
                return None

            if candle.high < candle.low:
                return None

            return candle

        except Exception:
            return None

    # ========================================================
    # RAW CANDLES
    # ========================================================

    async def _get_raw_candles(
        self,
        symbol: str,
        limit: int,
    ):

        if self.client is None:
            raise RuntimeError(
                "Pocket Option client не подключён."
            )

        # ====================================================
        # LIVE
        # ====================================================

        live_method = getattr(
            self.client,
            "get_candles_live",
            None,
        )

        if live_method is not None:

            try:

                hours = max(
                    2.0,
                    (limit / 60.0) + 0.5,
                )

                logger.info(
                    "LIVE candles request: symbol=%s "
                    "period=60 hours=%.2f max_rows=%s",
                    symbol,
                    hours,
                    limit,
                )

                stream = live_method(
                    symbol,
                    period=60,
                    hours=hours,
                    max_rows=limit,
                )

                # ВАЖНО:
                # get_candles_live() — async generator.
                first = await asyncio.wait_for(
                    anext(stream),
                    timeout=30,
                )

                if not first:
                    raise RuntimeError(
                        "LIVE stream не вернул данные."
                    )

                closed = None
                forming = None

                try:
                    closed, forming = first
                except Exception:
                    closed = first

                logger.info(
                    "LIVE response: closed=%s forming=%s",
                    len(closed or []),
                    bool(forming),
                )

                if closed:
                    return closed

            except Exception as exc:

                logger.exception(
                    "get_candles_live(%s) failed: %s",
                    symbol,
                    exc,
                )

        # ====================================================
        # FALLBACK
        # ====================================================

        get_method = getattr(
            self.client,
            "get_candles",
            None,
        )

        if get_method is None:
            raise RuntimeError(
                "PocketOptionAsync не содержит get_candles()."
            )

        try:

            offset = max(
                3600,
                limit * 60,
            )

            logger.info(
                "Fallback get_candles: "
                "symbol=%s period=60 offset=%s",
                symbol,
                offset,
            )

            raw = await asyncio.wait_for(
                get_method(
                    symbol,
                    60,
                    offset,
                ),
                timeout=30,
            )

            if not raw:
                raise RuntimeError(
                    "get_candles() вернул пустой результат."
                )

            return raw

        except Exception as exc:

            logger.exception(
                "get_candles(%s) failed: %s",
                symbol,
                exc,
            )

            raise RuntimeError(
                "Pocket Option не смог получить свечи "
                f"для {symbol}: {exc}"
            ) from exc

    # ========================================================
    # REQUIRED CANDLES
    # ========================================================

    @staticmethod
    def required_1m_candles(
        timeframe: int,
    ) -> int:

        timeframe = int(timeframe)

        # Нужно минимум 60 свечей выбранного TF.
        #
        # Добавляем запас для неполных минут,
        # пропусков и формирования последней свечи.

        required = (
            timeframe * 60
        )

        return max(
            200,
            required + 120,
        )

    # ========================================================
    # PUBLIC CANDLES
    # ========================================================

    async def candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int | None = None,
    ) -> list[Candle]:

        await self.connect()

        symbol = config.otc_symbols.get(
            pair
        )

        if not symbol:

            if pair.endswith("_otc"):
                symbol = pair

            else:
                raise ValueError(
                    f"Неизвестная OTC-пара: {pair}"
                )

        minutes = max(
            1,
            int(minutes),
        )

        if limit is None:
            limit = self.required_1m_candles(
                minutes
            )

        limit = max(
            200,
            min(
                int(limit),
                1600,
            ),
        )

        logger.info(
            "================================================"
        )

        logger.info(
            "OTC MARKET REQUEST"
        )

        logger.info(
            "Pair: %s",
            pair,
        )

        logger.info(
            "Symbol: %s",
            symbol,
        )

        logger.info(
            "Minutes: %s",
            minutes,
        )

        logger.info(
            "Limit: %s",
            limit,
        )

        logger.info(
            "================================================"
        )

        raw = await self._get_raw_candles(
            symbol,
            limit,
        )

        if not raw:
            raise RuntimeError(
                f"Pocket Option не вернул свечи "
                f"для {pair}."
            )

        # Wrapper.
        if isinstance(raw, dict):

            for key in (
                "candles",
                "data",
                "result",
            ):

                if key in raw:

                    raw = raw[key]
                    break

        try:
            items = list(raw)

        except TypeError as exc:

            raise RuntimeError(
                f"Неизвестный формат свечей "
                f"для {pair}."
            ) from exc

        parsed: list[Candle] = []

        for item in items:

            candle = self._parse_candle(
                item
            )

            if candle is not None:
                parsed.append(candle)

        # Dedupe.
        unique = {
            candle.time: candle
            for candle in parsed
        }

        result = sorted(
            unique.values(),
            key=lambda candle: candle.time,
        )

        result = result[-limit:]

        logger.info(
            "Parsed candles: pair=%s raw=%s valid=%s",
            pair,
            len(items),
            len(result),
        )

        if len(result) < 60:

            raise RuntimeError(
                f"Получено только "
                f"{len(result)} корректных свечей "
                f"для {pair}. "
                f"Нужно минимум 60."
            )

        # ====================================================
        # FRESHNESS
        # ====================================================

        now = datetime.now(
            timezone.utc
        )

        last_time = result[-1].time

        age = (
            now - last_time
        ).total_seconds()

        logger.info(
            "OTC %s: candles=%s last=%s age=%.1fs",
            pair,
            len(result),
            last_time.isoformat(),
            age,
        )

        if age > 180:

            raise RuntimeError(
                f"OTC-данные для {pair} устарели. "
                f"Последняя свеча "
                f"{age:.0f} секунд назад."
            )

        if age < -30:

            raise RuntimeError(
                f"Время последней свечи {pair} "
                f"находится в будущем."
            )

        logger.info(
            "OTC DATA OK: %s",
            pair,
        )

        return result

    # ========================================================
    # CLOSE
    # ========================================================

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

                result = shutdown()

                if asyncio.iscoroutine(
                    result
                ):
                    await result

        except Exception:

            logger.exception(
                "Ошибка закрытия Pocket Option."
            )

        finally:

            self.client = None
            self.ssid = None


# ============================================================
# GLOBAL MARKET
# ============================================================

market = PocketMarket()
