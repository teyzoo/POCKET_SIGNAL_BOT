from __future__ import annotations

import asyncio
import inspect
import logging
import multiprocessing as mp
import os
import queue
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("pocket_market")


# ============================================================
# CONFIG
# ============================================================

CONNECT_TIMEOUT = 60
AUTO_LOGIN_TIMEOUT = 120
LOGIN_LIBRARY_TIMEOUT = 90

BALANCE_TIMEOUT = 20
CANDLE_REQUEST_TIMEOUT = 30
CANDLES_HARD_TIMEOUT = 60
CLIENT_CLOSE_TIMEOUT = 10

PLAYWRIGHT_PREPARE_TIMEOUT = 120
PLAYWRIGHT_TEST_TIMEOUT = 25
PLAYWRIGHT_INSTALL_TIMEOUT = 300

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
# HELPERS
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


def _find_browser_executable(root: str) -> str | None:
    if not root or not os.path.exists(root):
        return None

    preferred_names = {
        "chrome",
        "chromium",
        "chromium-browser",
        "headless_shell",
    }

    try:
        for current_root, dirs, files in os.walk(root):
            for filename in files:
                if filename not in preferred_names:
                    continue

                path = os.path.join(current_root, filename)

                if os.path.isfile(path):
                    _make_executable(path)
                    return path
    except Exception as exc:
        logger.warning(
            "[PLAYWRIGHT] Не удалось просканировать %s: %s",
            root,
            exc,
        )

    return None


def _copy_tree(src: str, dst: str) -> bool:
    try:
        if not os.path.exists(src):
            return False

        os.makedirs(dst, exist_ok=True)

        for item in os.listdir(src):
            source = os.path.join(src, item)
            target = os.path.join(dst, item)

            if os.path.isdir(source):
                shutil.copytree(
                    source,
                    target,
                    dirs_exist_ok=True,
                )
            else:
                shutil.copy2(source, target)

        return True

    except Exception as exc:
        logger.warning(
            "[PLAYWRIGHT] Ошибка копирования %s -> %s: %s",
            src,
            dst,
            exc,
        )
        return False


# ============================================================
# PLAYWRIGHT TEST WORKER
# ============================================================

def _playwright_test_worker(result_queue: Any) -> None:
    """
    Отдельный процесс для проверки Chromium.

    ВАЖНО:
    Не запускаем sync_playwright внутри asyncio event loop.
    Отдельный процесс полностью изолирует зависший Chromium.
    """

    try:
        from playwright.sync_api import sync_playwright

        logger.info(
            "[PLAYWRIGHT TEST] Worker запущен."
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                timeout=15000,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--no-zygote",
                    "--disable-software-rasterizer",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            page = browser.new_page()

            page.goto(
                "about:blank",
                wait_until="domcontentloaded",
                timeout=10000,
            )

            title = page.title()

            page.close()
            browser.close()

            result_queue.put(
                {
                    "ok": True,
                    "title": title,
                }
            )

    except Exception as exc:
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
# POCKET OPTION LOGIN WORKER
# ============================================================

def _pocket_login_worker(
    result_queue: Any,
    email: str,
    password: str,
    demo: bool,
    headless: bool,
    timeout: int,
) -> None:
    """
    Pocket Option login в отдельном процессе.

    Это важно для Render:
    если библиотека Playwright зависнет,
    основной Telegram-процесс не заблокируется навсегда.
    """

    try:
        from BinaryOptionsToolsV2.pocketoption.tools.login import login

        logger.info(
            "[AUTO LOGIN WORKER] Запуск login()."
        )

        ssid = login(
            email,
            password,
            demo=demo,
            backend="playwright",
            headless=headless,
            timeout=timeout,
        )

        if not ssid:
            result_queue.put(
                {
                    "ok": False,
                    "error": "login() вернул пустой SSID",
                }
            )
            return

        result_queue.put(
            {
                "ok": True,
                "ssid": str(ssid),
            }
        )

    except Exception as exc:
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
# MARKET CLIENT
# ============================================================

class PocketMarket:

    def __init__(self) -> None:
        self.client: Any | None = None
        self.ssid: str | None = None

        self.connected: bool = False
        self.demo: bool = True

        self.email: str | None = None
        self.password: str | None = None

        self._connect_lock = asyncio.Lock()
        self._candles_lock = asyncio.Lock()

        logger.info(
            "[MARKET] PocketMarket создан."
        )

    # ========================================================
    # PLAYWRIGHT
    # ========================================================

    def _get_playwright_sources(self) -> list[str]:
        sources: list[str] = []

        env_sources = [
            os.getenv("POCKET_PLAYWRIGHT_SOURCE_PATH"),
            os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
        ]

        for path in env_sources:
            if path:
                sources.append(path)

        sources.extend(
            [
                "/opt/render/project/src/.cache/ms-playwright",
                "/opt/render/.cache/ms-playwright",
                "./.cache/ms-playwright",
            ]
        )

        result: list[str] = []

        for path in sources:
            if not path:
                continue

            path = os.path.abspath(path)

            if path not in result:
                result.append(path)

        return result

    def _launch_test_browser(self) -> bool:
        """
        Проверяет Chromium в отдельном процессе.

        Используется time.monotonic(), а не
        asyncio.get_event_loop(), потому что метод может
        выполняться внутри asyncio.to_thread().
        """

        ctx = mp.get_context("spawn")
        result_queue = ctx.Queue()

        process = ctx.Process(
            target=_playwright_test_worker,
            args=(result_queue,),
        )

        logger.info(
            "[PLAYWRIGHT] Проверяю запуск Chromium..."
        )

        started = time.monotonic()

        try:
            process.start()

            while time.monotonic() - started < PLAYWRIGHT_TEST_TIMEOUT:
                try:
                    result = result_queue.get(
                        timeout=0.5
                    )

                    if result.get("ok"):
                        logger.info(
                            "[PLAYWRIGHT] Chromium успешно запускается."
                        )
                        return True

                    logger.error(
                        "[PLAYWRIGHT TEST] ERROR: %s",
                        result.get("error"),
                    )
                    return False

                except queue.Empty:
                    if not process.is_alive():
                        break

            logger.error(
                "[PLAYWRIGHT] Chromium не ответил за %s секунд.",
                PLAYWRIGHT_TEST_TIMEOUT,
            )

            return False

        except Exception as exc:
            logger.error(
                "[PLAYWRIGHT] Ошибка запуска test worker: %s",
                exc,
            )
            return False

        finally:
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass

            try:
                process.join(timeout=5)
            except Exception:
                pass

            if process.is_alive():
                try:
                    process.kill()
                except Exception:
                    pass

    async def _install_chromium(self) -> bool:
        logger.info(
            "[PLAYWRIGHT] Chromium не найден. Устанавливаю..."
        )

        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = (
            RUNTIME_PLAYWRIGHT_PATH
        )

        os.makedirs(
            RUNTIME_PLAYWRIGHT_PATH,
            exist_ok=True,
        )

        command = [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=PLAYWRIGHT_INSTALL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[PLAYWRIGHT] Установка Chromium превысила timeout."
                )

                try:
                    process.kill()
                except Exception:
                    pass

                return False

            output = stdout.decode(
                "utf-8",
                errors="replace",
            )

            logger.info(
                "[PLAYWRIGHT] Install output:\n%s",
                output[-10000:],
            )

            if process.returncode != 0:
                logger.error(
                    "[PLAYWRIGHT] playwright install завершился с кодом %s",
                    process.returncode,
                )
                return False

            return True

        except Exception as exc:
            logger.error(
                "[PLAYWRIGHT] Ошибка установки Chromium: %s",
                exc,
            )
            return False

    async def _prepare_playwright_environment(self) -> bool:
        logger.info(
            "[PLAYWRIGHT] Runtime path: %s",
            RUNTIME_PLAYWRIGHT_PATH,
        )

        os.makedirs(
            RUNTIME_PLAYWRIGHT_PATH,
            exist_ok=True,
        )

        # ----------------------------------------------------
        # 1. Уже есть browser в runtime
        # ----------------------------------------------------

        executable = _find_browser_executable(
            RUNTIME_PLAYWRIGHT_PATH
        )

        if executable:
            logger.info(
                "[PLAYWRIGHT] Найден Chromium в runtime: %s",
                executable,
            )

            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
                RUNTIME_PLAYWRIGHT_PATH
            )

            test_ok = await asyncio.to_thread(
                self._launch_test_browser
            )

            if test_ok:
                return True

            logger.warning(
                "[PLAYWRIGHT] Chromium в runtime найден, "
                "но тест запуска не прошёл."
            )

        # ----------------------------------------------------
        # 2. Ищем browser в Render cache
        # ----------------------------------------------------

        for source in self._get_playwright_sources():
            logger.info(
                "[PLAYWRIGHT] Source path: %s",
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

            logger.info(
                "[PLAYWRIGHT] Копирую Playwright browser в runtime..."
            )

            copied = await asyncio.to_thread(
                _copy_tree,
                source,
                RUNTIME_PLAYWRIGHT_PATH,
            )

            if not copied:
                continue

            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
                RUNTIME_PLAYWRIGHT_PATH
            )

            executable = _find_browser_executable(
                RUNTIME_PLAYWRIGHT_PATH
            )

            if executable:
                logger.info(
                    "[PLAYWRIGHT] Runtime browser: %s",
                    executable,
                )

            test_ok = await asyncio.to_thread(
                self._launch_test_browser
            )

            if test_ok:
                return True

        # ----------------------------------------------------
        # 3. Устанавливаем Chromium
        # ----------------------------------------------------

        installed = await self._install_chromium()

        if not installed:
            logger.error(
                "[PLAYWRIGHT] Не удалось установить Chromium."
            )
            return False

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
            RUNTIME_PLAYWRIGHT_PATH
        )

        executable = _find_browser_executable(
            RUNTIME_PLAYWRIGHT_PATH
        )

        if executable:
            logger.info(
                "[PLAYWRIGHT] Chromium после установки: %s",
                executable,
            )

        test_ok = await asyncio.to_thread(
            self._launch_test_browser
        )

        if not test_ok:
            logger.error(
                "[PLAYWRIGHT] Chromium установлен, "
                "но не проходит smoke test."
            )
            return False

        return True

    # ========================================================
    # LOGIN
    # ========================================================

    async def _run_login_process(
        self,
        email: str,
        password: str,
        demo: bool,
        headless: bool,
    ) -> str | None:

        ctx = mp.get_context("spawn")
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
            ),
        )

        logger.info(
            "[AUTO LOGIN] Запускаю login worker."
        )

        started = time.monotonic()

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

                    if result.get("ok"):
                        ssid = result.get("ssid")

                        logger.info(
                            "[AUTO LOGIN] SSID успешно получен."
                        )

                        return str(ssid)

                    logger.error(
                        "[AUTO LOGIN] Login error: %s",
                        result.get("error"),
                    )

                    return None

                except queue.Empty:
                    if not process.is_alive():
                        break

            logger.error(
                "[AUTO LOGIN] Login worker timeout."
            )

            return None

        except Exception as exc:
            logger.error(
                "[AUTO LOGIN] Ошибка запуска login worker: %s",
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
                process.join(timeout=5)
            except Exception:
                pass

            if process.is_alive():
                try:
                    process.kill()
                except Exception:
                    pass

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

        prepared = await asyncio.wait_for(
            self._prepare_playwright_environment(),
            timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
        )

        if not prepared:
            logger.error(
                "[AUTO LOGIN] Playwright environment не готов."
            )
            return None

        logger.info(
            "[AUTO LOGIN] Playwright готов."
        )

        return await self._run_login_process(
            email=email,
            password=password,
            demo=demo,
            headless=headless,
        )

    # ========================================================
    # CLIENT
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

            client = PocketOptionAsync(ssid)

            await asyncio.sleep(
                WEBSOCKET_INIT_DELAY
            )

            logger.info(
                "[MARKET] PocketOptionAsync создан."
            )

            return client

        except Exception as exc:
            logger.exception(
                "[MARKET] Ошибка создания клиента: %s",
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

            if self.connected and self.client is not None:
                logger.info(
                    "[MARKET] Уже подключён."
                )
                return True

            self.demo = demo

            # ------------------------------------------------
            # SSID из аргумента
            # ------------------------------------------------

            if ssid:
                self.ssid = ssid

            # ------------------------------------------------
            # SSID из ENV
            # ------------------------------------------------

            if not self.ssid:
                env_ssid = os.getenv(
                    "PO_SSID"
                )

                if env_ssid:
                    self.ssid = env_ssid.strip()

                    logger.info(
                        "[MARKET] Использую PO_SSID из ENV."
                    )

            # ------------------------------------------------
            # AUTO LOGIN
            # ------------------------------------------------

            if not self.ssid:

                email = email or os.getenv(
                    "PO_EMAIL"
                )

                password = password or os.getenv(
                    "PO_PASSWORD"
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

                generated_ssid = await asyncio.wait_for(
                    self.auto_login(
                        email=email,
                        password=password,
                        demo=demo,
                        headless=headless,
                    ),
                    timeout=AUTO_LOGIN_TIMEOUT
                    + PLAYWRIGHT_PREPARE_TIMEOUT
                    + 30,
                )

                if not generated_ssid:
                    logger.error(
                        "[MARKET] Auto login не получил SSID."
                    )
                    return False

                self.ssid = generated_ssid

            # ------------------------------------------------
            # CREATE CLIENT
            # ------------------------------------------------

            logger.info(
                "[MARKET] STEP 2/5: "
                "Создаю Pocket Option client."
            )

            self.client = await asyncio.wait_for(
                self._create_client(
                    self.ssid
                ),
                timeout=CONNECT_TIMEOUT,
            )

            if self.client is None:
                return False

            # ------------------------------------------------
            # BALANCE TEST
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

                if balance_method is None:
                    logger.warning(
                        "[MARKET] У клиента нет balance(). "
                        "Продолжаю без balance check."
                    )
                else:
                    result = balance_method()

                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(
                            result,
                            timeout=BALANCE_TIMEOUT,
                        )

                    logger.info(
                        "[MARKET] Balance check OK: %s",
                        result,
                    )

            except Exception as exc:
                logger.error(
                    "[MARKET] Balance check failed: %s",
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

        """
        Совместимый метод для main.py / scheduler.

        Поддерживает:

            candles(
                pair,
                minutes=1,
                limit=300
            )

        Также понимает старый формат:

            period=60
            count=300
            offset=...
        """

        if "period" in kwargs:
            period = int(kwargs["period"])
        else:
            period = int(minutes) * 60

        if "count" in kwargs:
            count = int(kwargs["count"])
        else:
            count = int(limit)

        offset = kwargs.get(
            "offset",
            None,
        )

        return await self.get_candle_data(
            asset=asset,
            period=period,
            count=count,
            offset=offset,
        )

    async def get_candles(
        self,
        asset: str,
        period: int = 60,
        count: int = 300,
        offset: int | None = None,
    ) -> list[Candle]:

        return await self.get_candle_data(
            asset=asset,
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

        if self.client is None or not self.connected:
            logger.warning(
                "[MARKET] Candle request while disconnected."
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

        # ----------------------------------------------------
        # Рассчитываем offset
        # ----------------------------------------------------

        if offset is None:
            offset = period * count

        async with self._candles_lock:

            try:
                logger.info(
                    "[CANDLES] Запрашиваю %s: "
                    "period=%s count=%s offset=%s",
                    asset,
                    period,
                    count,
                    offset,
                )

                method = getattr(
                    self.client,
                    "get_candles",
                    None,
                )

                if method is None:
                    logger.error(
                        "[CANDLES] У клиента нет get_candles()."
                    )
                    return []

                # ------------------------------------------------
                # BinaryOptionsToolsV2:
                #
                # get_candles(asset, period, offset)
                # ------------------------------------------------

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

                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result,
                        timeout=CANDLE_REQUEST_TIMEOUT,
                    )

                candles = self._normalize_candles(
                    result
                )

                if not candles:
                    logger.warning(
                        "[CANDLES] %s: пустой результат.",
                        asset,
                    )
                    return []

                candles.sort(
                    key=lambda x: x.timestamp
                )

                # ------------------------------------------------
                # Удаляем дубликаты
                # ------------------------------------------------

                unique: dict[int, Candle] = {}

                for candle in candles:
                    unique[candle.timestamp] = candle

                candles = list(
                    unique.values()
                )

                candles.sort(
                    key=lambda x: x.timestamp
                )

                # ------------------------------------------------
                # Не отдаём формирующуюся свечу
                # ------------------------------------------------

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
                    if candle.timestamp < current_bucket
                ]

                # ------------------------------------------------
                # Последние count
                # ------------------------------------------------

                if len(candles) > count:
                    candles = candles[-count:]

                logger.info(
                    "[CANDLES] %s: получено %s свечей.",
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
                    "[CANDLES] Ошибка получения %s: %s",
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

        # ----------------------------------------------------
        # Некоторые версии API возвращают:
        #
        # {"data": [...]}
        # {"candles": [...]}
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # DataFrame
        # ----------------------------------------------------

        if hasattr(raw, "to_dict") and hasattr(
            raw,
            "columns",
        ):
            try:
                raw = raw.to_dict(
                    orient="records"
                )
            except Exception:
                pass

        if not isinstance(raw, (list, tuple)):
            return []

        result: list[Candle] = []

        for item in raw:

            try:

                # ------------------------------------------------
                # dict
                # ------------------------------------------------

                if isinstance(item, dict):

                    timestamp = (
                        item.get("timestamp")
                        or item.get("time")
                        or item.get("from")
                        or item.get("at")
                        or item.get("created_at")
                    )

                    open_price = (
                        item.get("open")
                        or item.get("o")
                    )

                    high_price = (
                        item.get("high")
                        or item.get("h")
                    )

                    low_price = (
                        item.get("low")
                        or item.get("l")
                    )

                    close_price = (
                        item.get("close")
                        or item.get("c")
                    )

                    volume = (
                        item.get("volume")
                        or item.get("v")
                        or 0
                    )

                # ------------------------------------------------
                # object
                # ------------------------------------------------

                else:

                    timestamp = (
                        getattr(item, "timestamp", None)
                        or getattr(item, "time", None)
                        or getattr(item, "from", None)
                    )

                    open_price = (
                        getattr(item, "open", None)
                        or getattr(item, "o", None)
                    )

                    high_price = (
                        getattr(item, "high", None)
                        or getattr(item, "h", None)
                    )

                    low_price = (
                        getattr(item, "low", None)
                        or getattr(item, "l", None)
                    )

                    close_price = (
                        getattr(item, "close", None)
                        or getattr(item, "c", None)
                    )

                    volume = (
                        getattr(item, "volume", None)
                        or getattr(item, "v", None)
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

                timestamp = float(timestamp)

                # milliseconds -> seconds
                if timestamp > 10_000_000_000:
                    timestamp /= 1000

                result.append(
                    Candle(
                        timestamp=int(timestamp),
                        open=float(open_price),
                        high=float(high_price),
                        low=float(low_price),
                        close=float(close_price),
                        volume=float(volume or 0),
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

        age = now - latest.timestamp

        if age < 0:
            return False

        if age > max_delay:
            logger.warning(
                "[CANDLES] Данные устарели: age=%ss max=%ss",
                age,
                max_delay,
            )
            return False

        return True

    # ========================================================
    # MARKET TEST
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
                    "[MARKET] У клиента нет close/disconnect."
                )
                return

            result = close_method()

            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    result,
                    timeout=CLIENT_CLOSE_TIMEOUT,
                )

            logger.info(
                "[MARKET] Client закрыт."
            )

        except asyncio.TimeoutError:
            logger.warning(
                "[MARKET] Client close timeout."
            )

        except Exception as exc:
            logger.warning(
                "[MARKET] Ошибка закрытия client: %s",
                exc,
            )

    async def disconnect(self) -> None:
        await self.close()


# ============================================================
# GLOBAL MARKET INSTANCE
# ============================================================

market = PocketMarket()


__all__ = [
    "Candle",
    "PocketMarket",
    "market",
]
