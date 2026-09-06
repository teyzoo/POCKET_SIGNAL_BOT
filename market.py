from __future__ import annotations

import asyncio
import inspect
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
    # 2. Render cache
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
                "[PLAYWRIGHT] Не удалось скопировать browser: %s",
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
        "[PLAYWRIGHT] Chromium не найден в Render runtime/cache."
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

        import BinaryOptionsToolsV2.pocketoption.tools.login as login_module

        # ====================================================
        # IMPORTANT
        #
        # BinaryOptionsToolsV2 normally tries:
        #
        # Firefox
        # Chromium
        # system Chrome
        #
        # The last backend tries:
        #
        # /opt/google/chrome/chrome
        #
        # Render does not have this Chrome.
        #
        # We therefore replace the browser configuration and
        # explicitly give Playwright the Chromium executable
        # that actually exists in Render.
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

            result_queue.put(
                {
                    "ok": True,
                    "ssid": str(ssid),
                }
            )

        else:

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

                    return str(
                        result["ssid"]
                    )

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

            self.demo = demo

            # ------------------------------------------------
            # SSID argument
            # ------------------------------------------------

            if ssid:

                self.ssid = ssid.strip()

            # ------------------------------------------------
            # SSID ENV
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
                        "[MARKET] Нет PO_SSID "
                        "и нет PO_EMAIL/PO_PASSWORD."
                    )

                    return False

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

                return False

            # ------------------------------------------------
            # BALANCE
            # ------------------------------------------------

            logger.info(
                "[MARKET] STEP 3/5: "
                "Проверяю соединение balance()."
            )

            try:

                balance_method = getattr(
                    self.client,
                    "balance",
                    None,
                )

                if balance_method is not None:

                    result = balance_method()

                    if inspect.isawaitable(
                        result
                    ):

                        result = (
                            await asyncio.wait_for(
                                result,
                                timeout=BALANCE_TIMEOUT,
                            )
                        )

                    logger.info(
                        "[MARKET] "
                        "Balance check OK: %s",
                        result,
                    )

            except asyncio.TimeoutError:

                logger.error(
                    "[MARKET] "
                    "Balance timeout."
                )

                await self.close()

                return False

            except Exception as exc:

                logger.error(
                    "[MARKET] "
                    "Balance check failed: %s",
                    exc,
                )

                await self.close()

                return False

            self.connected = True

            logger.info(
                "[MARKET] STEP 4/5: "
                "Соединение установлено."
            )

            logger.info(
                "[MARKET] STEP 5/5: "
                "Market готов."
            )

            return True

    # ========================================================
    # CANDLES
    # ========================================================

    async def candles(
        self,
        asset: str,
        minutes: int = 1,
        limit: int = 300,
        **kwargs: Any,
    ) -> list[Candle]:

        period = int(
            kwargs.get(
                "period",
                int(minutes) * 60,
            )
        )

        count = int(
            kwargs.get(
                "count",
                limit,
            )
        )

        return await self.get_candle_data(
            asset,
            period=period,
            count=count,
            offset=kwargs.get(
                "offset"
            ),
        )

    async def get_candles(
        self,
        asset: str,
        period: int = 60,
        count: int = 300,
        offset: int | None = None,
    ) -> list[Candle]:

        return await self.get_candle_data(
            asset,
            period=period,
            count=count,
            offset=offset,
        )

    async def get_candle_data(
        self,
        asset: str,
        period: int = 60,
        count: int = 300,
        offset: int | None = None,
    ) -> list[Candle]:

        if (
            self.client is None
            or not self.connected
        ):

            logger.warning(
                "[MARKET] "
                "Candle request while disconnected."
            )

            return []

        if not asset:

            return []

        period = max(
            1,
            int(period),
        )

        count = max(
            1,
            int(count),
        )

        if offset is None:

            offset = period * count

        async with self._candles_lock:

            try:

                method = getattr(
                    self.client,
                    "get_candles",
                    None,
                )

                if method is None:

                    logger.error(
                        "[CANDLES] "
                        "У клиента нет get_candles()."
                    )

                    return []

                try:

                    result = method(
                        asset,
                        period,
                        offset,
                    )

                except TypeError:

                    try:

                        result = method(
                            asset=asset,
                            period=period,
                            offset=offset,
                        )

                    except TypeError:

                        result = method(
                            asset,
                            period,
                        )

                if inspect.isawaitable(
                    result
                ):

                    result = (
                        await asyncio.wait_for(
                            result,
                            timeout=CANDLE_REQUEST_TIMEOUT,
                        )
                    )

                candles = (
                    self._normalize_candles(
                        result
                    )
                )

                candles.sort(
                    key=lambda x: x.timestamp
                )

                unique = {
                    candle.timestamp:
                        candle
                    for candle in candles
                }

                candles = list(
                    unique.values()
                )

                candles.sort(
                    key=lambda x: x.timestamp
                )

                now = int(
                    datetime.now(
                        timezone.utc
                    ).timestamp()
                )

                current_bucket = (
                    now // period
                ) * period

                candles = [
                    candle
                    for candle in candles
                    if candle.timestamp
                    < current_bucket
                ]

                if len(candles) > count:

                    candles = candles[
                        -count:
                    ]

                logger.info(
                    "[CANDLES] %s: "
                    "получено %s свечей.",
                    asset,
                    len(candles),
                )

                return candles

            except asyncio.TimeoutError:

                logger.error(
                    "[CANDLES] Timeout %s.",
                    asset,
                )

                return []

            except Exception as exc:

                logger.exception(
                    "[CANDLES] "
                    "Ошибка получения %s: %s",
                    asset,
                    exc,
                )

                return []

    # ========================================================
    # NORMALIZE
    # ========================================================

    def _normalize_candles(
        self,
        raw: Any,
    ) -> list[Candle]:

        if raw is None:

            return []

        if isinstance(raw, dict):

            for key in (
                "data",
                "candles",
                "result",
                "history",
            ):

                if key in raw:

                    raw = raw[key]

                    break

        if (
            hasattr(raw, "to_dict")
            and hasattr(raw, "columns")
        ):

            try:

                raw = raw.to_dict(
                    orient="records"
                )

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

                if isinstance(
                    item,
                    dict,
                ):

                    timestamp = (
                        item.get("timestamp")
                        or item.get("time")
                        or item.get("from")
                        or item.get("at")
                    )

                    open_price = (
                        item.get("open")
                        if item.get("open")
                        is not None
                        else item.get("o")
                    )

                    high_price = (
                        item.get("high")
                        if item.get("high")
                        is not None
                        else item.get("h")
                    )

                    low_price = (
                        item.get("low")
                        if item.get("low")
                        is not None
                        else item.get("l")
                    )

                    close_price = (
                        item.get("close")
                        if item.get("close")
                        is not None
                        else item.get("c")
                    )

                    volume = (
                        item.get("volume")
                        if item.get("volume")
                        is not None
                        else item.get("v", 0)
                    )

                else:

                    timestamp = (
                        getattr(
                            item,
                            "timestamp",
                            None,
                        )
                        or getattr(
                            item,
                            "time",
                            None,
                        )
                        or getattr(
                            item,
                            "from",
                            None,
                        )
                    )

                    open_price = (
                        getattr(
                            item,
                            "open",
                            None,
                        )
                        or getattr(
                            item,
                            "o",
                            None,
                        )
                    )

                    high_price = (
                        getattr(
                            item,
                            "high",
                            None,
                        )
                        or getattr(
                            item,
                            "h",
                            None,
                        )
                    )

                    low_price = (
                        getattr(
                            item,
                            "low",
                            None,
                        )
                        or getattr(
                            item,
                            "l",
                            None,
                        )
                    )

                    close_price = (
                        getattr(
                            item,
                            "close",
                            None,
                        )
                        or getattr(
                            item,
                            "c",
                            None,
                        )
                    )

                    volume = (
                        getattr(
                            item,
                            "volume",
                            None,
                        )
                        or getattr(
                            item,
                            "v",
                            None,
                        )
                        or 0
                    )

                if timestamp is None:
                    continue

                if open_price is None:
                    continue

                if high_price is None:
                    continue

                if low_price is None:
                    continue

                if close_price is None:
                    continue

                timestamp = float(
                    timestamp
                )

                if timestamp > 10_000_000_000:

                    timestamp /= 1000

                result.append(
                    Candle(
                        timestamp=int(
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
                )

            except Exception:
                continue

        return result

    # ========================================================
    # FRESHNESS
    # ========================================================

    def validate_freshness(
        self,
        candles: list[Candle],
        period: int = 60,
        max_delay: int | None = None,
    ) -> bool:

        if not candles:

            return False

        if max_delay is None:

            max_delay = period * 3

        latest = candles[-1]

        now = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )

        age = (
            now
            - latest.timestamp
        )

        if age < 0:

            return False

        if age > max_delay:

            logger.warning(
                "[CANDLES] "
                "Данные устарели: "
                "age=%ss max=%ss",
                age,
                max_delay,
            )

            return False

        return True

    # ========================================================
    # TEST
    # ========================================================

    async def test_market(
        self,
        asset: str = "EURUSD",
    ) -> bool:

        if not self.connected:

            return False

        candles = await self.candles(
            asset,
            minutes=1,
            limit=20,
        )

        if not candles:

            return False

        return self.validate_freshness(
            candles,
            period=60,
        )

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self) -> None:

        client = self.client

        self.client = None

        self.connected = False

        if client is None:

            return

        try:

            close_method = getattr(
                client,
                "close",
                None,
            )

            if close_method is None:

                close_method = getattr(
                    client,
                    "disconnect",
                    None,
                )

            if close_method is None:

                logger.info(
                    "[MARKET] "
                    "У клиента нет close/disconnect."
                )

                return

            result = close_method()

            if inspect.isawaitable(
                result
            ):

                await asyncio.wait_for(
                    result,
                    timeout=CLIENT_CLOSE_TIMEOUT,
                )

            logger.info(
                "[MARKET] Client закрыт."
            )

        except asyncio.TimeoutError:

            logger.warning(
                "[MARKET] "
                "Client close timeout."
            )

        except Exception as exc:

            logger.warning(
                "[MARKET] "
                "Ошибка закрытия client: %s",
                exc,
            )

    async def disconnect(self) -> None:

        await self.close()


# ============================================================
# GLOBAL INSTANCE
# ============================================================

market = PocketMarket()


__all__ = [
    "Candle",
    "PocketMarket",
    "market",
]
