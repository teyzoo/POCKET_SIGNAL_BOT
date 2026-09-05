from __future__ import annotations

import asyncio
import logging
import os
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
    # PLAYWRIGHT DIAGNOSTICS
    # ========================================================

    @staticmethod
    def _prepare_playwright_environment() -> None:
        """
        Проверяет Playwright-браузеры.

        ВАЖНО:
        Здесь НЕЛЬЗЯ самостоятельно менять
        PLAYWRIGHT_BROWSERS_PATH на Render cache.

        Render устанавливает браузеры с:

            PLAYWRIGHT_BROWSERS_PATH=0
            python -m playwright install ...

        При значении "0" Playwright использует локальную
        директорию браузеров рядом с Python-пакетом.

        Эта функция только диагностирует окружение.
        """

        configured_path = os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH"
        )

        logger.info(
            "PLAYWRIGHT_BROWSERS_PATH=%s",
            configured_path or "<default>",
        )

        try:
            from playwright.sync_api import sync_playwright

        except Exception as exc:
            raise RuntimeError(
                "Playwright не импортируется: "
                f"{exc}"
            ) from exc

        try:
            with sync_playwright() as pw:

                chromium_path = pw.chromium.executable_path
                firefox_path = pw.firefox.executable_path

                logger.info(
                    "Playwright Chromium executable: %s",
                    chromium_path,
                )

                logger.info(
                    "Playwright Firefox executable: %s",
                    firefox_path,
                )

                chromium_exists = bool(
                    chromium_path
                    and os.path.isfile(chromium_path)
                )

                firefox_exists = bool(
                    firefox_path
                    and os.path.isfile(firefox_path)
                )

                logger.info(
                    "Chromium installed: %s",
                    chromium_exists,
                )

                logger.info(
                    "Firefox installed: %s",
                    firefox_exists,
                )

                if not chromium_exists:
                    raise RuntimeError(
                        "Playwright Chromium не установлен. "
                        f"Ожидаемый путь: {chromium_path}. "
                        "Проверь render.yaml и "
                        "PLAYWRIGHT_BROWSERS_PATH=0."
                    )

                # ------------------------------------------------
                # Реально запускаем Chromium.
                #
                # Это важнее простой проверки файла:
                # файл может существовать, но браузер может
                # не запускаться из-за зависимостей Linux.
                # ------------------------------------------------

                logger.info(
                    "Проверяю фактический запуск Playwright Chromium..."
                )

                browser = None

                try:
                    browser = pw.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                        ],
                    )

                    logger.info(
                        "Playwright Chromium успешно запущен."
                    )

                finally:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            logger.exception(
                                "Ошибка закрытия диагностического Chromium."
                            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Диагностика Playwright завершилась ошибкой."
            )
            raise

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> str:
        """
        Автоматический вход Pocket Option через
        BinaryOptionsToolsV2.

        Используется только если PO_SSID отсутствует.

        Алгоритм:

        1. Проверяем email/password.
        2. Проверяем Playwright Chromium.
        3. Вызываем BinaryOptionsToolsV2 login().
        4. Получаем SSID.
        5. Возвращаем SSID для PocketOptionAsync.

        CAPTCHA здесь не обходится.
        Если Pocket Option действительно требует
        дополнительную проверку, ошибка будет показана
        как отдельная причина.
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
            "Запускаю автоматическую авторизацию "
            "Pocket Option через Playwright..."
        )

        # ----------------------------------------------------
        # Проверяем Playwright в отдельном thread.
        #
        # sync_playwright нельзя выполнять непосредственно
        # внутри async event loop.
        # ----------------------------------------------------

        try:
            await asyncio.to_thread(
                self._prepare_playwright_environment
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            self.last_error = str(exc)

            raise RuntimeError(
                "Playwright не готов для автоматического "
                f"входа Pocket Option: {exc}"
            ) from exc

        # ----------------------------------------------------
        # Import login
        # ----------------------------------------------------

        try:
            from BinaryOptionsToolsV2.pocketoption.tools.login import (
                login,
            )

        except Exception as exc:
            self.last_error = str(exc)

            raise RuntimeError(
                "Не удалось импортировать "
                "BinaryOptionsToolsV2 Pocket Option login: "
                f"{exc}"
            ) from exc

        # ----------------------------------------------------
        # Login
        # ----------------------------------------------------

        try:
            logger.info(
                "Calling BinaryOptionsToolsV2 login backend=playwright..."
            )

            ssid = await asyncio.to_thread(
                login,
                config.po_email,
                config.po_password,
                demo=config.po_demo,
                backend="playwright",
                headless=True,
                timeout=90,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            error_text = str(exc)
            error_lower = error_text.lower()

            self.last_error = error_text

            # ------------------------------------------------
            # ВАЖНО:
            # Не называем ошибку CAPTCHA автоматически.
            #
            # Старая версия делала это неправильно.
            # Ошибка могла быть обычной ошибкой браузера.
            # ------------------------------------------------

            if (
                "captcha" in error_lower
                or "recaptcha" in error_lower
            ):
                logger.error(
                    "Pocket Option сообщил о CAPTCHA/"
                    "дополнительной проверке."
                )

                raise RuntimeError(
                    "Pocket Option действительно потребовал "
                    "CAPTCHA/дополнительную проверку. "
                    "Автоматический вход остановлен. "
                    f"Детали: {error_text}"
                ) from exc

            if (
                "chromium distribution" in error_lower
                or "chrome is not found" in error_lower
                or "browser executable" in error_lower
                or "executable doesn't exist" in error_lower
            ):
                logger.error(
                    "Ошибка браузера Playwright: %s",
                    error_text,
                )

                raise RuntimeError(
                    "Playwright не смог запустить браузер. "
                    "Проверь установку Chromium в Render. "
                    f"Детали: {error_text}"
                ) from exc

            if (
                "firewall" in error_lower
                or "network" in error_lower
                or "connection" in error_lower
                or "timed out" in error_lower
                or "timeout" in error_lower
            ):
                logger.error(
                    "Ошибка сетевого подключения Pocket Option: %s",
                    error_text,
                )

                raise RuntimeError(
                    "Pocket Option недоступен из окружения Render "
                    "или соединение завершилось по timeout. "
                    f"Детали: {error_text}"
                ) from exc

            logger.exception(
                "Pocket Option automatic login failed"
            )

            raise RuntimeError(
                "Автоматическая авторизация Pocket Option "
                f"не удалась: {error_text}"
            ) from exc

        # ----------------------------------------------------
        # Validate SSID
        # ----------------------------------------------------

        if not ssid:
            self.last_error = (
                "Pocket Option login не вернул SSID."
            )

            raise RuntimeError(
                "Pocket Option login не вернул SSID."
            )

        ssid = str(ssid).strip()

        if not ssid:
            self.last_error = (
                "Pocket Option login вернул пустой SSID."
            )

            raise RuntimeError(
                "Pocket Option login вернул пустой SSID."
            )

        logger.info(
            "Pocket Option SSID успешно получен."
        )

        return ssid

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:
        """
        Подключение к Pocket Option.

        Приоритет:

        1. PO_SSID
        2. PO_EMAIL + PO_PASSWORD

        Никаких торговых операций здесь нет.
        """

        async with self.lock:

            # ------------------------------------------------
            # Уже подключены
            # ------------------------------------------------

            if (
                self.client is not None
                and self.connected
            ):
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
                        "PO_SSID не задан, а "
                        "PO_AUTO_LOGIN выключен."
                    )

                logger.warning(
                    "PO_SSID не задан. "
                    "Переходим к автоматическому входу."
                )

                try:
                    ssid = await self.auto_login()

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    self.last_error = str(exc)

                    raise RuntimeError(
                        "Не удалось получить SSID через "
                        f"автоматический вход: {exc}"
                    ) from exc

            if not ssid:

                raise RuntimeError(
                    "Не удалось получить Pocket Option SSID."
                )

            # ------------------------------------------------
            # Import client
            # ------------------------------------------------

            try:

                from BinaryOptionsToolsV2.pocketoption import (
                    PocketOptionAsync,
                )

            except Exception as exc:

                self.last_error = str(exc)

                raise RuntimeError(
                    "BinaryOptionsToolsV2 не импортируется: "
                    f"{exc}"
                ) from exc

            # ------------------------------------------------
            # Create client
            # ------------------------------------------------

            try:

                logger.info(
                    "Создаю PocketOptionAsync клиент..."
                )

                client = PocketOptionAsync(ssid)

                self.client = client
                self.ssid = ssid

                # ------------------------------------------------
                # Даём WebSocket время инициализироваться.
                # ------------------------------------------------

                logger.info(
                    "Ожидание инициализации Pocket Option WebSocket..."
                )

                await asyncio.sleep(5)

                # ------------------------------------------------
                # Health check
                # ------------------------------------------------

                balance_method = getattr(
                    client,
                    "balance",
                    None,
                )

                if balance_method is not None:

                    logger.info(
                        "Выполняю Pocket Option balance health-check..."
                    )

                    try:

                        result = balance_method()

                        if asyncio.iscoroutine(result):

                            await asyncio.wait_for(
                                result,
                                timeout=15,
                            )

                        logger.info(
                            "Pocket Option connection "
                            "health-check OK."
                        )

                    except asyncio.CancelledError:
                        raise

                    except Exception as health_exc:

                        logger.warning(
                            "Pocket Option balance health-check "
                            "не прошёл: %s",
                            health_exc,
                        )

                        # Не считаем это автоматически фатальной
                        # ошибкой: некоторые версии библиотеки
                        # могут подключить WebSocket немного
                        # раньше доступности balance.

                else:

                    logger.warning(
                        "PocketOptionAsync не содержит balance(). "
                        "Продолжаю с WebSocket-подключением."
                    )

                # ------------------------------------------------
                # Connected
                # ------------------------------------------------

                self.connected = True

                self.last_success = datetime.now(
                    timezone.utc
                )

                self.last_error = None

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

        required = (
            timeframe * 60
        )

        return max(
            240,
            required + 180,
        )

    # ========================================================
    # RESAMPLE
    # ========================================================

    @staticmethod
    def _resample_candles(
        candles: list[Candle],
        timeframe: int,
    ) -> list[Candle]:

        timeframe = max(
            1,
            int(timeframe),
        )

        if timeframe == 1:
            return candles

        if not candles:
            return []

        result: list[Candle] = []

        bucket: list[Candle] = []
        bucket_start: datetime | None = None

        bucket_seconds = (
            timeframe * 60
        )

        for candle in candles:

            timestamp = int(
                candle.time.timestamp()
            )

            current_bucket = (
                timestamp // bucket_seconds
            ) * bucket_seconds

            current_start = datetime.fromtimestamp(
                current_bucket,
                tz=timezone.utc,
            )

            if (
                bucket_start is None
                or current_start != bucket_start
            ):

                if bucket:

                    result.append(
                        Candle(
                            time=bucket_start,
                            open=bucket[0].open,
                            high=max(
                                x.high
                                for x in bucket
                            ),
                            low=min(
                                x.low
                                for x in bucket
                            ),
                            close=bucket[-1].close,
                            volume=sum(
                                x.volume
                                for x in bucket
                            ),
                        )
                    )

                bucket = []
                bucket_start = current_start

            bucket.append(candle)

        if (
            bucket
            and bucket_start is not None
        ):

            result.append(
                Candle(
                    time=bucket_start,
                    open=bucket[0].open,
                    high=max(
                        x.high
                        for x in bucket
                    ),
                    low=min(
                        x.low
                        for x in bucket
                    ),
                    close=bucket[-1].close,
                    volume=sum(
                        x.volume
                        for x in bucket
                    ),
                )
            )

        return result

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
        # Hard safety limit
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
        # RESAMPLE
        # ----------------------------------------------------

        if minutes > 1:

            result = self._resample_candles(
                result,
                minutes,
            )

            if len(result) < 60:

                raise RuntimeError(
                    f"После преобразования в "
                    f"{minutes}-минутный timeframe "
                    f"осталось только {len(result)} свечей."
                )

            result = result[
                -max(
                    60,
                    limit // minutes,
                ):
            ]

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
            "Timeframe: %s min",
            minutes,
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
