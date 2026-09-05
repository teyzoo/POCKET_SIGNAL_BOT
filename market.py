from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import config


logger = logging.getLogger("pocket_market")


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


# ============================================================
# POCKET MARKET
# ============================================================

class PocketMarket:

    def __init__(self):
        self.client: Any | None = None
        self.ssid: str | None = None

        self.lock = asyncio.Lock()
        self.connected = False

        self.last_error: str | None = None
        self.last_success: datetime | None = None

    # ========================================================
    # STATUS
    # ========================================================

    @property
    def is_connected(self) -> bool:
        return bool(
            self.client is not None
            and self.connected
        )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> str:
        """
        Резервный автоматический вход.

        Для Render предпочтительнее использовать PO_SSID.
        Если PO_SSID задан, этот метод вообще не вызывается.
        """

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
                "Автоматическая авторизация Pocket Option "
                f"не удалась: {exc}"
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

    async def connect(self) -> bool:
        """
        Подключение к Pocket Option.

        Приоритет:

        1. PO_SSID
        2. PO_EMAIL + PO_PASSWORD

        Если SSID указан, автоматический браузерный login
        никогда не запускается.
        """

        async with self.lock:

            # Уже подключены.
            if self.client is not None and self.connected:
                return True

            self.last_error = None

            # ------------------------------------------------
            # Получаем SSID
            # ------------------------------------------------

            ssid = ""

            if config.po_ssid:
                ssid = config.po_ssid.strip()

                logger.info(
                    "Использую PO_SSID из Render Environment."
                )

            else:

                if not config.po_auto_login:
                    raise RuntimeError(
                        "PO_SSID не задан, а PO_AUTO_LOGIN выключен."
                    )

                logger.warning(
                    "PO_SSID не задан. "
                    "Переходим к автоматическому входу."
                )

                ssid = await self.auto_login()

            if not ssid:
                raise RuntimeError(
                    "Не удалось получить Pocket Option SSID."
                )

            # ------------------------------------------------
            # Импорт клиента
            # ------------------------------------------------

            try:
                from BinaryOptionsToolsV2.pocketoption import (
                    PocketOptionAsync,
                )

            except Exception as exc:

                raise RuntimeError(
                    "BinaryOptionsToolsV2 не импортируется: "
                    f"{exc}"
                ) from exc

            # ------------------------------------------------
            # Создание клиента
            # ------------------------------------------------

            try:

                logger.info(
                    "Создаю PocketOptionAsync клиент..."
                )

                client = PocketOptionAsync(
                    ssid
                )

                self.client = client
                self.ssid = ssid

                # Библиотеке необходимо время
                # для инициализации WebSocket.
                await asyncio.sleep(5)

                # ------------------------------------------------
                # Проверяем, что клиент реально отвечает.
                #
                # balance() используется только как health-check.
                # Торговые операции НЕ выполняются.
                # ------------------------------------------------

                try:

                    balance_method = getattr(
                        client,
                        "balance",
                        None,
                    )

                    if balance_method is not None:

                        await asyncio.wait_for(
                            balance_method(),
                            timeout=15,
                        )

                        logger.info(
                            "Pocket Option connection health-check OK."
                        )

                except Exception as health_exc:

                    logger.warning(
                        "Pocket Option health-check balance "
                        "не прошёл: %s",
                        health_exc,
                    )

                    # Не считаем это автоматически критической
                    # ошибкой: некоторые версии API могут
                    # подключиться к WebSocket раньше,
                    # чем balance станет доступен.

                self.connected = True
                self.last_success = datetime.now(
                    timezone.utc
                )

                logger.info(
                    "=============================================="
                )

                logger.info(
                    "POCKET OPTION CONNECTED"
                )

                logger.info(
                    "SSID: configured"
                )

                logger.info(
                    "Demo: %s",
                    config.po_demo,
                )

                logger.info(
                    "=============================================="
                )

                return True

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                self.client = None
                self.ssid = None
                self.connected = False

                self.last_error = str(exc)

                logger.exception(
                    "Ошибка подключения Pocket Option: %s",
                    exc,
                )

                raise RuntimeError(
                    "Не удалось подключиться к Pocket Option: "
                    f"{exc}"
                ) from exc

    # ========================================================
    # RECONNECT
    # ========================================================

    async def reconnect(self) -> bool:

        logger.warning(
            "Переподключение к Pocket Option..."
        )

        await self.close()

        await asyncio.sleep(1)

        return await self.connect()

    # ========================================================
    # TIMESTAMP
    # ========================================================

    @staticmethod
    def _timestamp(value: Any) -> datetime:

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

        # milliseconds -> seconds
        if number > 10_000_000_000:
            number /= 1000.0

        return datetime.fromtimestamp(
            number,
            tz=timezone.utc,
        )

    # ========================================================
    # SAFE READ
    # ========================================================

    @staticmethod
    def _read(
        item: Any,
        name: str,
        default: Any = None,
    ) -> Any:

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
    # PARSE CANDLE
    # ========================================================

    def _parse_candle(
        self,
        item: Any,
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
                volume=float(
                    volume or 0
                ),
            )

            prices = (
                candle.open,
                candle.high,
                candle.low,
                candle.close,
            )

            # NaN / infinity
            if not all(
                x == x
                and abs(x) != float("inf")
                for x in prices
            ):
                return None

            # OHLC validation
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

            if candle.open <= 0:
                return None

            if candle.high <= 0:
                return None

            if candle.low <= 0:
                return None

            if candle.close <= 0:
                return None

            return candle

        except Exception:
            return None

    # ========================================================
    # REQUIRED 1M CANDLES
    # ========================================================

    @staticmethod
    def required_1m_candles(
        timeframe: int,
    ) -> int:

        timeframe = max(
            1,
            int(timeframe),
        )

        # SignalEngine использует минимум 60 свечей
        # выбранного timeframe.
        #
        # Для 20 минут:
        #
        # 20 * 60 = 1200 минут
        #
        # + запас.
        required = (
            timeframe * 60
        )

        return max(
            240,
            required + 180,
        )

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
        # LIVE CANDLES
        # ====================================================

        live_method = getattr(
            self.client,
            "get_candles_live",
            None,
        )

        if live_method is not None:

            try:

                # 1m candles.
                #
                # limit / 60 = часы истории.
                hours = max(
                    2.0,
                    (limit / 60.0) + 0.5,
                )

                logger.info(
                    "LIVE candles request: "
                    "symbol=%s period=60 hours=%.2f max_rows=%s",
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

                # BinaryOptionsToolsV2:
                #
                # get_candles_live()
                # -> async generator
                #
                # Первый yield:
                # (closed_candles, forming_candle)

                first = await asyncio.wait_for(
                    anext(stream),
                    timeout=45,
                )

                if not first:
                    raise RuntimeError(
                        "LIVE stream не вернул данные."
                    )

                closed = None
                forming = None

                if isinstance(
                    first,
                    tuple,
                ):
                    if len(first) >= 1:
                        closed = first[0]

                    if len(first) >= 2:
                        forming = first[1]

                elif isinstance(
                    first,
                    list,
                ):
                    closed = first

                else:
                    closed = first

                logger.info(
                    "LIVE response: closed=%s forming=%s",
                    len(closed or []),
                    bool(forming),
                )

                if closed:

                    # Важно:
                    # закрытые свечи нужны движку сигналов.
                    return closed

                logger.warning(
                    "LIVE stream подключился, "
                    "но closed candles пустые."
                )

            except asyncio.CancelledError:
                raise

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
                "PocketOptionAsync не содержит "
                "get_candles()."
            )

        try:

            # BinaryOptionsToolsV2 использует offset
            # в секундах.
            #
            # Для N минутных свечей:
            # N * 60 секунд.
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
                timeout=45,
            )

            if not raw:
                raise RuntimeError(
                    "get_candles() вернул пустой результат."
                )

            return raw

        except asyncio.CancelledError:
            raise

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
    # NORMALIZE RAW RESULT
    # ========================================================

    @staticmethod
    def _unwrap_raw(raw: Any) -> Any:

        if isinstance(raw, dict):

            for key in (
                "candles",
                "data",
                "result",
                "items",
            ):

                if key in raw:

                    return raw[key]

        return raw

    # ========================================================
    # PUBLIC CANDLES
    # ========================================================

    async def candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int | None = None,
    ) -> list[Candle]:

        # ----------------------------------------------------
        # Connection
        # ----------------------------------------------------

        if not self.is_connected:

            await self.connect()

        if self.client is None:
            raise RuntimeError(
                "Pocket Option client отсутствует."
            )

        # ----------------------------------------------------
        # Pair -> Pocket Option symbol
        # ----------------------------------------------------

        symbol = config.otc_symbols.get(
            pair
        )

        if not symbol:

            if pair.endswith(
                "_otc"
            ):
                symbol = pair

            else:
                raise ValueError(
                    f"Неизвестная OTC-пара: {pair}"
                )

        # ----------------------------------------------------
        # Timeframe
        # ----------------------------------------------------

        minutes = max(
            1,
            int(minutes),
        )

        # ----------------------------------------------------
        # Required data
        # ----------------------------------------------------

        if limit is None:

            limit = self.required_1m_candles(
                minutes
            )

        # ----------------------------------------------------
        # Hard safety limit.
        #
        # 20m timeframe:
        # 1380 candles.
        #
        # 1600 хватает с запасом.
        # ----------------------------------------------------

        limit = max(
            240,
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
            "Requested timeframe: %s min",
            minutes,
        )

        logger.info(
            "1m candles required: %s",
            limit,
        )

        logger.info(
            "================================================"
        )

        # ----------------------------------------------------
        # Fetch
        # ----------------------------------------------------

        try:

            raw = await self._get_raw_candles(
                symbol,
                limit,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            self.last_error = str(exc)

            raise RuntimeError(
                f"Ошибка получения рынка {pair}: {exc}"
            ) from exc

        if not raw:

            raise RuntimeError(
                f"Pocket Option не вернул свечи "
                f"для {pair}."
            )

        # ----------------------------------------------------
        # Normalize wrapper
        # ----------------------------------------------------

        raw = self._unwrap_raw(
            raw
        )

        try:

            items = list(raw)

        except TypeError as exc:

            raise RuntimeError(
                f"Неизвестный формат свечей "
                f"для {pair}."
            ) from exc

        # ----------------------------------------------------
        # Parse
        # ----------------------------------------------------

        parsed: list[Candle] = []

        for item in items:

            candle = self._parse_candle(
                item
            )

            if candle is not None:
                parsed.append(
                    candle
                )

        # ----------------------------------------------------
        # DEDUPLICATION
        # ----------------------------------------------------

        unique: dict[
            datetime,
            Candle,
        ] = {}

        for candle in parsed:
            unique[candle.time] = candle

        result = sorted(
            unique.values(),
            key=lambda candle: candle.time,
        )

        # ----------------------------------------------------
        # Keep requested amount
        # ----------------------------------------------------

        result = result[-limit:]

        logger.info(
            "Parsed candles: "
            "pair=%s raw=%s valid=%s",
            pair,
            len(items),
            len(result),
        )

        # ----------------------------------------------------
        # Minimum amount
        # ----------------------------------------------------

        if len(result) < 60:

            raise RuntimeError(
                f"Получено только "
                f"{len(result)} корректных свечей "
                f"для {pair}. "
                f"Нужно минимум 60."
            )

        # ----------------------------------------------------
        # TIME ORDER CHECK
        # ----------------------------------------------------

        for index in range(
            1,
            len(result),
        ):

            if result[index].time <= result[
                index - 1
            ].time:

                raise RuntimeError(
                    f"Некорректный порядок свечей "
                    f"для {pair}."
                )

        # ----------------------------------------------------
        # FRESHNESS
        # ----------------------------------------------------

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

        # Не допускаем старые данные.
        if age > 180:

            raise RuntimeError(
                f"OTC-данные для {pair} устарели. "
                f"Последняя свеча "
                f"{age:.0f} секунд назад."
            )

        # Не допускаем слишком далёкое будущее.
        if age < -30:

            raise RuntimeError(
                f"Время последней свечи {pair} "
                f"находится в будущем."
            )

        # ----------------------------------------------------
        # Success
        # ----------------------------------------------------

        self.last_success = datetime.now(
            timezone.utc
        )

        self.last_error = None

        logger.info(
            "================================================"
        )

        logger.info(
            "OTC DATA OK"
        )

        logger.info(
            "Pair: %s",
            pair,
        )

        logger.info(
            "Candles: %s",
            len(result),
        )

        logger.info(
            "Last close: %s",
            result[-1].close,
        )

        logger.info(
            "Last candle: %s",
            result[-1].time.isoformat(),
        )

        logger.info(
            "================================================"
        )

        return result

    # ========================================================
    # TEST MARKET
    # ========================================================

    async def test_market(
        self,
        pair: str = "EURUSD_otc",
    ) -> list[Candle]:

        """
        Быстрая проверка:

        1. подключение;
        2. получение свечей;
        3. проверка свежести.

        Никаких торговых операций.
        """

        logger.info(
            "Starting Pocket Option market test: %s",
            pair,
        )

        await self.connect()

        candles = await self.candles(
            pair,
            minutes=1,
            limit=240,
        )

        if not candles:
            raise RuntimeError(
                "Рынок вернул пустые данные."
            )

        return candles

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self):

        client = self.client

        self.client = None
        self.ssid = None
        self.connected = False

        if client is None:
            return

        try:

            shutdown = getattr(
                client,
                "shutdown",
                None,
            )

            if shutdown is not None:

                result = shutdown()

                if asyncio.iscoroutine(
                    result
                ):
                    await asyncio.wait_for(
                        result,
                        timeout=10,
                    )

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "Ошибка закрытия Pocket Option."
            )

        finally:

            self.client = None
            self.ssid = None
            self.connected = False


# ============================================================
# GLOBAL MARKET
# ============================================================

market = PocketMarket()
