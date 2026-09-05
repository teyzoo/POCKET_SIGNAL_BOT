from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
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
    # PLAYWRIGHT
    # ========================================================

    @staticmethod
    def _get_playwright_browser_path() -> str:
        """
        Возвращает директорию Playwright browsers.

        Приоритет:
        1. PLAYWRIGHT_BROWSERS_PATH из окружения Render.
        2. /opt/render/project/src/.cache/ms-playwright
        3. .cache/ms-playwright относительно текущего проекта.
        """

        configured_path = os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH"
        )

        if configured_path:
            return os.path.abspath(
                os.path.expanduser(
                    configured_path
                )
            )

        render_path = (
            "/opt/render/project/src/"
            ".cache/ms-playwright"
        )

        if os.path.isdir(
            "/opt/render/project/src"
        ):
            return render_path

        return os.path.abspath(
            os.path.join(
                os.getcwd(),
                ".cache",
                "ms-playwright",
            )
        )

    # --------------------------------------------------------

    @staticmethod
    def _find_browser_executables(
        browser_path: str,
    ) -> list[str]:

        found: list[str] = []

        if not os.path.isdir(browser_path):
            return found

        try:

            for root, dirs, files in os.walk(
                browser_path
            ):

                for filename in files:

                    if filename in (
                        "chrome",
                        "chrome-headless-shell",
                        "chromium",
                        "firefox",
                    ):

                        full_path = os.path.join(
                            root,
                            filename,
                        )

                        if os.path.isfile(
                            full_path
                        ):

                            found.append(
                                full_path
                            )

        except Exception:

            logger.exception(
                "Не удалось просканировать "
                "Playwright browser directory."
            )

        return found

    # --------------------------------------------------------

    @staticmethod
    def _get_chromium_executable() -> str | None:

        try:

            from playwright.sync_api import (
                sync_playwright,
            )

        except Exception as exc:

            raise RuntimeError(
                "Playwright не импортируется: "
                f"{exc}"
            ) from exc

        try:

            with sync_playwright() as pw:

                path = (
                    pw.chromium.executable_path
                )

                if path:
                    return path

        except Exception as exc:

            logger.warning(
                "Не удалось получить Chromium "
                "executable path: %s",
                exc,
            )

        return None

    # --------------------------------------------------------

    @staticmethod
    def _install_chromium(
        browser_path: str,
    ) -> None:
        """
        Устанавливает Chromium через текущую
        установленную версию Playwright.

        Никаких жёстких chromium-XXXX путей.
        """

        logger.warning(
            "Playwright Chromium отсутствует. "
            "Запускаю установку Chromium..."
        )

        env = os.environ.copy()

        env[
            "PLAYWRIGHT_BROWSERS_PATH"
        ] = browser_path

        command = [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
        ]

        logger.info(
            "Playwright install command: %s",
            " ".join(command),
        )

        try:

            process = subprocess.run(
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
                check=False,
            )

        except subprocess.TimeoutExpired as exc:

            raise RuntimeError(
                "Установка Playwright Chromium "
                "превысила timeout 300 секунд."
            ) from exc

        except Exception as exc:

            raise RuntimeError(
                "Не удалось запустить установку "
                "Chromium: "
                f"{exc}"
            ) from exc

        output = (
            process.stdout or ""
        ).strip()

        if output:

            logger.info(
                "Playwright install output:\n%s",
                output[-15000:],
            )

        if process.returncode != 0:

            raise RuntimeError(
                "Playwright Chromium не удалось "
                "установить. "
                f"Код завершения: "
                f"{process.returncode}"
            )

    # --------------------------------------------------------

    @staticmethod
    def _launch_test_browser(
        executable_path: str | None = None,
    ) -> None:
        """
        Фактически запускает Chromium и проверяет,
        что браузер не только установлен,
        но и способен работать в Render.
        """

        from playwright.sync_api import (
            sync_playwright,
        )

        browser = None

        try:

            with sync_playwright() as pw:

                launch_kwargs: dict[str, Any] = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-setuid-sandbox",
                        "--no-zygote",
                    ],
                }

                if executable_path:

                    launch_kwargs[
                        "executable_path"
                    ] = executable_path

                browser = (
                    pw.chromium.launch(
                        **launch_kwargs
                    )
                )

                logger.info(
                    "Playwright Chromium "
                    "успешно запущен."
                )

        except Exception as exc:

            logger.exception(
                "Chromium установлен, "
                "но не смог запуститься."
            )

            raise RuntimeError(
                "Playwright Chromium установлен, "
                "но не запускается: "
                f"{exc}"
            ) from exc

        finally:

            if browser is not None:

                try:
                    browser.close()

                except Exception:

                    logger.exception(
                        "Ошибка закрытия "
                        "диагностического Chromium."
                    )

    # --------------------------------------------------------

    @staticmethod
    def _prepare_playwright_environment() -> None:
        """
        Подготавливает Playwright для Render.

        Главное отличие от старой версии:

        НЕ используется:
            chromium-1234

        Playwright самостоятельно определяет
        установленную версию Chromium.
        """

        browser_path = (
            PocketMarket
            ._get_playwright_browser_path()
        )

        os.environ[
            "PLAYWRIGHT_BROWSERS_PATH"
        ] = browser_path

        logger.info(
            "PLAYWRIGHT_BROWSERS_PATH=%s",
            browser_path,
        )

        os.makedirs(
            browser_path,
            exist_ok=True,
        )

        # ----------------------------------------------------
        # Проверяем Playwright
        # ----------------------------------------------------

        try:

            from playwright.sync_api import (
                sync_playwright,
            )

        except Exception as exc:

            raise RuntimeError(
                "Playwright не импортируется: "
                f"{exc}"
            ) from exc

        # ----------------------------------------------------
        # Получаем пути, которые Playwright считает
        # актуальными.
        # ----------------------------------------------------

        chromium_path: str | None = None
        firefox_path: str | None = None

        try:

            with sync_playwright() as pw:

                chromium_path = (
                    pw.chromium.executable_path
                )

                firefox_path = (
                    pw.firefox.executable_path
                )

        except Exception as exc:

            logger.warning(
                "Не удалось получить browser "
                "paths из Playwright: %s",
                exc,
            )

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
            and os.path.isfile(
                chromium_path
            )
        )

        firefox_exists = bool(
            firefox_path
            and os.path.isfile(
                firefox_path
            )
        )

        logger.info(
            "Chromium installed: %s",
            chromium_exists,
        )

        logger.info(
            "Firefox installed: %s",
            firefox_exists,
        )

        # ----------------------------------------------------
        # Если Chromium отсутствует —
        # пытаемся установить.
        # ----------------------------------------------------

        if not chromium_exists:

            found_browsers = (
                PocketMarket
                ._find_browser_executables(
                    browser_path
                )
            )

            logger.warning(
                "До установки найдены "
                "browser executables: %s",
                found_browsers,
            )

            PocketMarket._install_chromium(
                browser_path
            )

            # ------------------------------------------------
            # После установки заново получаем путь.
            # ------------------------------------------------

            chromium_path = (
                PocketMarket
                ._get_chromium_executable()
            )

            logger.info(
                "Chromium executable after "
                "installation: %s",
                chromium_path,
            )

            chromium_exists = bool(
                chromium_path
                and os.path.isfile(
                    chromium_path
                )
            )

        # ----------------------------------------------------
        # Если Playwright всё ещё не видит Chromium,
        # ищем реальные executable.
        # ----------------------------------------------------

        if not chromium_exists:

            found_browsers = (
                PocketMarket
                ._find_browser_executables(
                    browser_path
                )
            )

            logger.error(
                "Найденные browser executables: %s",
                found_browsers,
            )

            # ------------------------------------------------
            # Иногда Playwright executable_path может
            # отличаться от нашего ожидаемого пути.
            # Если найден chrome — используем его.
            # ------------------------------------------------

            candidate = None

            for path in found_browsers:

                filename = os.path.basename(
                    path
                ).lower()

                if filename in (
                    "chrome",
                    "chrome-headless-shell",
                    "chromium",
                ):

                    candidate = path
                    break

            if candidate:

                logger.info(
                    "Найден Chromium вручную: %s",
                    candidate,
                )

                chromium_path = candidate
                chromium_exists = True

        # ----------------------------------------------------
        # Финальная проверка.
        # ----------------------------------------------------

        if not chromium_exists:

            raise RuntimeError(
                "Playwright Chromium отсутствует "
                "даже после попытки установки. "
                f"Browser directory: "
                f"{browser_path}"
            )

        logger.info(
            "Chromium найден: %s",
            chromium_path,
        )

        # ----------------------------------------------------
        # Реальный запуск.
        # ----------------------------------------------------

        PocketMarket._launch_test_browser(
            executable_path=chromium_path
        )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> str:
        """
        Автоматический вход Pocket Option через
        BinaryOptionsToolsV2.

        Используется, если PO_SSID отсутствует.

        Требуются:

            PO_EMAIL
            PO_PASSWORD

        CAPTCHA не обходится.
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
                "Playwright не готов для "
                "автоматического входа Pocket Option: "
                f"{exc}"
            ) from exc

        # ----------------------------------------------------
        # BinaryOptionsToolsV2 login
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
        # Авторизация
        # ----------------------------------------------------

        try:

            logger.info(
                "Calling BinaryOptionsToolsV2 "
                "login backend=playwright..."
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
            error_lower = (
                error_text.lower()
            )

            self.last_error = error_text

            # ------------------------------------------------
            # CAPTCHA
            # ------------------------------------------------

            if (
                "captcha" in error_lower
                or "recaptcha" in error_lower
            ):

                logger.error(
                    "Pocket Option сообщил о "
                    "CAPTCHA/дополнительной проверке."
                )

                raise RuntimeError(
                    "Pocket Option потребовал "
                    "CAPTCHA/дополнительную проверку. "
                    "Автоматический вход остановлен. "
                    f"Детали: {error_text}"
                ) from exc

            # ------------------------------------------------
            # Browser
            # ------------------------------------------------

            browser_errors = (
                "chromium distribution",
                "chrome is not found",
                "browser executable",
                "executable doesn't exist",
                "executable doesn't exist at",
                "browser_type.launch",
                "failed to launch",
            )

            if any(
                item in error_lower
                for item in browser_errors
            ):

                logger.error(
                    "Ошибка браузера Playwright: %s",
                    error_text,
                )

                raise RuntimeError(
                    "Playwright не смог запустить "
                    "Chromium для Pocket Option. "
                    f"Детали: {error_text}"
                ) from exc

            # ------------------------------------------------
            # Network
            # ------------------------------------------------

            network_errors = (
                "firewall",
                "network",
                "connection",
                "timed out",
                "timeout",
                "net::",
            )

            if any(
                item in error_lower
                for item in network_errors
            ):

                logger.error(
                    "Ошибка сетевого подключения "
                    "Pocket Option: %s",
                    error_text,
                )

                raise RuntimeError(
                    "Pocket Option недоступен из "
                    "окружения Render или соединение "
                    "завершилось по timeout. "
                    f"Детали: {error_text}"
                ) from exc

            # ------------------------------------------------
            # Остальная ошибка
            # ------------------------------------------------

            logger.exception(
                "Pocket Option automatic login failed"
            )

            raise RuntimeError(
                "Автоматическая авторизация "
                "Pocket Option не удалась: "
                f"{error_text}"
            ) from exc

        # ----------------------------------------------------
        # Проверяем SSID
        # ----------------------------------------------------

        if not ssid:

            self.last_error = (
                "Pocket Option login не вернул SSID."
            )

            raise RuntimeError(
                "Pocket Option login не вернул SSID."
            )

        ssid = str(
            ssid
        ).strip()

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
        """

        async with self.lock:

            if (
                self.client is not None
                and self.connected
            ):

                return True

            self.last_error = None

            ssid = ""

            # ------------------------------------------------
            # Используем SSID
            # ------------------------------------------------

            if config.po_ssid:

                ssid = (
                    config.po_ssid.strip()
                )

                logger.info(
                    "Использую PO_SSID из "
                    "Render Environment."
                )

            # ------------------------------------------------
            # Автоматический вход
            # ------------------------------------------------

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
                        "Не удалось получить SSID "
                        "через автоматический вход: "
                        f"{exc}"
                    ) from exc

            # ------------------------------------------------
            # SSID validation
            # ------------------------------------------------

            if not ssid:

                raise RuntimeError(
                    "Не удалось получить "
                    "Pocket Option SSID."
                )

            # ------------------------------------------------
            # Import PocketOptionAsync
            # ------------------------------------------------

            try:

                from BinaryOptionsToolsV2.pocketoption import (
                    PocketOptionAsync,
                )

            except Exception as exc:

                self.last_error = str(exc)

                raise RuntimeError(
                    "BinaryOptionsToolsV2 "
                    "не импортируется: "
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

                # ------------------------------------------------
                # Даём WebSocket время на инициализацию
                # ------------------------------------------------

                logger.info(
                    "Ожидание инициализации "
                    "Pocket Option WebSocket..."
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
                        "Выполняю Pocket Option "
                        "balance health-check..."
                    )

                    try:

                        result = balance_method()

                        if asyncio.iscoroutine(
                            result
                        ):

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
                            "Pocket Option balance "
                            "health-check не прошёл: %s",
                            health_exc,
                        )

                else:

                    logger.warning(
                        "PocketOptionAsync не содержит "
                        "balance(). Продолжаю."
                    )

                # ------------------------------------------------
                # Connected
                # ------------------------------------------------

                self.connected = True

                self.last_success = (
                    datetime.now(
                        timezone.utc
                    )
                )

                self.last_error = None

                logger.info(
                    "=============================================="
                )

                logger.info(
                    "POCKET OPTION CONNECTED"
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
                    "Не удалось подключиться "
                    "к Pocket Option: "
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
    def _timestamp(
        value: Any,
    ) -> datetime:

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

        if value is None:

            return datetime.now(
                timezone.utc
            )

        try:

            numeric = float(
                value
            )

            if numeric > 10_000_000_000:

                numeric /= 1000.0

            return datetime.fromtimestamp(
                numeric,
                tz=timezone.utc,
            )

        except Exception:

            return datetime.now(
                timezone.utc
            )

    # ========================================================
    # VALUE EXTRACTION
    # ========================================================

    @staticmethod
    def _get_value(
        item: Any,
        *names: str,
        default: Any = None,
    ) -> Any:

        if isinstance(
            item,
            dict,
        ):

            for name in names:

                if name in item:

                    return item[name]

        else:

            for name in names:

                try:

                    value = getattr(
                        item,
                        name,
                    )

                except Exception:

                    continue

                if value is not None:

                    return value

        return default

    # ========================================================
    # PARSE CANDLE
    # ========================================================

    @classmethod
    def _parse_candle(
        cls,
        item: Any,
    ) -> Candle | None:

        try:

            timestamp = cls._get_value(
                item,
                "time",
                "timestamp",
                "at",
                "from",
                "from_time",
                "date",
                default=None,
            )

            open_price = cls._get_value(
                item,
                "open",
                "o",
                default=None,
            )

            high_price = cls._get_value(
                item,
                "high",
                "h",
                default=None,
            )

            low_price = cls._get_value(
                item,
                "low",
                "l",
                default=None,
            )

            close_price = cls._get_value(
                item,
                "close",
                "c",
                default=None,
            )

            volume = cls._get_value(
                item,
                "volume",
                "v",
                default=0.0,
            )

            if (
                open_price is None
                or high_price is None
                or low_price is None
                or close_price is None
            ):

                return None

            candle = Candle(
                time=cls._timestamp(
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
                    volume or 0.0
                ),
            )

            if (
                candle.open <= 0
                or candle.high <= 0
                or candle.low <= 0
                or candle.close <= 0
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

            return candle

        except Exception:

            return None

    # ========================================================
    # EXTRACT RAW CANDLES
    # ========================================================

    @classmethod
    def _extract_candles(
        cls,
        raw: Any,
    ) -> list[Candle]:

        if raw is None:

            return []

        # ----------------------------------------------------
        # Dict wrapper
        # ----------------------------------------------------

        if isinstance(
            raw,
            dict,
        ):

            for key in (
                "data",
                "candles",
                "history",
                "result",
                "items",
            ):

                if key in raw:

                    return cls._extract_candles(
                        raw[key]
                    )

            candle = cls._parse_candle(
                raw
            )

            return (
                [candle]
                if candle is not None
                else []
            )

        # ----------------------------------------------------
        # Object wrapper
        # ----------------------------------------------------

        for attr in (
            "data",
            "candles",
            "history",
            "result",
            "items",
        ):

            try:

                value = getattr(
                    raw,
                    attr,
                    None,
                )

            except Exception:

                value = None

            if value is not None:

                return cls._extract_candles(
                    value
                )

        # ----------------------------------------------------
        # List / tuple
        # ----------------------------------------------------

        if isinstance(
            raw,
            (list, tuple),
        ):

            result: list[Candle] = []

            for item in raw:

                candle = cls._parse_candle(
                    item
                )

                if candle is not None:

                    result.append(
                        candle
                    )

            return result

        # ----------------------------------------------------
        # Single candle
        # ----------------------------------------------------

        candle = cls._parse_candle(
            raw
        )

        return (
            [candle]
            if candle is not None
            else []
        )

    # ========================================================
    # NORMALIZE CANDLES
    # ========================================================

    @staticmethod
    def _normalize_candles(
        candles: list[Candle],
    ) -> list[Candle]:

        if not candles:

            return []

        candles = sorted(
            candles,
            key=lambda c: c.time,
        )

        unique: dict[
            datetime,
            Candle,
        ] = {}

        for candle in candles:

            unique[candle.time] = candle

        candles = list(
            unique.values()
        )

        candles.sort(
            key=lambda c: c.time
        )

        return candles

    # ========================================================
    # RAW CANDLE REQUEST
    # ========================================================

    async def _request_raw_candles(
        self,
        asset: str,
        period: int,
        count: int,
    ) -> Any:

        if self.client is None:

            raise RuntimeError(
                "Pocket Option client не создан."
            )

        methods = (
            "get_candles",
            "candles",
            "get_candle",
            "history",
            "get_history",
        )

        for method_name in methods:

            method = getattr(
                self.client,
                method_name,
                None,
            )

            if method is None:

                continue

            attempts = [
                (
                    asset,
                    period,
                    count,
                ),
                (
                    asset,
                    period,
                ),
                (
                    asset,
                    count,
                    period,
                ),
            ]

            for args in attempts:

                try:

                    logger.debug(
                        "Calling market method %s "
                        "args=%s",
                        method_name,
                        args,
                    )

                    result = method(
                        *args
                    )

                    if asyncio.iscoroutine(
                        result
                    ):

                        result = await result

                    if result is not None:

                        return result

                except TypeError:

                    continue

                except Exception as exc:

                    logger.warning(
                        "Market method %s failed: %s",
                        method_name,
                        exc,
                    )

                    continue

        raise RuntimeError(
            "BinaryOptionsToolsV2 не предоставил "
            "поддерживаемый метод получения свечей."
        )

    # ========================================================
    # GET CANDLES
    # ========================================================

    async def get_candles(
        self,
        asset: str,
        period: int = 60,
        count: int = 100,
    ) -> list[Candle]:

        if not self.is_connected:

            await self.connect()

        if not asset:

            raise ValueError(
                "asset не задан."
            )

        period = int(
            period
        )

        count = int(
            count
        )

        if period <= 0:

            raise ValueError(
                "period должен быть > 0."
            )

        if count <= 0:

            raise ValueError(
                "count должен быть > 0."
            )

        try:

            raw = await self._request_raw_candles(
                asset=asset,
                period=period,
                count=count,
            )

            candles = (
                self._extract_candles(
                    raw
                )
            )

            candles = (
                self._normalize_candles(
                    candles
                )
            )

            if len(candles) > count:

                candles = candles[
                    -count:
                ]

            if candles:

                self.last_success = (
                    datetime.now(
                        timezone.utc
                    )
                )

                self.last_error = None

            return candles

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            self.last_error = str(
                exc
            )

            logger.exception(
                "Ошибка получения свечей %s: %s",
                asset,
                exc,
            )

            raise

    # ========================================================
    # GET DATAFRAME-LIKE DICT
    # ========================================================

    async def get_candle_data(
        self,
        asset: str,
        period: int = 60,
        count: int = 100,
    ) -> list[dict[str, Any]]:

        candles = await self.get_candles(
            asset=asset,
            period=period,
            count=count,
        )

        return [
            {
                "datetime": candle.time,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]

    # ========================================================
    # FRESHNESS
    # ========================================================

    @staticmethod
    def validate_freshness(
        candles: list[Candle],
        max_age_seconds: int = 180,
    ) -> bool:

        if not candles:

            return False

        latest = candles[-1].time

        now = datetime.now(
            timezone.utc
        )

        age = (
            now - latest
        ).total_seconds()

        if age < 0:

            return True

        return (
            age <= max_age_seconds
        )

    # ========================================================
    # TEST MARKET
    # ========================================================

    async def test_market(
        self,
        asset: str = "EURUSD",
        period: int = 60,
        count: int = 10,
    ) -> bool:

        try:

            if not self.is_connected:

                await self.connect()

            candles = await self.get_candles(
                asset=asset,
                period=period,
                count=count,
            )

            if not candles:

                logger.warning(
                    "Market test: свечи не получены."
                )

                return False

            valid = all(
                (
                    candle.open > 0
                    and candle.high > 0
                    and candle.low > 0
                    and candle.close > 0
                    and candle.high
                    >= max(
                        candle.open,
                        candle.close,
                    )
                    and candle.low
                    <= min(
                        candle.open,
                        candle.close,
                    )
                )
                for candle in candles
            )

            if not valid:

                logger.warning(
                    "Market test: обнаружены "
                    "некорректные свечи."
                )

                return False

            logger.info(
                "Market test OK: %s candles for %s",
                len(candles),
                asset,
            )

            return True

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            self.last_error = str(
                exc
            )

            logger.exception(
                "Market test failed: %s",
                exc,
            )

            return False

    # ========================================================
    # MARKET STATUS
    # ========================================================

    def status(
        self,
    ) -> dict[str, Any]:

        return {
            "connected": self.connected,
            "has_client": (
                self.client is not None
            ),
            "has_ssid": bool(
                self.ssid
            ),
            "last_error": self.last_error,
            "last_success": (
                self.last_success.isoformat()
                if self.last_success
                else None
            ),
        }

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(
        self,
    ) -> None:

        async with self.lock:

            client = self.client

            self.client = None
            self.ssid = None
            self.connected = False

            if client is None:

                return

            for method_name in (
                "close",
                "disconnect",
                "shutdown",
            ):

                method = getattr(
                    client,
                    method_name,
                    None,
                )

                if method is None:

                    continue

                try:

                    result = method()

                    if asyncio.iscoroutine(
                        result
                    ):

                        await result

                    logger.info(
                        "Pocket Option client "
                        "closed using %s().",
                        method_name,
                    )

                    break

                except Exception:

                    logger.exception(
                        "Ошибка при вызове "
                        "%s() Pocket Option client.",
                        method_name,
                    )

            self.last_error = None


# ============================================================
# GLOBAL MARKET INSTANCE
# ============================================================

market = PocketMarket()
