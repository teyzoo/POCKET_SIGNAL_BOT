from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from BinaryOptionsToolsV2.pocketoption.api import PocketOptionAsync

import config


logger = logging.getLogger("pocket_market")


# ============================================================
# TIMEOUTS
# ============================================================

CONNECT_TIMEOUT = 90
AUTO_LOGIN_TIMEOUT = 150
BALANCE_TIMEOUT = 30
CANDLE_REQUEST_TIMEOUT = 30
CLIENT_CLOSE_TIMEOUT = 10
PLAYWRIGHT_PREPARE_TIMEOUT = 60

RUNTIME_PLAYWRIGHT_PATH = "/tmp/pocket-option-ms-playwright"


# ============================================================
# CANDLE
# ============================================================

@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ============================================================
# HELPERS
# ============================================================

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _find_browser_executable(
    base_paths: list[Path],
) -> Optional[str]:
    """
    Ищет Chromium/Chrome внутри Playwright directories.
    """

    executable_names = (
        "chrome",
        "chromium",
        "chromium-browser",
        "headless_shell",
    )

    for base in base_paths:
        if not base.exists():
            continue

        try:
            for name in executable_names:
                matches = list(
                    base.rglob(name)
                )

                for path in matches:
                    if (
                        path.is_file()
                        and os.access(
                            path,
                            os.X_OK,
                        )
                    ):
                        return str(
                            path.resolve()
                        )

        except Exception:
            logger.exception(
                "[PLAYWRIGHT] Ошибка поиска браузера: %s",
                base,
            )

    return None


def _get_playwright_sources() -> list[Path]:
    """
    Источники Playwright browser cache.

    На Render основной путь:
    /opt/render/project/src/.cache/ms-playwright
    """

    result: list[Path] = []

    custom = os.getenv(
        "POCKET_PLAYWRIGHT_SOURCE_PATH"
    )

    if custom:
        result.append(
            Path(custom)
        )

    env_path = os.getenv(
        "PLAYWRIGHT_BROWSERS_PATH"
    )

    if env_path:
        result.append(
            Path(env_path)
        )

    result.extend(
        [
            Path(
                "/opt/render/project/src/.cache/ms-playwright"
            ),
            Path(
                "/opt/render/.cache/ms-playwright"
            ),
            Path(
                "./.cache/ms-playwright"
            ),
        ]
    )

    unique: list[Path] = []
    seen: set[str] = set()

    for path in result:
        key = str(
            path.expanduser().resolve()
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(
            path
        )

    return unique


# ============================================================
# PLAYWRIGHT PREPARATION
# ============================================================

def prepare_playwright_environment() -> Optional[str]:
    """
    Находит установленный Render Chromium и при необходимости
    копирует его в /tmp.

    Важно:
    /tmp используется только как runtime-копия.
    """

    runtime = Path(
        RUNTIME_PLAYWRIGHT_PATH
    )

    runtime.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "[PLAYWRIGHT] Runtime path: %s",
        runtime,
    )

    # --------------------------------------------------------
    # FIRST: RUNTIME
    # --------------------------------------------------------

    browser = _find_browser_executable(
        [runtime]
    )

    if browser:
        logger.info(
            "[PLAYWRIGHT] Chromium уже есть в runtime: %s",
            browser,
        )

        return browser

    # --------------------------------------------------------
    # SOURCE
    # --------------------------------------------------------

    sources = _get_playwright_sources()

    for source in sources:
        logger.info(
            "[PLAYWRIGHT] Проверяю source: %s",
            source,
        )

        if not source.exists():
            continue

        browser = _find_browser_executable(
            [source]
        )

        if not browser:
            logger.info(
                "[PLAYWRIGHT] Chromium не найден: %s",
                source,
            )
            continue

        logger.info(
            "[PLAYWRIGHT] Найден Chromium в source: %s",
            browser,
        )

        # ----------------------------------------------------
        # COPY SOURCE TO RUNTIME
        # ----------------------------------------------------

        try:
            if runtime.exists():
                for item in runtime.iterdir():
                    try:
                        if item.is_dir():
                            shutil.rmtree(
                                item,
                                ignore_errors=True,
                            )
                        else:
                            item.unlink(
                                missing_ok=True
                            )
                    except Exception:
                        pass

            shutil.copytree(
                source,
                runtime,
                dirs_exist_ok=True,
            )

            runtime_browser = _find_browser_executable(
                [runtime]
            )

            if runtime_browser:
                logger.info(
                    "[PLAYWRIGHT] Runtime Chromium: %s",
                    runtime_browser,
                )

                return runtime_browser

        except Exception:
            logger.exception(
                "[PLAYWRIGHT] Не удалось скопировать browser cache."
            )

            # Если копирование не получилось, всё равно
            # возвращаем исходный executable.
            return browser

    logger.error(
        "[PLAYWRIGHT] Рабочий Chromium не найден."
    )

    return None


# ============================================================
# AUTO LOGIN
# ============================================================

def _pocket_login_sync(
    email: str,
    password: str,
    browser_executable: str,
) -> Optional[str]:
    """
    Синхронный login в отдельном thread.

    multiprocessing здесь намеренно НЕ используется.
    Это сильно уменьшает расход RAM на Render Free.
    """

    logger.info(
        "[AUTO LOGIN WORKER] Browser executable=%s",
        browser_executable,
    )

    try:
        login_module = importlib.import_module(
            "BinaryOptionsToolsV2.pocketoption.tools.login"
        )

    except Exception:
        logger.exception(
            "[AUTO LOGIN WORKER] Не удалось импортировать login module."
        )
        return None

    try:
        original_browser_configs = getattr(
            login_module,
            "_browser_configs",
            None,
        )

        # ----------------------------------------------------
        # PLAYWRIGHT CONFIG
        # ----------------------------------------------------

        def forced_browser_configs(
            pw,
            headless=True,
        ):
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-features=Translate,BackForwardCache",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-renderer-backgrounding",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--no-zygote",
            ]

            yield (
                pw.chromium,
                {
                    "headless": headless,
                    "executable_path": browser_executable,
                    "args": launch_args,
                },
            )

        if original_browser_configs is not None:
            login_module._browser_configs = (
                forced_browser_configs
            )

        logger.info(
            "[AUTO LOGIN WORKER] Запускаю login() с Render Chromium."
        )

        login_function = getattr(
            login_module,
            "login",
            None,
        )

        if not callable(login_function):
            logger.error(
                "[AUTO LOGIN WORKER] login() не найден."
            )
            return None

        # ----------------------------------------------------
        # CALL LIBRARY LOGIN
        # ----------------------------------------------------

        result = login_function(
            email,
            password,
            backend="playwright",
            headless=True,
        )

        # ----------------------------------------------------
        # RESULT NORMALIZATION
        # ----------------------------------------------------

        if isinstance(
            result,
            str,
        ):
            ssid = result.strip()

            if ssid:
                return ssid

        if isinstance(
            result,
            dict,
        ):
            for key in (
                "ssid",
                "session",
                "session_id",
                "po_session",
            ):
                value = result.get(key)

                if value:
                    return str(value).strip()

        # Некоторые версии библиотеки могут вернуть объект
        # с атрибутом ssid.
        for attr in (
            "ssid",
            "session",
            "session_id",
            "po_session",
        ):
            try:
                value = getattr(
                    result,
                    attr,
                    None,
                )

                if value:
                    return str(
                        value
                    ).strip()

            except Exception:
                pass

        logger.error(
            "[AUTO LOGIN WORKER] Login завершился без SSID."
        )

        return None

    except Exception:
        logger.exception(
            "[AUTO LOGIN WORKER] Login exception."
        )

        return None

    finally:
        # ----------------------------------------------------
        # RESTORE MODULE
        # ----------------------------------------------------

        try:
            if (
                original_browser_configs
                is not None
            ):
                login_module._browser_configs = (
                    original_browser_configs
                )
        except Exception:
            pass


# ============================================================
# MARKET
# ============================================================

class PocketMarket:
    def __init__(self):
        self.client: Optional[Any] = None
        self.connected: bool = False
        self.ssid: Optional[str] = None

        logger.info(
            "[MARKET] PocketMarket создан."
        )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> Optional[str]:
        email = getattr(
            config,
            "PO_EMAIL",
            None,
        )

        password = getattr(
            config,
            "PO_PASSWORD",
            None,
        )

        if not email or not password:
            logger.error(
                "[AUTO LOGIN] PO_EMAIL/PO_PASSWORD не заданы."
            )
            return None

        logger.info(
            "[AUTO LOGIN] Подготавливаю Playwright..."
        )

        try:
            browser_executable = await asyncio.wait_for(
                asyncio.to_thread(
                    prepare_playwright_environment
                ),
                timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
            )

        except asyncio.TimeoutError:
            logger.error(
                "[AUTO LOGIN] Таймаут подготовки Playwright."
            )
            return None

        except Exception:
            logger.exception(
                "[AUTO LOGIN] Ошибка подготовки Playwright."
            )
            return None

        if not browser_executable:
            logger.error(
                "[AUTO LOGIN] Рабочий Chromium не найден."
            )
            return None

        logger.info(
            "[AUTO LOGIN] Playwright готов."
        )

        logger.info(
            "[AUTO LOGIN] Browser executable: %s",
            browser_executable,
        )

        logger.info(
            "[AUTO LOGIN] Запускаю login worker."
        )

        try:
            ssid = await asyncio.wait_for(
                asyncio.to_thread(
                    _pocket_login_sync,
                    email,
                    password,
                    browser_executable,
                ),
                timeout=AUTO_LOGIN_TIMEOUT,
            )

        except asyncio.TimeoutError:
            logger.error(
                "[AUTO LOGIN] Login timeout (%s sec).",
                AUTO_LOGIN_TIMEOUT,
            )
            return None

        except Exception:
            logger.exception(
                "[AUTO LOGIN] Ошибка login worker."
            )
            return None

        if not ssid:
            logger.error(
                "[AUTO LOGIN] Login не получил SSID."
            )
            return None

        logger.info(
            "[AUTO LOGIN] SSID успешно получен."
        )

        return ssid

    # ========================================================
    # CREATE CLIENT
    # ========================================================

    async def _create_client(
        self,
        ssid: str,
    ) -> bool:
        try:
            logger.info(
                "[MARKET] STEP 3/5: Создаю PocketOptionAsync client."
            )

            self.client = PocketOptionAsync(
                ssid
            )

            if self.client is None:
                logger.error(
                    "[MARKET] PocketOptionAsync вернул None."
                )
                return False

            # Даём websocket время на инициализацию.
            await asyncio.sleep(3)

            return True

        except Exception:
            logger.exception(
                "[MARKET] Не удалось создать client."
            )

            self.client = None
            return False

    # ========================================================
    # CONNECTION CHECK
    # ========================================================

    async def _check_connection(self) -> bool:
        if self.client is None:
            return False

        try:
            balance_method = getattr(
                self.client,
                "balance",
                None,
            )

            if not callable(balance_method):
                logger.warning(
                    "[MARKET] balance() отсутствует."
                )

                # Если библиотека не предоставляет balance,
                # наличие client считаем достаточным.
                return True

            await asyncio.wait_for(
                balance_method(),
                timeout=BALANCE_TIMEOUT,
            )

            return True

        except asyncio.TimeoutError:
            logger.warning(
                "[MARKET] balance() timeout."
            )
            return False

        except Exception as exc:
            logger.warning(
                "[MARKET] Connection check failed: %s",
                exc,
            )
            return False

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:
        self.connected = False

        # ----------------------------------------------------
        # CLOSE OLD CLIENT
        # ----------------------------------------------------

        if self.client is not None:
            try:
                await self.disconnect()
            except Exception:
                logger.exception(
                    "[MARKET] Ошибка закрытия старого client."
                )

        # ----------------------------------------------------
        # SSID
        # ----------------------------------------------------

        ssid = (
            getattr(
                config,
                "PO_SSID",
                None,
            )
            or os.getenv(
                "PO_SSID"
            )
            or os.getenv(
                "POCKET_OPTION_SSID"
            )
        )

        if ssid:
            ssid = str(
                ssid
            ).strip()

        # ----------------------------------------------------
        # AUTO LOGIN
        # ----------------------------------------------------

        if not ssid:
            logger.info(
                "[MARKET] STEP 1/5: Запускаю автоматический login."
            )

            auto_login_enabled = _env_bool(
                "PO_AUTO_LOGIN",
                bool(
                    getattr(
                        config,
                        "PO_AUTO_LOGIN",
                        True,
                    )
                ),
            )

            if not auto_login_enabled:
                logger.error(
                    "[MARKET] Auto login отключён."
                )
                return False

            ssid = await self.auto_login()

            if not ssid:
                logger.error(
                    "[MARKET] Auto login не получил SSID."
                )
                return False

        else:
            logger.info(
                "[MARKET] STEP 1/5: Использую SSID из конфигурации."
            )

        self.ssid = ssid

        # ----------------------------------------------------
        # CREATE CLIENT
        # ----------------------------------------------------

        logger.info(
            "[MARKET] STEP 2/5: Устанавливаю рыночное соединение."
        )

        created = await asyncio.wait_for(
            self._create_client(ssid),
            timeout=CONNECT_TIMEOUT,
        )

        if not created:
            self.connected = False
            return False

        # ----------------------------------------------------
        # CONNECTION CHECK
        # ----------------------------------------------------

        logger.info(
            "[MARKET] STEP 4/5: Проверяю соединение."
        )

        healthy = await self._check_connection()

        if not healthy:
            logger.error(
                "[MARKET] Проверка соединения не пройдена."
            )

            await self.disconnect()

            return False

        # ----------------------------------------------------
        # READY
        # ----------------------------------------------------

        self.connected = True

        logger.info(
            "[MARKET] STEP 5/5: Pocket Option market READY."
        )

        return True

    # ========================================================
    # IS CONNECTED
    # ========================================================

    def is_connected(self) -> bool:
        return bool(
            self.connected
            and self.client is not None
        )

    # ========================================================
    # CANDLES
    # ========================================================

    async def get_candles(
        self,
        asset: str,
        period: int = 60,
        count: int = 200,
    ) -> list[Candle]:
        """
        Получение свечей.

        period:
            секунды.

        Основной режим:
            get_candles_live()

        Fallback:
            get_candles()
        """

        if not self.is_connected():
            raise RuntimeError(
                "Pocket Option market disconnected"
            )

        if not asset:
            raise ValueError(
                "asset is empty"
            )

        # ----------------------------------------------------
        # LIVE
        # ----------------------------------------------------

        live_method = getattr(
            self.client,
            "get_candles_live",
            None,
        )

        if callable(live_method):
            try:
                raw = await asyncio.wait_for(
                    live_method(
                        asset,
                        period,
                    ),
                    timeout=CANDLE_REQUEST_TIMEOUT,
                )

                candles = self._normalize_candles(
                    raw
                )

                if candles:
                    return candles[-count:]

            except asyncio.TimeoutError:
                logger.warning(
                    "[MARKET] get_candles_live timeout: %s",
                    asset,
                )

            except Exception as exc:
                logger.warning(
                    "[MARKET] get_candles_live failed %s: %s",
                    asset,
                    exc,
                )

        # ----------------------------------------------------
        # FALLBACK
        # ----------------------------------------------------

        get_method = getattr(
            self.client,
            "get_candles",
            None,
        )

        if not callable(get_method):
            self.connected = False

            raise RuntimeError(
                "PocketOption client has no candle method"
            )

        try:
            raw = await asyncio.wait_for(
                get_method(
                    asset,
                    period,
                    count,
                ),
                timeout=CANDLE_REQUEST_TIMEOUT,
            )

            candles = self._normalize_candles(
                raw
            )

            if not candles:
                raise RuntimeError(
                    "Empty candle response"
                )

            return candles[-count:]

        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Candle request timeout: {asset}"
            )

        except Exception:
            if not self._client_looks_alive():
                self.connected = False

            raise

    # ========================================================
    # ALIAS
    # ========================================================

    async def candles(
        self,
        asset: str,
        minutes: int = 1,
        limit: int = 200,
    ) -> list[Candle]:
        """
        Удобный интерфейс для signal engine.

        minutes=1 -> period=60 секунд.
        """

        period = max(
            1,
            int(minutes),
        ) * 60

        return await self.get_candles(
            asset=asset,
            period=period,
            count=limit,
        )

    async def get_candle_data(
        self,
        asset: str,
        period: int = 60,
        count: int = 200,
    ) -> list[Candle]:
        return await self.get_candles(
            asset,
            period,
            count,
        )

    # ========================================================
    # NORMALIZE
    # ========================================================

    def _normalize_candles(
        self,
        raw: Any,
    ) -> list[Candle]:
        if raw is None:
            return []

        # ----------------------------------------------------
        # DICT WRAPPER
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
            ):
                if key in raw:
                    raw = raw[key]
                    break

        # ----------------------------------------------------
        # OBJECT WRAPPER
        # ----------------------------------------------------

        if not isinstance(
            raw,
            (list, tuple),
        ):
            for attr in (
                "data",
                "candles",
                "history",
                "result",
            ):
                try:
                    value = getattr(
                        raw,
                        attr,
                        None,
                    )

                    if value is not None:
                        raw = value
                        break

                except Exception:
                    pass

        if not isinstance(
            raw,
            (list, tuple),
        ):
            return []

        result: list[Candle] = []

        for item in raw:
            try:
                candle = self._normalize_one(
                    item
                )

                if candle is not None:
                    result.append(
                        candle
                    )

            except Exception:
                logger.debug(
                    "[MARKET] Не удалось нормализовать candle: %r",
                    item,
                )

        result.sort(
            key=lambda x: x.timestamp
        )

        return result

    def _normalize_one(
        self,
        item: Any,
    ) -> Optional[Candle]:
        # ----------------------------------------------------
        # DICT
        # ----------------------------------------------------

        if isinstance(
            item,
            dict,
        ):
            timestamp = (
                item.get("timestamp")
                or item.get("time")
                or item.get("at")
                or item.get("from")
            )

            open_price = (
                item.get("open")
                or item.get("o")
            )

            high = (
                item.get("high")
                or item.get("h")
            )

            low = (
                item.get("low")
                or item.get("l")
            )

            close = (
                item.get("close")
                or item.get("c")
            )

            volume = (
                item.get("volume")
                or item.get("v")
                or 0
            )

        # ----------------------------------------------------
        # LIST / TUPLE
        # ----------------------------------------------------

        elif isinstance(
            item,
            (list, tuple),
        ):
            if len(item) < 5:
                return None

            timestamp = item[0]
            open_price = item[1]
            high = item[2]
            low = item[3]
            close = item[4]

            volume = (
                item[5]
                if len(item) > 5
                else 0
            )

        # ----------------------------------------------------
        # OBJECT
        # ----------------------------------------------------

        else:
            timestamp = getattr(
                item,
                "timestamp",
                getattr(
                    item,
                    "time",
                    None,
                ),
            )

            open_price = getattr(
                item,
                "open",
                getattr(
                    item,
                    "o",
                    None,
                ),
            )

            high = getattr(
                item,
                "high",
                getattr(
                    item,
                    "h",
                    None,
                ),
            )

            low = getattr(
                item,
                "low",
                getattr(
                    item,
                    "l",
                    None,
                ),
            )

            close = getattr(
                item,
                "close",
                getattr(
                    item,
                    "c",
                    None,
                ),
            )

            volume = getattr(
                item,
                "volume",
                getattr(
                    item,
                    "v",
                    0,
                ),
            )

        if (
            timestamp is None
            or open_price is None
            or high is None
            or low is None
            or close is None
        ):
            return None

        timestamp = float(
            timestamp
        )

        # Milliseconds -> seconds
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0

        return Candle(
            timestamp=timestamp,
            open=float(
                open_price
            ),
            high=float(
                high
            ),
            low=float(
                low
            ),
            close=float(
                close
            ),
            volume=float(
                volume or 0
            ),
        )

    # ========================================================
    # CLIENT HEALTH
    # ========================================================

    def _client_looks_alive(self) -> bool:
        if self.client is None:
            return False

        # Если client существует, считаем его потенциально
        # живым. Реальное состояние проверяется запросом.
        return True

    # ========================================================
    # FRESHNESS
    # ========================================================

    def candles_are_fresh(
        self,
        candles: list[Candle],
        max_age_seconds: int = 180,
    ) -> bool:
        if not candles:
            return False

        latest = candles[-1]

        now = asyncio.get_running_loop()

        # Здесь не используем loop time для timestamp.
        import time

        age = (
            time.time()
            - float(latest.timestamp)
        )

        return (
            age <= max_age_seconds
        )

    # ========================================================
    # TEST
    # ========================================================

    async def test_market(
        self,
        asset: Optional[str] = None,
    ) -> bool:
        if not self.is_connected():
            return False

        if asset is None:
            try:
                pairs = getattr(
                    config,
                    "pairs",
                    [],
                )

                if pairs:
                    asset = pairs[0][1]

            except Exception:
                asset = None

        if not asset:
            return False

        try:
            candles = await self.candles(
                asset,
                minutes=1,
                limit=10,
            )

            return bool(
                candles
            )

        except Exception:
            logger.exception(
                "[MARKET] Market test failed."
            )
            return False

    # ========================================================
    # DISCONNECT
    # ========================================================

    async def disconnect(self):
        self.connected = False

        client = self.client
        self.client = None

        if client is None:
            return

        close_method = getattr(
            client,
            "close",
            None,
        )

        if not callable(
            close_method
        ):
            return

        try:
            result = close_method()

            if asyncio.iscoroutine(result):
                await asyncio.wait_for(
                    result,
                    timeout=CLIENT_CLOSE_TIMEOUT,
                )

        except Exception:
            logger.exception(
                "[MARKET] Ошибка закрытия client."
            )

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self):
        await self.disconnect()


# ============================================================
# GLOBAL MARKET
# ============================================================

market = PocketMarket()
