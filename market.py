from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

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

WEBSOCKET_INIT_DELAY = 5

RUNTIME_PLAYWRIGHT_PATH = "/tmp/pocket-option-ms-playwright"


# ============================================================
# CANDLE MODEL
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
# ENV HELPERS
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


# ============================================================
# PLAYWRIGHT
# ============================================================

def _find_browser_executable(
    base_paths: list[Path],
) -> Optional[str]:
    """
    Ищет Chromium/Chrome в указанных директориях.
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
                for path in base.rglob(name):
                    try:
                        if path.is_file() and os.access(
                            path,
                            os.X_OK,
                        ):
                            return str(path.resolve())
                    except Exception:
                        continue

        except Exception:
            logger.exception(
                "[PLAYWRIGHT] Ошибка поиска браузера: %s",
                base,
            )

    return None


def _get_playwright_sources() -> list[Path]:
    """
    Все возможные места, где Render/Playwright
    может хранить браузер.
    """

    result: list[Path] = []

    custom = os.getenv(
        "POCKET_PLAYWRIGHT_SOURCE_PATH"
    )

    if custom:
        result.append(Path(custom))

    env_path = os.getenv(
        "PLAYWRIGHT_BROWSERS_PATH"
    )

    if env_path:
        result.append(Path(env_path))

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
        try:
            key = str(
                path.expanduser().resolve()
            )
        except Exception:
            key = str(path)

        if key in seen:
            continue

        seen.add(key)
        unique.append(path)

    return unique


def prepare_playwright_environment() -> Optional[str]:
    """
    Находит установленный Chromium и при необходимости
    копирует его в /tmp.
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
    # Уже есть браузер в runtime
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
    # Ищем browser source
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

        try:
            # ------------------------------------------------
            # Очищаем runtime
            # ------------------------------------------------

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

            # ------------------------------------------------
            # Копируем browser
            # ------------------------------------------------

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
                "[PLAYWRIGHT] Ошибка копирования Chromium."
            )

            # Если копирование не удалось,
            # возвращаем исходный путь.
            return browser

    logger.error(
        "[PLAYWRIGHT] Рабочий Chromium не найден."
    )

    return None


# ============================================================
# POCKET OPTION LOGIN
# ============================================================

def _pocket_login_sync(
    email: str,
    password: str,
    browser_executable: str,
) -> Optional[str]:
    """
    Синхронная функция авторизации Pocket Option.

    Запускается через asyncio.to_thread(),
    поэтому не создаёт дополнительный Python process
    и не удваивает расход RAM на Render Free.
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
            "[AUTO LOGIN WORKER] "
            "Не удалось импортировать login module."
        )

        return None

    original_browser_configs = getattr(
        login_module,
        "_browser_configs",
        None,
    )

    try:

        # ====================================================
        # FORCED BROWSER CONFIG
        # ====================================================

        def forced_browser_configs(
            pw,
            headless=True,
        ):
            """
            ВАЖНО:

            Текущая версия BinaryOptionsToolsV2
            ожидает ТРИ значения:

                browser_type
                launch_kwargs
                ctx_kwargs

            Старый вариант возвращал только 2,
            из-за чего возникало:

                ValueError:
                not enough values to unpack
                (expected 3, got 2)
            """

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
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-renderer-backgrounding",
                "--disable-sync",

                "--metrics-recording-only",
                "--no-first-run",
                "--no-zygote",

                # Ограничиваем renderer
                # для Render Free RAM.
                "--renderer-process-limit=1",

                "--js-flags=--max-old-space-size=128",
            ]

            # =================================================
            # ИМЕННО ТРИ ЗНАЧЕНИЯ
            # =================================================

            yield (
                pw.chromium,

                {
                    "headless": headless,
                    "executable_path": browser_executable,
                    "args": launch_args,
                },

                {
                    # Browser context options.
                    # Оставляем пустым,
                    # чтобы библиотека сама использовала
                    # свои настройки.
                },
            )

        # ====================================================
        # Подменяем browser configs библиотеки
        # ====================================================

        if original_browser_configs is not None:
            login_module._browser_configs = (
                forced_browser_configs
            )

        # ====================================================
        # Получаем login()
        # ====================================================

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

        logger.info(
            "[AUTO LOGIN WORKER] Запускаю login()..."
        )

        # ====================================================
        # LOGIN
        # ====================================================

        result = login_function(
            email,
            password,
            backend="playwright",
            headless=True,
        )

        # ====================================================
        # Результат может быть str
        # ====================================================

        if isinstance(result, str):
            ssid = result.strip()

            if ssid:
                logger.info(
                    "[AUTO LOGIN WORKER] Получен SSID."
                )

                return ssid

        # ====================================================
        # Результат может быть dict
        # ====================================================

        if isinstance(result, dict):

            for key in (
                "ssid",
                "session",
                "session_id",
                "po_session",
            ):

                value = result.get(key)

                if value:
                    ssid = str(value).strip()

                    if ssid:
                        logger.info(
                            "[AUTO LOGIN WORKER] "
                            "Получен SSID из dict."
                        )

                        return ssid

        # ====================================================
        # Результат может быть объектом
        # ====================================================

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
                    ssid = str(value).strip()

                    if ssid:
                        logger.info(
                            "[AUTO LOGIN WORKER] "
                            "Получен SSID из объекта."
                        )

                        return ssid

            except Exception:
                pass

        logger.error(
            "[AUTO LOGIN WORKER] "
            "Login завершился без SSID."
        )

        return None

    except Exception:
        logger.exception(
            "[AUTO LOGIN WORKER] Login exception."
        )

        return None

    finally:

        # ====================================================
        # Возвращаем оригинальную функцию
        # ====================================================

        try:

            if original_browser_configs is not None:
                login_module._browser_configs = (
                    original_browser_configs
                )

        except Exception:
            pass


# ============================================================
# POCKET MARKET
# ============================================================

class PocketMarket:

    def __init__(self):

        self.client: Optional[Any] = None

        self.connected: bool = False

        self.ssid: Optional[str] = None

        self._login_lock = asyncio.Lock()

        self._connect_lock = asyncio.Lock()

        logger.info(
            "[MARKET] PocketMarket создан."
        )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> Optional[str]:

        async with self._login_lock:

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
                    "[AUTO LOGIN] "
                    "PO_EMAIL/PO_PASSWORD не заданы."
                )

                return None

            logger.info(
                "[AUTO LOGIN] Подготавливаю Playwright..."
            )

            # ------------------------------------------------
            # Prepare browser
            # ------------------------------------------------

            try:

                browser_executable = (
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            prepare_playwright_environment
                        ),
                        timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
                    )
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[AUTO LOGIN] "
                    "Таймаут подготовки Playwright."
                )

                return None

            except Exception:

                logger.exception(
                    "[AUTO LOGIN] "
                    "Ошибка подготовки Playwright."
                )

                return None

            if not browser_executable:

                logger.error(
                    "[AUTO LOGIN] Chromium не найден."
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

            # ------------------------------------------------
            # Login without multiprocessing
            # ------------------------------------------------

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
                    "[AUTO LOGIN] "
                    "Login timeout: %s sec",
                    AUTO_LOGIN_TIMEOUT,
                )

                return None

            except Exception:

                logger.exception(
                    "[AUTO LOGIN] "
                    "Ошибка login worker."
                )

                return None

            if not ssid:

                logger.error(
                    "[AUTO LOGIN] "
                    "Login не получил SSID."
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
                "[MARKET] "
                "STEP 3/5: Создаю PocketOptionAsync client."
            )

            self.client = PocketOptionAsync(
                ssid
            )

            if self.client is None:

                logger.error(
                    "[MARKET] "
                    "PocketOptionAsync вернул None."
                )

                return False

            logger.info(
                "[MARKET] "
                "Жду инициализацию WebSocket..."
            )

            await asyncio.sleep(
                WEBSOCKET_INIT_DELAY
            )

            return True

        except Exception:

            logger.exception(
                "[MARKET] "
                "Не удалось создать client."
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

                # Если balance API нет,
                # не считаем это автоматически
                # разрывом соединения.
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
                "[MARKET] "
                "Connection check failed: %s",
                exc,
            )

            return False

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:

        async with self._connect_lock:

            self.connected = False

            logger.info(
                "[MARKET] "
                "Подключение к Pocket Option..."
            )

            # ------------------------------------------------
            # Close old client
            # ------------------------------------------------

            if self.client is not None:

                try:
                    await self.disconnect()

                except Exception:

                    logger.exception(
                        "[MARKET] "
                        "Ошибка закрытия старого client."
                    )

            # ------------------------------------------------
            # SSID
            # ------------------------------------------------

            ssid = getattr(
                config,
                "PO_SSID",
                None,
            )

            if ssid:

                ssid = str(ssid).strip()

                logger.info(
                    "[MARKET] "
                    "STEP 1/5: Использую PO_SSID."
                )

            else:

                logger.info(
                    "[MARKET] "
                    "STEP 1/5: "
                    "Запускаю автоматический login."
                )

                ssid = await self.auto_login()

                if not ssid:

                    logger.error(
                        "[MARKET] "
                        "Не удалось получить SSID."
                    )

                    return False

            self.ssid = ssid

            logger.info(
                "[MARKET] "
                "STEP 2/5: SSID получен."
            )

            # ------------------------------------------------
            # Create client
            # ------------------------------------------------

            try:

                client_created = await asyncio.wait_for(
                    self._create_client(
                        ssid
                    ),
                    timeout=CONNECT_TIMEOUT,
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[MARKET] "
                    "STEP 3/5 TIMEOUT: client."
                )

                return False

            if not client_created:

                logger.error(
                    "[MARKET] "
                    "STEP 3/5 FAILED: client."
                )

                return False

            # ------------------------------------------------
            # Connection check
            # ------------------------------------------------

            logger.info(
                "[MARKET] "
                "STEP 4/5: Проверяю соединение."
            )

            connected = await self._check_connection()

            if not connected:

                logger.warning(
                    "[MARKET] "
                    "Первый connection check failed."
                )

                await asyncio.sleep(3)

                connected = await self._check_connection()

            if not connected:

                logger.error(
                    "[MARKET] "
                    "STEP 4/5 FAILED: connection."
                )

                await self.disconnect()

                return False

            # ------------------------------------------------
            # Ready
            # ------------------------------------------------

            self.connected = True

            logger.info(
                "[MARKET] "
                "STEP 5/5: Pocket Option connected."
            )

            logger.info(
                "[MARKET] MARKET READY."
            )

            return True

    # ========================================================
    # CANDLES
    # ========================================================

    async def candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int = 200,
    ) -> list[dict[str, Any]]:

        if not self.connected or self.client is None:

            raise RuntimeError(
                "Market is not connected"
            )

        pair = str(pair).strip()

        if not pair:

            raise ValueError(
                "Pair is empty"
            )

        minutes = max(
            1,
            int(minutes),
        )

        limit = max(
            1,
            int(limit),
        )

        get_candles = getattr(
            self.client,
            "get_candles",
            None,
        )

        candles_method = getattr(
            self.client,
            "candles",
            None,
        )

        try:

            # ------------------------------------------------
            # Preferred API
            # ------------------------------------------------

            if callable(get_candles):

                logger.debug(
                    "[MARKET] "
                    "get_candles(%s, %s, %s)",
                    pair,
                    minutes,
                    limit,
                )

                result = await asyncio.wait_for(
                    get_candles(
                        pair,
                        minutes * 60,
                        limit,
                    ),
                    timeout=CANDLE_REQUEST_TIMEOUT,
                )

            # ------------------------------------------------
            # Fallback API
            # ------------------------------------------------

            elif callable(candles_method):

                logger.debug(
                    "[MARKET] "
                    "candles(%s, %s, %s)",
                    pair,
                    minutes,
                    limit,
                )

                result = await asyncio.wait_for(
                    candles_method(
                        pair,
                        minutes * 60,
                        limit,
                    ),
                    timeout=CANDLE_REQUEST_TIMEOUT,
                )

            else:

                raise RuntimeError(
                    "PocketOption client "
                    "has no candle method"
                )

        except asyncio.TimeoutError:

            logger.error(
                "[MARKET] "
                "Candle request timeout: %s",
                pair,
            )

            raise

        except Exception:

            logger.exception(
                "[MARKET] "
                "Candle request failed: %s",
                pair,
            )

            self.connected = False

            raise

        # ====================================================
        # EMPTY
        # ====================================================

        if result is None:
            return []

        # ====================================================
        # Tuple response
        # ====================================================

        if isinstance(result, tuple):

            result = result[0]

        # ====================================================
        # Convert iterable to list
        # ====================================================

        if not isinstance(result, list):

            try:
                result = list(result)

            except Exception:

                return []

        # ====================================================
        # NORMALIZE
        # ====================================================

        normalized: list[
            dict[str, Any]
        ] = []

        for candle in result:

            # ------------------------------------------------
            # Dict candle
            # ------------------------------------------------

            if isinstance(
                candle,
                dict,
            ):

                item = dict(candle)

            # ------------------------------------------------
            # Object candle
            # ------------------------------------------------

            else:

                item = {}

                for name in (
                    "timestamp",
                    "time",
                    "timestamp_ms",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ):

                    try:

                        value = getattr(
                            candle,
                            name,
                            None,
                        )

                        if value is not None:
                            item[name] = value

                    except Exception:
                        pass

            if not item:
                continue

            # ------------------------------------------------
            # Timestamp
            # ------------------------------------------------

            timestamp = (
                item.get("timestamp")
                or item.get("time")
                or item.get("timestamp_ms")
            )

            if timestamp is None:
                continue

            try:

                timestamp = float(
                    timestamp
                )

                # milliseconds -> seconds
                if timestamp > 10_000_000_000:
                    timestamp /= 1000.0

            except Exception:

                continue

            # ------------------------------------------------
            # OHLC
            # ------------------------------------------------

            try:

                open_price = float(
                    item.get("open")
                )

                high_price = float(
                    item.get("high")
                )

                low_price = float(
                    item.get("low")
                )

                close_price = float(
                    item.get("close")
                )

            except Exception:

                continue

            # ------------------------------------------------
            # Volume
            # ------------------------------------------------

            try:

                volume = float(
                    item.get(
                        "volume",
                        0,
                    )
                    or 0
                )

            except Exception:

                volume = 0.0

            # ------------------------------------------------
            # Final normalized candle
            # ------------------------------------------------

            normalized.append(
                {
                    "timestamp": timestamp,
                    "datetime": timestamp,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                }
            )

        # ====================================================
        # SORT
        # ====================================================

        normalized.sort(
            key=lambda x: x["timestamp"]
        )

        # ====================================================
        # LIMIT
        # ====================================================

        if limit > 0:

            normalized = normalized[
                -limit:
            ]

        logger.debug(
            "[MARKET] "
            "Получено свечей %s: %s",
            pair,
            len(normalized),
        )

        return normalized

    # ========================================================
    # GET CANDLES ALIAS
    # ========================================================

    async def get_candles(
        self,
        pair: str,
        minutes: int = 1,
        limit: int = 200,
    ) -> list[dict[str, Any]]:

        return await self.candles(
            pair=pair,
            minutes=minutes,
            limit=limit,
        )

    # ========================================================
    # SERVER TIME
    # ========================================================

    async def server_time(
        self,
    ) -> Optional[int]:

        if self.client is None:
            return None

        method = getattr(
            self.client,
            "server_time",
            None,
        )

        if not callable(method):
            return None

        try:

            result = await asyncio.wait_for(
                method(),
                timeout=10,
            )

            if result is None:
                return None

            return int(result)

        except Exception:

            logger.exception(
                "[MARKET] "
                "server_time failed."
            )

            return None

    # ========================================================
    # RECONNECT
    # ========================================================

    async def reconnect(self) -> bool:

        if not self.ssid:

            return await self.connect()

        try:

            if self.client is None:

                return await self.connect()

            reconnect_method = getattr(
                self.client,
                "reconnect",
                None,
            )

            if callable(reconnect_method):

                logger.info(
                    "[MARKET] "
                    "Использую reconnect() библиотеки."
                )

                await asyncio.wait_for(
                    reconnect_method(),
                    timeout=CONNECT_TIMEOUT,
                )

                await asyncio.sleep(3)

                if await self._check_connection():

                    self.connected = True

                    logger.info(
                        "[MARKET] "
                        "Reconnect успешен."
                    )

                    return True

        except Exception:

            logger.exception(
                "[MARKET] "
                "reconnect() failed."
            )

        self.connected = False

        return await self.connect()

    # ========================================================
    # DISCONNECT
    # ========================================================

    async def disconnect(self) -> None:

        self.connected = False

        client = self.client

        if client is None:
            return

        self.client = None

        # ----------------------------------------------------
        # shutdown()
        # ----------------------------------------------------

        shutdown = getattr(
            client,
            "shutdown",
            None,
        )

        if callable(shutdown):

            try:

                logger.info(
                    "[MARKET] "
                    "Закрываю PocketOption client..."
                )

                await asyncio.wait_for(
                    shutdown(),
                    timeout=CLIENT_CLOSE_TIMEOUT,
                )

            except asyncio.TimeoutError:

                logger.warning(
                    "[MARKET] "
                    "Client shutdown timeout."
                )

            except Exception:

                logger.exception(
                    "[MARKET] "
                    "Client shutdown error."
                )

            return

        # ----------------------------------------------------
        # close()
        # ----------------------------------------------------

        close = getattr(
            client,
            "close",
            None,
        )

        if callable(close):

            try:

                result = close()

                if asyncio.iscoroutine(
                    result
                ):

                    await asyncio.wait_for(
                        result,
                        timeout=CLIENT_CLOSE_TIMEOUT,
                    )

            except Exception:

                logger.exception(
                    "[MARKET] "
                    "Client close error."
                )

    # ========================================================
    # CLOSE ALIAS
    # ========================================================

    async def close(self) -> None:

        await self.disconnect()

    # ========================================================
    # CONNECTION STATE
    # ========================================================

    def is_connected(self) -> bool:

        return bool(
            self.connected
            and self.client is not None
        )

    # ========================================================
    # BALANCE
    # ========================================================

    async def balance(
        self,
    ) -> Optional[float]:

        if self.client is None:
            return None

        method = getattr(
            self.client,
            "balance",
            None,
        )

        if not callable(method):
            return None

        try:

            result = await asyncio.wait_for(
                method(),
                timeout=BALANCE_TIMEOUT,
            )

            if result is None:
                return None

            return float(result)

        except Exception:

            logger.exception(
                "[MARKET] "
                "Balance request failed."
            )

            return None


# ============================================================
# LOCAL TEST
# ============================================================

if __name__ == "__main__":

    async def _test():

        logging.basicConfig(
            level=logging.INFO,
            format=(
                "%(asctime)s | "
                "%(levelname)s | "
                "%(name)s | "
                "%(message)s"
            ),
        )

        market = PocketMarket()

        try:

            ok = await market.connect()

            print(
                "CONNECTED:",
                ok,
            )

            if ok:

                balance = await market.balance()

                print(
                    "BALANCE:",
                    balance,
                )

                try:

                    candles = await market.candles(
                        "EURUSD_otc",
                        minutes=1,
                        limit=10,
                    )

                    print(
                        "CANDLES:",
                        len(candles),
                    )

                    if candles:

                        print(
                            candles[-1]
                        )

                except Exception as exc:

                    print(
                        "CANDLE ERROR:",
                        exc,
                    )

        finally:

            await market.disconnect()

    asyncio.run(
        _test()
    )
