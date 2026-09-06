from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing as mp
import os
import queue
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("pocket_market")


# ============================================================
# TIMEOUTS
# ============================================================

CONNECT_TIMEOUT = 90
AUTO_LOGIN_TIMEOUT = 180
LOGIN_LIBRARY_TIMEOUT = 150

BALANCE_TIMEOUT = 30
CANDLE_REQUEST_TIMEOUT = 30
CLIENT_CLOSE_TIMEOUT = 10

PLAYWRIGHT_PREPARE_TIMEOUT = 60

WEBSOCKET_INIT_DELAY = 5

RUNTIME_PLAYWRIGHT_PATH = "/tmp/pocket-option-ms-playwright"


# ============================================================
# CANDLE
# ============================================================

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(
            self.timestamp,
            tz=timezone.utc,
        )


# ============================================================
# FILE HELPERS
# ============================================================

def _make_executable(path: str) -> None:
    try:
        mode = os.stat(path).st_mode

        os.chmod(
            path,
            mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH,
        )

    except Exception:
        pass


def _find_browser_executable(
    root: str,
) -> str | None:

    if not root:
        return None

    if not os.path.exists(root):
        return None

    preferred_names = {
        "chrome",
        "chromium",
        "chromium-browser",
        "headless_shell",
    }

    try:

        for current_root, _, files in os.walk(root):

            for filename in files:

                if filename not in preferred_names:
                    continue

                path = os.path.join(
                    current_root,
                    filename,
                )

                if os.path.isfile(path):

                    _make_executable(path)

                    return path

    except Exception as exc:

        logger.warning(
            "[PLAYWRIGHT] Ошибка поиска browser: %s",
            exc,
        )

    return None


def _get_playwright_sources() -> list[str]:

    values = [
        os.getenv(
            "POCKET_PLAYWRIGHT_SOURCE_PATH"
        ),

        os.getenv(
            "PLAYWRIGHT_BROWSERS_PATH"
        ),

        "/opt/render/project/src/.cache/ms-playwright",

        "/opt/render/.cache/ms-playwright",

        "./.cache/ms-playwright",
    ]

    result: list[str] = []

    for value in values:

        if not value:
            continue

        value = os.path.abspath(value)

        if value not in result:
            result.append(value)

    return result


# ============================================================
# PLAYWRIGHT
# ============================================================

async def prepare_playwright_environment() -> tuple[
    bool,
    str | None,
]:

    os.makedirs(
        RUNTIME_PLAYWRIGHT_PATH,
        exist_ok=True,
    )

    logger.info(
        "[PLAYWRIGHT] Runtime path: %s",
        RUNTIME_PLAYWRIGHT_PATH,
    )

    # --------------------------------------------------------
    # 1. Runtime
    # --------------------------------------------------------

    executable = _find_browser_executable(
        RUNTIME_PLAYWRIGHT_PATH
    )

    if executable:

        logger.info(
            "[PLAYWRIGHT] Найден Chromium в runtime: %s",
            executable,
        )

        os.environ[
            "PLAYWRIGHT_BROWSERS_PATH"
        ] = RUNTIME_PLAYWRIGHT_PATH

        return True, executable

    # --------------------------------------------------------
    # 2. Render cache / configured path
    # --------------------------------------------------------

    for source in _get_playwright_sources():

        logger.info(
            "[PLAYWRIGHT] Проверяю source: %s",
            source,
        )

        executable = _find_browser_executable(
            source
        )

        if not executable:
            continue

        logger.info(
            "[PLAYWRIGHT] Найден Chromium в source: %s",
            executable,
        )

        try:

            import shutil

            shutil.copytree(
                source,
                RUNTIME_PLAYWRIGHT_PATH,
                dirs_exist_ok=True,
            )

            executable = _find_browser_executable(
                RUNTIME_PLAYWRIGHT_PATH
            )

        except Exception as exc:

            logger.warning(
                "[PLAYWRIGHT] "
                "Не удалось скопировать browser: %s",
                exc,
            )

            continue

        if executable:

            logger.info(
                "[PLAYWRIGHT] Runtime Chromium: %s",
                executable,
            )

            os.environ[
                "PLAYWRIGHT_BROWSERS_PATH"
            ] = RUNTIME_PLAYWRIGHT_PATH

            return True, executable

    logger.error(
        "[PLAYWRIGHT] "
        "Chromium не найден в Render runtime/cache."
    )

    return False, None


# ============================================================
# LOGIN WORKER
# ============================================================

def _pocket_login_worker(
    result_queue: Any,
    email: str,
    password: str,
    demo: bool,
    headless: bool,
    timeout: int,
    browser_executable: str | None,
) -> None:

    try:

        if browser_executable:

            _make_executable(
                browser_executable
            )

        logger.info(
            "[AUTO LOGIN WORKER] Browser executable=%s",
            browser_executable,
        )

        # ====================================================
        # IMPORTANT FIX
        #
        # Не используем:
        #
        # import ...tools.login as login_module
        #
        # В BinaryOptionsToolsV2 эта конструкция может вернуть
        # функцию login вместо самого модуля.
        #
        # Поэтому используем importlib.import_module().
        # ====================================================

        login_module = importlib.import_module(
            "BinaryOptionsToolsV2.pocketoption.tools.login"
        )

        # ====================================================
        # Render browser configuration
        # ====================================================

        if browser_executable:

            def _render_browser_configs(
                pw: Any,
                browser_headless: bool,
            ):

                common_ctx = {

                    "user_agent":
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/146.0.0.0 "
                        "Safari/537.36",

                    "locale":
                        "en-US",

                    "timezone_id":
                        "America/New_York",

                    "viewport": {
                        "width": 1366,
                        "height": 768,
                    },

                    "extra_http_headers": {
                        "Accept-Language":
                            "en-US,en;q=0.9",
                    },
                }

                yield (
                    pw.chromium,

                    {
                        "headless":
                            browser_headless,

                        "executable_path":
                            browser_executable,

                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-software-rasterizer",
                            "--lang=en-US,en",
                        ],
                    },

                    common_ctx,
                )

            login_module._browser_configs = (
                _render_browser_configs
            )

        login = login_module.login

        logger.info(
            "[AUTO LOGIN WORKER] "
            "Запускаю login() с Render Chromium."
        )

        ssid = login(
            email,
            password,
            demo=demo,
            backend="playwright",
            headless=headless,
            timeout=timeout,
        )

        if ssid:

            logger.info(
                "[AUTO LOGIN WORKER] "
                "Login успешно получил SSID."
            )

            result_queue.put(
                {
                    "ok": True,
                    "ssid": str(ssid),
                }
            )

        else:

            logger.error(
                "[AUTO LOGIN WORKER] "
                "login() вернул пустой SSID."
            )

            result_queue.put(
                {
                    "ok": False,
                    "error":
                        "login() вернул пустой SSID",
                }
            )

    except Exception as exc:

        logger.exception(
            "[AUTO LOGIN WORKER] Login exception."
        )

        try:

            result_queue.put(
                {
                    "ok": False,
                    "error": repr(exc),
                }
            )

        except Exception:
            pass


# ============================================================
# MARKET
# ============================================================

class PocketMarket:

    def __init__(self) -> None:

        self.client: Any | None = None

        self.ssid: str | None = None

        self.connected = False

        self.demo = True

        self.email: str | None = None
        self.password: str | None = None

        self._connect_lock = asyncio.Lock()

        self._candles_lock = asyncio.Lock()

        logger.info(
            "[MARKET] PocketMarket создан."
        )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(
        self,
        email: str,
        password: str,
        demo: bool = True,
        headless: bool = True,
    ) -> str | None:

        logger.info(
            "[AUTO LOGIN] Подготавливаю Playwright..."
        )

        try:

            prepared, browser_executable = (
                await asyncio.wait_for(
                    prepare_playwright_environment(),
                    timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
                )
            )

        except asyncio.TimeoutError:

            logger.error(
                "[AUTO LOGIN] "
                "Playwright preparation timeout."
            )

            return None

        if not prepared or not browser_executable:

            logger.error(
                "[AUTO LOGIN] "
                "Рабочий Chromium не найден."
            )

            return None

        logger.info(
            "[AUTO LOGIN] Playwright готов."
        )

        logger.info(
            "[AUTO LOGIN] Browser executable: %s",
            browser_executable,
        )

        # ----------------------------------------------------
        # Separate process
        # ----------------------------------------------------

        ctx = mp.get_context(
            "spawn"
        )

        result_queue = ctx.Queue()

        process = ctx.Process(
            target=_pocket_login_worker,

            args=(
                result_queue,
                email,
                password,
                demo,
                headless,
                LOGIN_LIBRARY_TIMEOUT,
                browser_executable,
            ),
        )

        started = time.monotonic()

        logger.info(
            "[AUTO LOGIN] Запускаю login worker."
        )

        try:

            process.start()

            while (
                time.monotonic() - started
                < AUTO_LOGIN_TIMEOUT
            ):

                try:

                    result = result_queue.get(
                        timeout=1
                    )

                except queue.Empty:

                    if not process.is_alive():
                        break

                    continue

                if result.get("ok"):

                    logger.info(
                        "[AUTO LOGIN] "
                        "SSID успешно получен."
                    )

                    self.ssid = str(
                        result["ssid"]
                    )

                    return self.ssid

                logger.error(
                    "[AUTO LOGIN] Login error: %s",
                    result.get("error"),
                )

                return None

            logger.error(
                "[AUTO LOGIN] "
                "Login worker timeout."
            )

            return None

        except Exception as exc:

            logger.exception(
                "[AUTO LOGIN] "
                "Ошибка login worker: %s",
                exc,
            )

            return None

        finally:

            if process.is_alive():

                try:
                    process.terminate()
                except Exception:
                    pass

            try:

                process.join(
                    timeout=5
                )

            except Exception:
                pass

            if process.is_alive():

                try:
                    process.kill()
                except Exception:
                    pass

            try:
                result_queue.close()
            except Exception:
                pass

    # ========================================================
    # CREATE CLIENT
    # ========================================================

    async def _create_client(
        self,
        ssid: str,
    ) -> Any | None:

        try:

            from BinaryOptionsToolsV2.pocketoption import (
                PocketOptionAsync,
            )

            logger.info(
                "[MARKET] Создаю PocketOptionAsync..."
            )

            client = PocketOptionAsync(
                ssid
            )

            await asyncio.sleep(
                WEBSOCKET_INIT_DELAY
            )

            logger.info(
                "[MARKET] PocketOptionAsync создан."
            )

            return client

        except Exception as exc:

            logger.exception(
                "[MARKET] "
                "Ошибка создания клиента: %s",
                exc,
            )

            return None

    # ========================================================
    # BALANCE TEST
    # ========================================================

    async def _check_connection(
        self,
        client: Any,
    ) -> bool:

        try:

            logger.info(
                "[MARKET] Проверяю connection через balance()..."
            )

            balance = await asyncio.wait_for(
                client.balance(),
                timeout=BALANCE_TIMEOUT,
            )

            logger.info(
                "[MARKET] Balance check OK: %s",
                balance,
            )

            return True

        except Exception as exc:

            logger.error(
                "[MARKET] Balance check failed: %s",
                exc,
            )

            return False

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(
        self,
        ssid: str | None = None,
        email: str | None = None,
        password: str | None = None,
        demo: bool = True,
        headless: bool = True,
    ) -> bool:

        async with self._connect_lock:

            if (
                self.connected
                and self.client is not None
            ):

                return True

            self.connected = False

            self.demo = demo

            # ------------------------------------------------
            # Explicit SSID
            # ------------------------------------------------

            if ssid:

                self.ssid = ssid.strip()

            # ------------------------------------------------
            # ENV SSID
            # ------------------------------------------------

            if not self.ssid:

                env_ssid = os.getenv(
                    "PO_SSID"
                )

                if env_ssid:

                    self.ssid = (
                        env_ssid.strip()
                    )

                    logger.info(
                        "[MARKET] "
                        "Использую PO_SSID из ENV."
                    )

            # ------------------------------------------------
            # AUTO LOGIN
            # ------------------------------------------------

            if not self.ssid:

                email = (
                    email
                    or os.getenv(
                        "PO_EMAIL"
                    )
                )

                password = (
                    password
                    or os.getenv(
                        "PO_PASSWORD"
                    )
                )

                if not email or not password:

                    logger.error(
                        "[MARKET] "
                        "Нет PO_SSID "
                        "и нет PO_EMAIL/PO_PASSWORD."
                    )

                    return False

                self.email = email
                self.password = password

                logger.info(
                    "[MARKET] STEP 1/5: "
                    "Запускаю автоматический login."
                )

                try:

                    self.ssid = (
                        await asyncio.wait_for(
                            self.auto_login(
                                email=email,
                                password=password,
                                demo=demo,
                                headless=headless,
                            ),
                            timeout=(
                                AUTO_LOGIN_TIMEOUT
                                + PLAYWRIGHT_PREPARE_TIMEOUT
                                + 30
                            ),
                        )
                    )

                except asyncio.TimeoutError:

                    logger.error(
                        "[MARKET] "
                        "Общий timeout авторизации."
                    )

                    self.ssid = None

                    return False

                if not self.ssid:

                    logger.error(
                        "[MARKET] "
                        "Auto login не получил SSID."
                    )

                    return False

            # ------------------------------------------------
            # CLIENT
            # ------------------------------------------------

            logger.info(
                "[MARKET] STEP 2/5: "
                "Создаю Pocket Option client."
            )

            try:

                self.client = (
                    await asyncio.wait_for(
                        self._create_client(
                            self.ssid
                        ),
                        timeout=CONNECT_TIMEOUT,
                    )
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[MARKET] "
                    "Client creation timeout."
                )

                self.client = None

                return False

            if self.client is None:

                logger.error(
                    "[MARKET] "
                    "PocketOptionAsync не создан."
                )

                return False

            # ------------------------------------------------
            # CONNECTION CHECK
            # ------------------------------------------------

            logger.info(
                "[MARKET] STEP 3/5: "
                "Проверяю подключение."
            )

            connection_ok = (
                await self._check_connection(
                    self.client
                )
            )

            if not connection_ok:

                logger.error(
                    "[MARKET] "
                    "Connection check failed."
                )

                await self.disconnect()

                return False

            # ------------------------------------------------
            # CONNECTED
            # ------------------------------------------------

            self.connected = True

            logger.info(
                "[MARKET] STEP 4/5: "
                "Pocket Option connection established."
            )

            logger.info(
                "[MARKET] STEP 5/5: "
                "Market is READY."
            )

            return True

    # ========================================================
    # CONNECTION STATE
    # ========================================================

    def is_connected(self) -> bool:

        return (
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
        count: int = 100,
    ) -> list[Any]:

        if not self.is_connected():

            logger.warning(
                "[MARKET] "
                "Candle request while disconnected."
            )

            return []

        async with self._candles_lock:

            try:

                logger.debug(
                    "[MARKET] "
                    "Request candles: %s "
                    "period=%s count=%s",
                    asset,
                    period,
                    count,
                )

                method = getattr(
                    self.client,
                    "get_candles_live",
                    None,
                )

                if method is not None:

                    result = await asyncio.wait_for(
                        method(
                            asset,
                            period,
                        ),
                        timeout=CANDLE_REQUEST_TIMEOUT,
                    )

                    candles = (
                        self._normalize_candles(
                            result,
                            count,
                        )
                    )

                    if candles:
                        return candles

                # ------------------------------------------------
                # Fallback
                # ------------------------------------------------

                method = getattr(
                    self.client,
                    "get_candles",
                    None,
                )

                if method is None:

                    logger.error(
                        "[MARKET] "
                        "Client has no candle method."
                    )

                    return []

                result = await asyncio.wait_for(
                    method(
                        asset,
                        period,
                        count,
                    ),
                    timeout=CANDLE_REQUEST_TIMEOUT,
                )

                return self._normalize_candles(
                    result,
                    count,
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[MARKET] "
                    "Candle request timeout: %s",
                    asset,
                )

                return []

            except Exception as exc:

                logger.exception(
                    "[MARKET] "
                    "Candle request failed for %s: %s",
                    asset,
                    exc,
                )

                # Очень важно:
                # не считаем обычную ошибку свечей
                # полноценным подключением.

                if self.client is None:
                    self.connected = False

                return []

    # ========================================================
    # CANDLE DATA ALIAS
    # ========================================================

    async def get_candle_data(
        self,
        asset: str,
        period: int = 60,
        count: int = 100,
    ) -> list[Any]:

        return await self.get_candles(
            asset=asset,
            period=period,
            count=count,
        )

    # ========================================================
    # CANDLE NORMALIZATION
    # ========================================================

    def _normalize_candles(
        self,
        data: Any,
        count: int,
    ) -> list[Candle]:

        if data is None:
            return []

        if isinstance(data, dict):

            for key in (
                "data",
                "candles",
                "result",
                "items",
            ):

                if key in data:

                    data = data[key]

                    break

        if not isinstance(
            data,
            (list, tuple),
        ):

            return []

        normalized: list[Candle] = []

        for item in data:

            try:

                if isinstance(item, dict):

                    timestamp = item.get(
                        "timestamp",
                        item.get(
                            "time",
                            item.get(
                                "from",
                                item.get(
                                    "at",
                                    0,
                                ),
                            ),
                        ),
                    )

                    open_price = item.get(
                        "open",
                        item.get("o"),
                    )

                    high_price = item.get(
                        "high",
                        item.get("h"),
                    )

                    low_price = item.get(
                        "low",
                        item.get("l"),
                    )

                    close_price = item.get(
                        "close",
                        item.get("c"),
                    )

                    volume = item.get(
                        "volume",
                        item.get(
                            "v",
                            0,
                        ),
                    )

                elif isinstance(
                    item,
                    (list, tuple),
                ):

                    if len(item) < 5:
                        continue

                    timestamp = item[0]
                    open_price = item[1]
                    high_price = item[2]
                    low_price = item[3]
                    close_price = item[4]

                    volume = (
                        item[5]
                        if len(item) > 5
                        else 0
                    )

                else:

                    timestamp = getattr(
                        item,
                        "timestamp",
                        getattr(
                            item,
                            "time",
                            0,
                        ),
                    )

                    open_price = getattr(
                        item,
                        "open",
                        None,
                    )

                    high_price = getattr(
                        item,
                        "high",
                        None,
                    )

                    low_price = getattr(
                        item,
                        "low",
                        None,
                    )

                    close_price = getattr(
                        item,
                        "close",
                        None,
                    )

                    volume = getattr(
                        item,
                        "volume",
                        0,
                    )

                if timestamp is None:
                    continue

                timestamp = int(
                    float(timestamp)
                )

                # milliseconds → seconds
                if timestamp > 10_000_000_000:
                    timestamp //= 1000

                candle = Candle(
                    timestamp=timestamp,
                    open=float(open_price),
                    high=float(high_price),
                    low=float(low_price),
                    close=float(close_price),
                    volume=float(volume or 0),
                )

                normalized.append(
                    candle
                )

            except Exception:

                continue

        normalized.sort(
            key=lambda x: x.timestamp
        )

        if count > 0:

            normalized = normalized[-count:]

        return normalized

    # ========================================================
    # FRESHNESS
    # ========================================================

    def candles_are_fresh(
        self,
        candles: list[Any],
        max_age_seconds: int,
    ) -> bool:

        if not candles:
            return False

        try:

            last = candles[-1]

            timestamp = getattr(
                last,
                "timestamp",
                0,
            )

            if not timestamp:
                return False

            now = int(
                time.time()
            )

            age = now - int(
                timestamp
            )

            return (
                age >= -10
                and age <= max_age_seconds
            )

        except Exception:

            return False

    # ========================================================
    # MARKET TEST
    # ========================================================

    async def test_market(
        self,
        asset: str = "EURUSD_otc",
        period: int = 60,
        count: int = 10,
    ) -> bool:

        if not self.is_connected():

            return False

        candles = await self.get_candles(
            asset=asset,
            period=period,
            count=count,
        )

        if not candles:

            logger.error(
                "[MARKET] "
                "Market test failed: empty candles."
            )

            return False

        logger.info(
            "[MARKET] "
            "Market test OK: %s candles for %s",
            len(candles),
            asset,
        )

        return True

    # ========================================================
    # DISCONNECT
    # ========================================================

    async def disconnect(self) -> None:

        self.connected = False

        client = self.client

        self.client = None

        if client is None:
            return

        try:

            close_method = getattr(
                client,
                "close",
                None,
            )

            if close_method is not None:

                result = close_method()

                if inspect.isawaitable(
                    result
                ):

                    await asyncio.wait_for(
                        result,
                        timeout=CLIENT_CLOSE_TIMEOUT,
                    )

        except Exception as exc:

            logger.warning(
                "[MARKET] "
                "Client close error: %s",
                exc,
            )

        logger.info(
            "[MARKET] "
            "Pocket Option disconnected."
        )

    # ========================================================
    # CLOSE ALIAS
    # ========================================================

    async def close(self) -> None:

        await self.disconnect()


# ============================================================
# GLOBAL MARKET INSTANCE
# ============================================================

market = PocketMarket()
