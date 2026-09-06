from __future__ import annotations

import asyncio
import inspect
import logging
import multiprocessing
import os
import queue
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


from config import config


logger = logging.getLogger("pocket_market")


# ============================================================
# TIMEOUTS
# ============================================================

CONNECT_TIMEOUT = 60
AUTO_LOGIN_TIMEOUT = 120
BALANCE_TIMEOUT = 20
CANDLE_REQUEST_TIMEOUT = 30
CLIENT_CLOSE_TIMEOUT = 10
CANDLES_HARD_TIMEOUT = 60

PLAYWRIGHT_PREPARE_TIMEOUT = 120
LOGIN_LIBRARY_TIMEOUT = 90

WEBSOCKET_INIT_DELAY = 5


# ============================================================
# PLAYWRIGHT
# ============================================================

RUNTIME_PLAYWRIGHT_PATH = os.environ.get(
    "POCKET_PLAYWRIGHT_RUNTIME_PATH",
    "/tmp/pocket-option-ms-playwright",
)


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
# LOGIN WORKER
# ============================================================

def _pocket_login_worker(
    result_queue: Any,
    email: str,
    password: str,
    demo: bool,
    headless: bool,
    timeout: int,
    browser_path: str,
) -> None:
    """
    Запускает синхронный BinaryOptionsToolsV2 login()
    в отдельном процессе.

    Это важно: если Playwright/login зависнет,
    основной asyncio event loop не зависнет вместе с ним.
    """

    try:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
        os.environ["PYTHONUNBUFFERED"] = "1"

        if os.name == "posix":
            try:
                os.setsid()
            except Exception:
                pass

        print(
            "[LOGIN WORKER] =====================================",
            flush=True,
        )

        print(
            "[LOGIN WORKER] START",
            flush=True,
        )

        print(
            f"[LOGIN WORKER] Browser path: {browser_path}",
            flush=True,
        )

        print(
            f"[LOGIN WORKER] Email: {email[:3]}***",
            flush=True,
        )

        print(
            f"[LOGIN WORKER] demo={demo}",
            flush=True,
        )

        print(
            f"[LOGIN WORKER] headless={headless}",
            flush=True,
        )

        print(
            f"[LOGIN WORKER] timeout={timeout}",
            flush=True,
        )

        print(
            "[LOGIN WORKER] Импортирую BinaryOptionsToolsV2...",
            flush=True,
        )

        from BinaryOptionsToolsV2.pocketoption.tools.login import (
            login,
        )

        print(
            "[LOGIN WORKER] BinaryOptionsToolsV2 импортирован.",
            flush=True,
        )

        print(
            "[LOGIN WORKER] Запускаю login()...",
            flush=True,
        )

        ssid = login(
            email,
            password,
            demo=demo,
            backend="playwright",
            headless=headless,
            timeout=timeout,
        )

        print(
            "[LOGIN WORKER] login() завершился.",
            flush=True,
        )

        if ssid:
            ssid_text = str(ssid).strip()

            if ssid_text:
                result_queue.put(
                    (
                        "ok",
                        ssid_text,
                    )
                )

                print(
                    "[LOGIN WORKER] SSID получен.",
                    flush=True,
                )

                return

        error_text = "login() вернул пустой SSID."

        try:
            result_queue.put(
                (
                    "error",
                    error_text,
                )
            )
        except Exception:
            pass

        print(
            f"[LOGIN WORKER] ERROR: {error_text}",
            flush=True,
        )

    except BaseException as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        print(
            f"[LOGIN WORKER] ERROR: {error_text}",
            flush=True,
        )

        try:
            result_queue.put(
                (
                    "error",
                    error_text,
                )
            )
        except Exception:
            pass


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

        self._playwright_ready = False
        self._playwright_path: str | None = None

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
    # PLAYWRIGHT SOURCE
    # ========================================================

    @staticmethod
    def _get_playwright_source_path() -> str:
        explicit = os.environ.get(
            "POCKET_PLAYWRIGHT_SOURCE_PATH"
        )

        if explicit:
            return os.path.abspath(
                os.path.expanduser(explicit)
            )

        configured = os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH"
        )

        runtime = os.path.abspath(
            RUNTIME_PLAYWRIGHT_PATH
        )

        if configured:
            configured = os.path.abspath(
                os.path.expanduser(configured)
            )

            if configured != runtime:
                if os.path.isdir(configured):
                    return configured

        candidates = [
            "/opt/render/project/src/.cache/ms-playwright",
            "/opt/render/.cache/ms-playwright",
            os.path.join(
                os.getcwd(),
                ".cache",
                "ms-playwright",
            ),
        ]

        for path in candidates:
            if os.path.isdir(path):
                return os.path.abspath(path)

        return os.path.abspath(
            os.path.join(
                os.getcwd(),
                ".cache",
                "ms-playwright",
            )
        )

    # ========================================================
    # PLAYWRIGHT RUNTIME
    # ========================================================

    @staticmethod
    def _get_playwright_runtime_path() -> str:
        return os.path.abspath(
            os.path.expanduser(
                RUNTIME_PLAYWRIGHT_PATH
            )
        )

    # ========================================================
    # FIND BROWSER
    # ========================================================

    @staticmethod
    def _find_browser_executables(
        browser_path: str,
    ) -> list[str]:

        result: list[str] = []

        if not os.path.isdir(browser_path):
            return result

        try:
            for root, _, files in os.walk(
                browser_path
            ):
                for filename in files:
                    if filename in {
                        "chrome",
                        "chromium",
                        "chrome-headless-shell",
                    }:
                        path = os.path.join(
                            root,
                            filename,
                        )

                        if os.path.isfile(path):
                            result.append(path)

        except Exception:
            logger.exception(
                "[PLAYWRIGHT] Ошибка поиска Chromium."
            )

        return result

    @staticmethod
    def _find_chromium_executable(
        browser_path: str,
    ) -> str | None:

        paths = (
            PocketMarket
            ._find_browser_executables(
                browser_path
            )
        )

        priority = (
            "chrome",
            "chromium",
            "chrome-headless-shell",
        )

        for name in priority:
            for path in paths:
                if (
                    os.path.basename(path).lower()
                    == name
                ):
                    return path

        return None

    # ========================================================
    # EXECUTABLE PERMISSIONS
    # ========================================================

    @staticmethod
    def _make_executable(
        path: str,
    ) -> None:

        try:
            mode = os.stat(path).st_mode

            os.chmod(
                path,
                mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH,
            )

        except Exception as exc:
            logger.warning(
                "[PLAYWRIGHT] chmod failed %s: %s",
                path,
                exc,
            )

    # ========================================================
    # TEST BROWSER
    # ========================================================

    @staticmethod
    def _launch_test_browser(
        executable_path: str,
    ) -> None:

        from playwright.sync_api import (
            sync_playwright,
        )

        logger.info(
            "[PLAYWRIGHT] Проверяю запуск Chromium..."
        )

        with sync_playwright() as pw:

            browser = pw.chromium.launch(
                executable_path=executable_path,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--no-zygote",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            try:
                page = browser.new_page()

                page.goto(
                    "about:blank",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )

            finally:
                browser.close()

        logger.info(
            "[PLAYWRIGHT] Chromium успешно запущен."
        )

    # ========================================================
    # INSTALL CHROMIUM
    # ========================================================

    @staticmethod
    def _install_chromium(
        browser_path: str,
    ) -> None:

        os.makedirs(
            browser_path,
            exist_ok=True,
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

        logger.warning(
            "[PLAYWRIGHT] Устанавливаю Chromium..."
        )

        process = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )

        output = (
            process.stdout or ""
        ).strip()

        if output:
            logger.info(
                "[PLAYWRIGHT] Install output:\n%s",
                output[-12000:],
            )

        if process.returncode != 0:
            raise RuntimeError(
                "Playwright Chromium installation "
                f"failed with code {process.returncode}"
            )

    # ========================================================
    # COPY BROWSER TO /tmp
    # ========================================================

    @staticmethod
    def _copy_chromium_to_runtime(
        source_executable: str,
        runtime_path: str,
    ) -> str:

        if not os.path.isfile(
            source_executable
        ):
            raise RuntimeError(
                "Chromium executable not found: "
                f"{source_executable}"
            )

        source_dir = os.path.dirname(
            source_executable
        )

        browser_root = source_dir

        for _ in range(10):

            name = os.path.basename(
                browser_root
            ).lower()

            if name.startswith(
                "chromium-"
            ):
                break

            parent = os.path.dirname(
                browser_root
            )

            if parent == browser_root:
                break

            browser_root = parent

        name = os.path.basename(
            browser_root
        )

        if not name.lower().startswith(
            "chromium-"
        ):
            raise RuntimeError(
                "Не удалось определить Chromium root."
            )

        destination = os.path.join(
            runtime_path,
            name,
        )

        os.makedirs(
            runtime_path,
            exist_ok=True,
        )

        existing = (
            PocketMarket
            ._find_chromium_executable(
                destination
            )
        )

        if existing:
            PocketMarket._make_executable(
                existing
            )

            logger.info(
                "[PLAYWRIGHT] Runtime Chromium: %s",
                existing,
            )

            return existing

        logger.info(
            "[PLAYWRIGHT] Копирую Chromium:"
        )

        logger.info(
            "[PLAYWRIGHT] SOURCE: %s",
            browser_root,
        )

        logger.info(
            "[PLAYWRIGHT] DEST: %s",
            destination,
        )

        shutil.copytree(
            browser_root,
            destination,
            dirs_exist_ok=True,
        )

        for root, _, files in os.walk(
            destination
        ):
            for filename in files:

                if filename in {
                    "chrome",
                    "chromium",
                    "chrome-headless-shell",
                }:
                    PocketMarket._make_executable(
                        os.path.join(
                            root,
                            filename,
                        )
                    )

        relative = os.path.relpath(
            source_executable,
            browser_root,
        )

        runtime_executable = os.path.join(
            destination,
            relative,
        )

        if not os.path.isfile(
            runtime_executable
        ):
            runtime_executable = (
                PocketMarket
                ._find_chromium_executable(
                    destination
                )
            )

        if not runtime_executable:
            raise RuntimeError(
                "После копирования Chromium "
                "executable не найден."
            )

        PocketMarket._make_executable(
            runtime_executable
        )

        return runtime_executable

    # ========================================================
    # PREPARE PLAYWRIGHT
    # ========================================================

    def _prepare_playwright_environment(
        self,
    ) -> str:

        if (
            self._playwright_ready
            and self._playwright_path
            and os.path.isfile(
                self._playwright_path
            )
        ):
            logger.info(
                "[PLAYWRIGHT] Уже подготовлен: %s",
                self._playwright_path,
            )

            return self._playwright_path

        runtime = (
            self
            ._get_playwright_runtime_path()
        )

        source = (
            self
            ._get_playwright_source_path()
        )

        logger.info(
            "[PLAYWRIGHT] Runtime path: %s",
            runtime,
        )

        logger.info(
            "[PLAYWRIGHT] Source path: %s",
            source,
        )

        os.makedirs(
            runtime,
            exist_ok=True,
        )

        # ----------------------------------------------------
        # 1. Уже установленный runtime Chromium
        # ----------------------------------------------------

        executable = (
            self
            ._find_chromium_executable(
                runtime
            )
        )

        if executable:

            logger.info(
                "[PLAYWRIGHT] Найден Chromium "
                "в runtime: %s",
                executable,
            )

            self._make_executable(
                executable
            )

            try:
                self._launch_test_browser(
                    executable
                )

                self._playwright_ready = True
                self._playwright_path = executable

                return executable

            except Exception as exc:

                logger.warning(
                    "[PLAYWRIGHT] Runtime Chromium "
                    "не прошёл тест: %s",
                    exc,
                )

        # ----------------------------------------------------
        # 2. Chromium из Render cache
        # ----------------------------------------------------

        source_executable = (
            self
            ._find_chromium_executable(
                source
            )
        )

        if source_executable:

            logger.info(
                "[PLAYWRIGHT] Найден Chromium "
                "в source cache: %s",
                source_executable,
            )

            executable = (
                self
                ._copy_chromium_to_runtime(
                    source_executable,
                    runtime,
                )
            )

            self._launch_test_browser(
                executable
            )

            self._playwright_ready = True
            self._playwright_path = executable

            return executable

        # ----------------------------------------------------
        # 3. Install
        # ----------------------------------------------------

        logger.warning(
            "[PLAYWRIGHT] Chromium не найден. "
            "Запускаю playwright install chromium."
        )

        self._install_chromium(
            runtime
        )

        executable = (
            self
            ._find_chromium_executable(
                runtime
            )
        )

        if not executable:
            raise RuntimeError(
                "Chromium установился, "
                "но executable не найден."
            )

        self._make_executable(
            executable
        )

        self._launch_test_browser(
            executable
        )

        self._playwright_ready = True
        self._playwright_path = executable

        return executable

    # ========================================================
    # RUN LOGIN PROCESS
    # ========================================================

    async def _run_login_process(
        self,
        browser_path: str,
    ) -> str:

        logger.info(
            "[AUTO LOGIN] Запускаю login worker..."
        )

        ctx = multiprocessing.get_context(
            "spawn"
        )

        result_queue = ctx.Queue()

        process = ctx.Process(
            target=_pocket_login_worker,
            args=(
                result_queue,
                config.po_email,
                config.po_password,
                bool(config.po_demo),
                True,
                LOGIN_LIBRARY_TIMEOUT,
                browser_path,
            ),
            daemon=False,
        )

        process.start()

        logger.info(
            "[AUTO LOGIN] Worker started. PID=%s",
            process.pid,
        )

        deadline = (
            asyncio.get_running_loop().time()
            + AUTO_LOGIN_TIMEOUT
        )

        while True:

            try:
                result = result_queue.get_nowait()

                status, value = result

                if status == "ok":
                    logger.info(
                        "[AUTO LOGIN] SSID получен."
                    )

                    return str(value).strip()

                raise RuntimeError(
                    str(value)
                )

            except queue.Empty:
                pass

            if not process.is_alive():

                try:
                    result = result_queue.get_nowait()

                    status, value = result

                    if status == "ok":
                        return str(value).strip()

                    raise RuntimeError(
                        str(value)
                    )

                except queue.Empty:
                    pass

                exit_code = process.exitcode

                raise RuntimeError(
                    "Login worker завершился "
                    f"без результата. exitcode={exit_code}"
                )

            if (
                asyncio.get_running_loop().time()
                >= deadline
            ):

                logger.error(
                    "[AUTO LOGIN] Timeout "
                    "ожидания login worker."
                )

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

                raise TimeoutError(
                    "Pocket Option login "
                    "превысил лимит 120 секунд."
                )

            await asyncio.sleep(
                0.25
            )

    # ========================================================
    # AUTO LOGIN
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
            "[AUTO LOGIN] ====================================="
        )

        logger.info(
            "[AUTO LOGIN] Email: %s***",
            config.po_email[:3],
        )

        logger.info(
            "[AUTO LOGIN] Demo: %s",
            config.po_demo,
        )

        logger.info(
            "[AUTO LOGIN] Подготавливаю Playwright..."
        )

        browser_path = await asyncio.wait_for(
            asyncio.to_thread(
                self._prepare_playwright_environment
            ),
            timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
        )

        logger.info(
            "[AUTO LOGIN] Playwright готов: %s",
            browser_path,
        )

        ssid = await self._run_login_process(
            browser_path
        )

        if not ssid:
            raise RuntimeError(
                "Pocket Option login вернул пустой SSID."
            )

        self.ssid = ssid

        logger.info(
            "[AUTO LOGIN] Успешный вход."
        )

        return ssid

    # ========================================================
    # CALL METHOD
    # ========================================================

    async def _call_method(
        self,
        method: Any,
        *args,
        timeout: float,
        **kwargs,
    ):

        if inspect.iscoroutinefunction(
            method
        ):
            return await asyncio.wait_for(
                method(
                    *args,
                    **kwargs,
                ),
                timeout=timeout,
            )

        result = await asyncio.wait_for(
            asyncio.to_thread(
                method,
                *args,
                **kwargs,
            ),
            timeout=timeout,
        )

        if inspect.isawaitable(
            result
        ):
            return await asyncio.wait_for(
                result,
                timeout=timeout,
            )

        return result

    # ========================================================
    # CREATE CLIENT
    # ========================================================

    async def _create_client(
        self,
        ssid: str,
    ) -> Any:

        logger.info(
            "[MARKET] Создаю PocketOptionAsync..."
        )

        from BinaryOptionsToolsV2.pocketoption import (
            PocketOptionAsync,
        )

        # ВАЖНО:
        # PocketOptionAsync является async-клиентом.
        # Не создаём его через to_thread().
        client = PocketOptionAsync(
            ssid
        )

        logger.info(
            "[MARKET] PocketOptionAsync создан."
        )

        # В текущих версиях библиотека сама
        # инициализирует соединение после создания.
        # Дадим WebSocket время подняться.

        await asyncio.sleep(
            WEBSOCKET_INIT_DELAY
        )

        # Если у объекта есть connect(),
        # вызываем его только если клиент ещё
        # не подключён.

        connect_method = getattr(
            client,
            "connect",
            None,
        )

        if connect_method:
            try:
                await self._call_method(
                    connect_method,
                    timeout=CLIENT_CLOSE_TIMEOUT
                    + CLIENT_INIT_TIMEOUT,
                )

                logger.info(
                    "[MARKET] connect() выполнен."
                )

            except Exception as exc:
                # Некоторые версии клиента уже
                # подключаются автоматически.
                logger.debug(
                    "[MARKET] connect() не потребовался "
                    "или вернул ошибку: %s",
                    exc,
                )

        return client

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:

        async with self.lock:

            if self.is_connected:
                logger.info(
                    "[MARKET] Уже подключен."
                )

                return True

            started = (
                asyncio.get_running_loop().time()
            )

            self.last_error = None

            logger.info(
                "[MARKET] ====================================="
            )

            logger.info(
                "[MARKET] Начинаю подключение."
            )

            try:

                # ------------------------------------------------
                # STEP 1 — AUTH
                # ------------------------------------------------

                if config.po_ssid:

                    ssid = (
                        config.po_ssid.strip()
                    )

                    logger.info(
                        "[MARKET] STEP 1/5: "
                        "Использую PO_SSID."
                    )

                elif config.po_auto_login:

                    logger.info(
                        "[MARKET] STEP 1/5: "
                        "Запускаю автоматический login."
                    )

                    ssid = await asyncio.wait_for(
                        self.auto_login(),
                        timeout=(
                            AUTO_LOGIN_TIMEOUT
                            + PLAYWRIGHT_PREPARE_TIMEOUT
                        ),
                    )

                else:

                    raise RuntimeError(
                        "PO_SSID отсутствует, "
                        "а PO_AUTO_LOGIN отключён."
                    )

                if not ssid:
                    raise RuntimeError(
                        "Получен пустой SSID."
                    )

                self.ssid = ssid

                logger.info(
                    "[MARKET] STEP 1/5: AUTH OK."
                )

                # ------------------------------------------------
                # STEP 2 — CLIENT
                # ------------------------------------------------

                logger.info(
                    "[MARKET] STEP 2/5: "
                    "Создание async клиента."
                )

                client = await asyncio.wait_for(
                    self._create_client(
                        ssid
                    ),
                    timeout=CONNECT_TIMEOUT,
                )

                self.client = client

                logger.info(
                    "[MARKET] STEP 2/5: "
                    "CLIENT OK."
                )

                # ------------------------------------------------
                # STEP 3 — BALANCE
                # ------------------------------------------------

                logger.info(
                    "[MARKET] STEP 3/5: "
                    "Проверка WebSocket/balance."
                )

                balance_method = getattr(
                    client,
                    "balance",
                    None,
                )

                if balance_method:

                    balance = await self._call_method(
                        balance_method,
                        timeout=BALANCE_TIMEOUT,
                    )

                    logger.info(
                        "[MARKET] STEP 3/5: "
                        "BALANCE OK: %s",
                        balance,
                    )

                else:

                    logger.warning(
                        "[MARKET] balance() отсутствует. "
                        "Продолжаю."
                    )

                # ------------------------------------------------
                # STEP 4 — STATE
                # ------------------------------------------------

                logger.info(
                    "[MARKET] STEP 4/5: "
                    "Проверка клиента."
                )

                if self.client is None:
                    raise RuntimeError(
                        "PocketOptionAsync client is None."
                    )

                self.connected = True

                logger.info(
                    "[MARKET] STEP 4/5: "
                    "CLIENT READY."
                )

                # ------------------------------------------------
                # STEP 5 — DONE
                # ------------------------------------------------

                self.last_success = (
                    datetime.now(
                        timezone.utc
                    )
                )

                elapsed = (
                    asyncio.get_running_loop().time()
                    - started
                )

                logger.info(
                    "[MARKET] STEP 5/5: "
                    "ПОКЕТ ОПШН ПОДКЛЮЧЕН. "
                    "Время: %.1f сек.",
                    elapsed,
                )

                return True

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                self.connected = False
                self.last_error = (
                    f"{type(exc).__name__}: {exc}"
                )

                logger.exception(
                    "[MARKET] CONNECTION FAILED: %s",
                    exc,
                )

                raise

    # ========================================================
    # ENSURE CONNECTED
    # ========================================================

    async def _ensure_connected(
        self,
    ) -> None:

        if self.is_connected:
            return

        await self.connect()

    # ========================================================
    # NORMALIZE CANDLE
    # ========================================================

    @staticmethod
    def _normalize_timestamp(
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
            raise ValueError(
                "Candle timestamp is empty."
            )

        number = float(value)

        if number > 10_000_000_000:
            number /= 1000.0

        return datetime.fromtimestamp(
            number,
            tz=timezone.utc,
        )

    @classmethod
    def _normalize_candle(
        cls,
        item: Any,
    ) -> Candle | None:

        if isinstance(
            item,
            Candle,
        ):
            return item

        if hasattr(
            item,
            "model_dump",
        ):
            try:
                item = item.model_dump()
            except Exception:
                pass

        if hasattr(
            item,
            "__dict__",
        ) and not isinstance(
            item,
            dict,
        ):
            try:
                item = vars(item)
            except Exception:
                pass

        if not isinstance(
            item,
            dict,
        ):
            return None

        time_value = (
            item.get("time")
            if "time" in item
            else item.get("timestamp")
        )

        if time_value is None:
            time_value = item.get(
                "datetime"
            )

        open_value = item.get(
            "open"
        )

        high_value = item.get(
            "high"
        )

        low_value = item.get(
            "low"
        )

        close_value = item.get(
            "close"
        )

        if (
            time_value is None
            or open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
        ):
            return None

        try:

            return Candle(
                time=cls._normalize_timestamp(
                    time_value
                ),
                open=float(
                    open_value
                ),
                high=float(
                    high_value
                ),
                low=float(
                    low_value
                ),
                close=float(
                    close_value
                ),
                volume=float(
                    item.get(
                        "volume",
                        0.0,
                    )
                    or 0.0
                ),
            )

        except Exception:
            return None

    # ========================================================
    # EXTRACT CANDLES
    # ========================================================

    @classmethod
    def _extract_candles(
        cls,
        raw: Any,
    ) -> list[Candle]:

        if raw is None:
            return []

        if isinstance(
            raw,
            dict,
        ):

            for key in (
                "candles",
                "data",
                "history",
                "result",
            ):
                if key in raw:
                    return cls._extract_candles(
                        raw[key]
                    )

            one = cls._normalize_candle(
                raw
            )

            return (
                [one]
                if one is not None
                else []
            )

        if isinstance(
            raw,
            (list, tuple),
        ):

            result: list[Candle] = []

            for item in raw:

                candle = (
                    cls._normalize_candle(
                        item
                    )
                )

                if candle is not None:
                    result.append(
                        candle
                    )

            return result

        return []

    # ========================================================
    # REQUEST RAW CANDLES
    # ========================================================

    async def _request_raw_candles(
        self,
        asset: str,
        minutes: int,
        limit: int,
    ) -> Any:

        if self.client is None:
            raise RuntimeError(
                "Market client is not initialized."
            )

        period = max(
            1,
            int(minutes),
        ) * 60

        # get_candles() API:
        #
        # asset
        # period = timeframe seconds
        # offset = history seconds
        #
        # Поэтому для 300 свечей по 1 минуте
        # запрашиваем примерно 300 минут истории.

        offset = max(
            period,
            int(limit) * period,
        )

        get_candles = getattr(
            self.client,
            "get_candles",
            None,
        )

        if get_candles:

            logger.info(
                "[MARKET] get_candles("
                "%s, period=%s, offset=%s)",
                asset,
                period,
                offset,
            )

            return await self._call_method(
                get_candles,
                asset,
                period,
                offset,
                timeout=CANDLE_REQUEST_TIMEOUT,
            )

        candles_method = getattr(
            self.client,
            "candles",
            None,
        )

        if candles_method:

            logger.info(
                "[MARKET] candles("
                "%s, period=%s)",
                asset,
                period,
            )

            return await self._call_method(
                candles_method,
                asset,
                period,
                timeout=CANDLE_REQUEST_TIMEOUT,
            )

        history_method = getattr(
            self.client,
            "history",
            None,
        )

        if history_method:

            logger.info(
                "[MARKET] history("
                "%s, period=%s)",
                asset,
                period,
            )

            return await self._call_method(
                history_method,
                asset,
                period,
                timeout=CANDLE_REQUEST_TIMEOUT,
            )

        raise RuntimeError(
            "PocketOptionAsync не имеет "
            "get_candles/candles/history."
        )

    # ========================================================
    # CANDLES
    # ========================================================

    async def candles(
        self,
        asset: str,
        minutes: int = 1,
        limit: int = 300,
        **kwargs,
    ) -> list[Candle]:

        # Совместимость с возможными вызовами:
        #
        # candles(pair, minutes=1, limit=300)
        #
        # candles(pair, period=60, count=300)

        if "period" in kwargs:
            period = int(
                kwargs["period"]
            )

            minutes = max(
                1,
                period // 60,
            )

        if "count" in kwargs:
            limit = int(
                kwargs["count"]
            )

        if "offset" in kwargs:
            offset = int(
                kwargs["offset"]
            )

            if minutes <= 0:
                minutes = 1

            limit = max(
                1,
                offset // (
                    minutes * 60
                ),
            )

        minutes = max(
            1,
            int(minutes),
        )

        limit = max(
            1,
            int(limit),
        )

        logger.info(
            "[MARKET] candles request: "
            "asset=%s minutes=%s limit=%s",
            asset,
            minutes,
            limit,
        )

        await self._ensure_connected()

        try:

            raw = await asyncio.wait_for(
                self._request_raw_candles(
                    asset,
                    minutes,
                    limit,
                ),
                timeout=CANDLES_HARD_TIMEOUT,
            )

            result = (
                self._extract_candles(
                    raw
                )
            )

            if not result:
                raise RuntimeError(
                    "Pocket Option вернул "
                    "0 свечей."
                )

            result.sort(
                key=lambda candle: candle.time
            )

            # Удаляем дубликаты по времени.

            unique: dict[
                int,
                Candle,
            ] = {}

            for candle in result:
                key = int(
                    candle.time.timestamp()
                )

                unique[key] = candle

            result = [
                unique[key]
                for key in sorted(unique)
            ]

            # Не отдаём незакрытую свечу.

            now = datetime.now(
                timezone.utc
            )

            candle_seconds = (
                minutes * 60
            )

            if result:

                last = result[-1]

                if (
                    last.time.timestamp()
                    + candle_seconds
                    > now.timestamp()
                ):
                    result.pop()

            if len(result) > limit:
                result = result[-limit:]

            logger.info(
                "[MARKET] Получено свечей: %s "
                "для %s",
                len(result),
                asset,
            )

            if result:
                logger.info(
                    "[MARKET] Диапазон свечей: "
                    "%s -> %s",
                    result[0].time.isoformat(),
                    result[-1].time.isoformat(),
                )

            return result

        except asyncio.TimeoutError:
            self.last_error = (
                "Candle request timeout."
            )

            logger.error(
                "[MARKET] Timeout получения "
                "свечей для %s.",
                asset,
            )

            raise

        except Exception as exc:

            self.last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            logger.exception(
                "[MARKET] Ошибка получения "
                "свечей для %s: %s",
                asset,
                exc,
            )

            raise

    # ========================================================
    # GET CANDLES
    # ========================================================

    async def get_candles(
        self,
        asset: str,
        period: int = 60,
        count: int = 300,
    ) -> list[Candle]:

        minutes = max(
            1,
            int(period) // 60,
        )

        return await self.candles(
            asset,
            minutes=minutes,
            limit=count,
        )

    # ========================================================
    # CANDLE DATA
    # ========================================================

    async def get_candle_data(
        self,
        asset: str,
        period: int = 60,
        count: int = 300,
    ) -> list[dict[str, Any]]:

        candles = await self.get_candles(
            asset,
            period=period,
            count=count,
        )

        return [
            {
                "datetime": candle.time,
                "time": candle.time,
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

    async def validate_freshness(
        self,
        asset: str,
        max_age_seconds: int = 180,
    ) -> bool:

        candles = await self.candles(
            asset,
            minutes=1,
            limit=5,
        )

        if not candles:
            return False

        last = candles[-1]

        age = (
            datetime.now(
                timezone.utc
            )
            - last.time
        ).total_seconds()

        logger.info(
            "[MARKET] Freshness %s: "
            "%.1f sec",
            asset,
            age,
        )

        return age <= max_age_seconds

    # ========================================================
    # TEST MARKET
    # ========================================================

    async def test_market(
        self,
        asset: str = "EURUSD_otc",
    ) -> bool:

        try:

            await self._ensure_connected()

            candles = await self.candles(
                asset,
                minutes=1,
                limit=10,
            )

            if len(candles) < 5:
                return False

            logger.info(
                "[MARKET] TEST OK: %s candles=%s",
                asset,
                len(candles),
            )

            return True

        except Exception:

            logger.exception(
                "[MARKET] TEST FAILED."
            )

            return False

    # ========================================================
    # STATUS
    # ========================================================

    def status(self) -> dict[str, Any]:

        return {
            "connected": self.connected,
            "has_client": self.client is not None,
            "has_ssid": bool(self.ssid),
            "last_error": self.last_error,
            "last_success": (
                self.last_success.isoformat()
                if self.last_success
                else None
            ),
            "playwright_ready": (
                self._playwright_ready
            ),
            "playwright_path": (
                self._playwright_path
            ),
        }

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self) -> None:

        async with self.lock:

            client = self.client

            self.client = None
            self.connected = False

            if client is None:
                return

            logger.info(
                "[MARKET] Закрываю Pocket Option client..."
            )

            for method_name in (
                "disconnect",
                "close",
                "shutdown",
            ):

                method = getattr(
                    client,
                    method_name,
                    None,
                )

                if not method:
                    continue

                try:

                    await self._call_method(
                        method,
                        timeout=CLIENT_CLOSE_TIMEOUT,
                    )

                    logger.info(
                        "[MARKET] Client closed "
                        "using %s().",
                        method_name,
                    )

                    break

                except Exception as exc:

                    logger.debug(
                        "[MARKET] %s() failed: %s",
                        method_name,
                        exc,
                    )

            self.ssid = None

            logger.info(
                "[MARKET] Client полностью закрыт."
            )


# ============================================================
# GLOBAL MARKET INSTANCE
# ============================================================

market = PocketMarket()
