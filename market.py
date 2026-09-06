from __future__ import annotations

import asyncio
import inspect
import logging
import multiprocessing
import os
import queue
import shutil
import signal
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

CONNECT_TIMEOUT = 30
AUTO_LOGIN_TIMEOUT = 120
BALANCE_TIMEOUT = 15
CANDLE_REQUEST_TIMEOUT = 20
CLIENT_CLOSE_TIMEOUT = 5
WEBSOCKET_INIT_DELAY = 5

PLAYWRIGHT_PREPARE_TIMEOUT = 120
LOGIN_LIBRARY_TIMEOUT = 90

MARKET_TEST_CONNECT_EXTRA = 30

# Жёсткие дополнительные ограничения
CLIENT_CREATE_TIMEOUT = 30
CLIENT_INIT_TIMEOUT = 20
CONNECT_HARD_TIMEOUT = 180
CANDLES_HARD_TIMEOUT = 45

# ============================================================
# PLAYWRIGHT PATHS
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
    Запускает Pocket Option login() в отдельном процессе.

    Отдельный процесс используется специально:
    если библиотека зависнет внутри Playwright/login(),
    основной asyncio event loop не блокируется.
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
            f"[LOGIN WORKER] Email: "
            f"{email[:3]}***",
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
            "[LOGIN WORKER] Импортирую "
            "BinaryOptionsToolsV2...",
            flush=True,
        )

        from BinaryOptionsToolsV2.pocketoption.tools.login import (
            login,
        )

        print(
            "[LOGIN WORKER] "
            "BinaryOptionsToolsV2 импортирован.",
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
                    "[LOGIN WORKER] "
                    "✅ SSID получен.",
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
            f"[LOGIN WORKER] ❌ {error_text}",
            flush=True,
        )

    except BaseException as exc:

        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        print(
            f"[LOGIN WORKER] ❌ ERROR: {error_text}",
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
    # PLAYWRIGHT SOURCE PATH
    # ========================================================

    @staticmethod
    def _get_playwright_source_path() -> str:
        """
        Определяет source cache Playwright.

        Приоритет:

        1. POCKET_PLAYWRIGHT_SOURCE_PATH
        2. PLAYWRIGHT_BROWSERS_PATH
        3. Render cache
        4. .cache/ms-playwright
        """

        explicit_source = os.environ.get(
            "POCKET_PLAYWRIGHT_SOURCE_PATH"
        )

        if explicit_source:
            return os.path.abspath(
                os.path.expanduser(
                    explicit_source
                )
            )

        configured_path = os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH"
        )

        runtime_path = os.path.abspath(
            RUNTIME_PLAYWRIGHT_PATH
        )

        if configured_path:
            configured_path = os.path.abspath(
                os.path.expanduser(
                    configured_path
                )
            )

            if configured_path != runtime_path:
                return configured_path

        render_paths = [
            "/opt/render/project/src/.cache/ms-playwright",
            "/opt/render/.cache/ms-playwright",
        ]

        for render_path in render_paths:
            if os.path.isdir(render_path):
                return render_path

        return os.path.abspath(
            os.path.join(
                os.getcwd(),
                ".cache",
                "ms-playwright",
            )
        )

    # ========================================================
    # PLAYWRIGHT RUNTIME PATH
    # ========================================================

    @staticmethod
    def _get_playwright_runtime_path() -> str:
        return os.path.abspath(
            os.path.expanduser(
                RUNTIME_PLAYWRIGHT_PATH
            )
        )

    # ========================================================
    # FIND EXECUTABLES
    # ========================================================

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
                _ = dirs

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

                        if os.path.isfile(full_path):
                            found.append(full_path)

        except Exception:
            logger.exception(
                "Не удалось просканировать "
                "Playwright browser directory."
            )

        return found

    # ========================================================
    # FIND CHROMIUM
    # ========================================================

    @staticmethod
    def _find_chromium_executable(
        browser_path: str,
    ) -> str | None:

        executables = (
            PocketMarket
            ._find_browser_executables(
                browser_path
            )
        )

        preferred = (
            "chrome",
            "chromium",
            "chrome-headless-shell",
        )

        for filename in preferred:
            for path in executables:

                if (
                    os.path.basename(path).lower()
                    == filename
                ):
                    return path

        return None

    # ========================================================
    # GET PLAYWRIGHT CHROMIUM
    # ========================================================

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

    # ========================================================
    # INSTALL CHROMIUM
    # ========================================================

    @staticmethod
    def _install_chromium(
        browser_path: str,
    ) -> None:

        logger.warning(
            "[PLAYWRIGHT] Chromium отсутствует. "
            "Устанавливаю в runtime..."
        )

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

        logger.info(
            "[PLAYWRIGHT] Install command: %s",
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
                "превысила 300 секунд."
            ) from exc

        except Exception as exc:

            raise RuntimeError(
                "Не удалось запустить установку "
                f"Chromium: {exc}"
            ) from exc

        output = (
            process.stdout or ""
        ).strip()

        if output:

            logger.info(
                "[PLAYWRIGHT] Install output:\n%s",
                output[-15000:],
            )

        if process.returncode != 0:

            raise RuntimeError(
                "Playwright Chromium не удалось "
                "установить. Код: "
                f"{process.returncode}"
            )

    # ========================================================
    # EXECUTABLE PERMISSIONS
    # ========================================================

    @staticmethod
    def _make_executable(
        path: str,
    ) -> None:

        try:

            current_mode = os.stat(
                path
            ).st_mode

            os.chmod(
                path,
                current_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH,
            )

        except Exception as exc:

            logger.warning(
                "Не удалось выставить executable "
                "permissions для %s: %s",
                path,
                exc,
            )

    # ========================================================
    # COPY CHROMIUM
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
                "Исходный Chromium executable "
                "не найден: "
                f"{source_executable}"
            )

        source_dir = os.path.dirname(
            source_executable
        )

        browser_root = source_dir

        for _ in range(8):

            base = os.path.basename(
                browser_root
            ).lower()

            if base.startswith(
                "chromium-"
            ):
                break

            parent = os.path.dirname(
                browser_root
            )

            if parent == browser_root:
                break

            browser_root = parent

        base_name = os.path.basename(
            browser_root
        )

        if not base_name.lower().startswith(
            "chromium-"
        ):
            raise RuntimeError(
                "Не удалось определить корень "
                "Chromium browser directory: "
                f"{browser_root}"
            )

        destination_root = os.path.join(
            runtime_path,
            base_name,
        )

        logger.info(
            "[PLAYWRIGHT] SOURCE: %s",
            browser_root,
        )

        logger.info(
            "[PLAYWRIGHT] DESTINATION: %s",
            destination_root,
        )

        os.makedirs(
            runtime_path,
            exist_ok=True,
        )

        # Если такая версия уже существует —
        # не копируем заново.
        existing_executable = (
            PocketMarket
            ._find_chromium_executable(
                destination_root
            )
        )

        if existing_executable:

            PocketMarket._make_executable(
                existing_executable
            )

            logger.info(
                "[PLAYWRIGHT] Chromium уже "
                "скопирован в runtime: %s",
                existing_executable,
            )

            return existing_executable

        try:

            shutil.copytree(
                browser_root,
                destination_root,
                dirs_exist_ok=True,
            )

        except Exception as exc:

            raise RuntimeError(
                "Не удалось скопировать Chromium "
                f"в /tmp: {exc}"
            ) from exc

        for root, dirs, files in os.walk(
            destination_root
        ):

            _ = dirs

            for filename in files:

                if filename in (
                    "chrome",
                    "chromium",
                    "chrome-headless-shell",
                ):

                    path = os.path.join(
                        root,
                        filename,
                    )

                    PocketMarket._make_executable(
                        path
                    )

        relative_executable = os.path.relpath(
            source_executable,
            browser_root,
        )

        runtime_executable = os.path.join(
            destination_root,
            relative_executable,
        )

        if not os.path.isfile(
            runtime_executable
        ):

            runtime_executable = (
                PocketMarket
                ._find_chromium_executable(
                    destination_root
                )
                or ""
            )

        if not runtime_executable:

            raise RuntimeError(
                "Chromium скопирован, "
                "но executable не найден."
            )

        PocketMarket._make_executable(
            runtime_executable
        )

        logger.info(
            "[PLAYWRIGHT] Runtime Chromium: %s",
            runtime_executable,
        )

        return runtime_executable

    # ========================================================
    # TEST CHROMIUM
    # ========================================================

    @staticmethod
    def _launch_test_browser(
        executable_path: str | None = None,
    ) -> None:

        from playwright.sync_api import (
            sync_playwright,
        )

        browser = None
        page = None

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
                        "--disable-blink-features=AutomationControlled",
                    ],
                }

                if executable_path:
                    launch_kwargs[
                        "executable_path"
                    ] = executable_path

                logger.info(
                    "[PLAYWRIGHT] Проверяю запуск Chromium..."
                )

                browser = (
                    pw.chromium.launch(
                        **launch_kwargs
                    )
                )

                logger.info(
                    "[PLAYWRIGHT] "
                    "Chromium успешно запущен."
                )

                page = browser.new_page()

                try:

                    page.goto(
                        "about:blank",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )

                finally:

                    try:
                        page.close()
                    except Exception:
                        pass

        except Exception as exc:

            logger.exception(
                "[PLAYWRIGHT] Chromium "
                "не смог запуститься."
            )

            raise RuntimeError(
                "Playwright Chromium установлен, "
                "но не запускается: "
                f"{exc}"
            ) from exc

        finally:

            if page is not None:

                try:
                    page.close()
                except Exception:
                    pass

            if browser is not None:

                try:
                    browser.close()
                except Exception:
                    pass

    # ========================================================
    # PREPARE PLAYWRIGHT
    # ========================================================

    @staticmethod
    def _prepare_playwright_environment() -> str:

        runtime_path = (
            PocketMarket
            ._get_playwright_runtime_path()
        )

        source_path = (
            PocketMarket
            ._get_playwright_source_path()
        )

        logger.info(
            "[PLAYWRIGHT] Source path: %s",
            source_path,
        )

        logger.info(
            "[PLAYWRIGHT] Runtime path: %s",
            runtime_path,
        )

        os.makedirs(
            runtime_path,
            exist_ok=True,
        )

        # ====================================================
        # 1. RUNTIME
        # ====================================================

        runtime_executable = (
            PocketMarket
            ._find_chromium_executable(
                runtime_path
            )
        )

        if runtime_executable:

            logger.info(
                "[PLAYWRIGHT] Найден Chromium "
                "в runtime: %s",
                runtime_executable,
            )

            PocketMarket._make_executable(
                runtime_executable
            )

            os.environ[
                "PLAYWRIGHT_BROWSERS_PATH"
            ] = runtime_path

            try:

                PocketMarket._launch_test_browser(
                    runtime_executable
                )

                logger.info(
                    "[PLAYWRIGHT] Runtime Chromium "
                    "прошёл smoke test."
                )

                return runtime_path

            except Exception as exc:

                logger.warning(
                    "[PLAYWRIGHT] Runtime Chromium "
                    "не запустился: %s",
                    exc,
                )

                # Удаляем только сломанные browser
                # директории.
                try:

                    for name in os.listdir(
                        runtime_path
                    ):

                        path = os.path.join(
                            runtime_path,
                            name,
                        )

                        if os.path.isdir(path):

                            shutil.rmtree(
                                path,
                                ignore_errors=True,
                            )

                except Exception:

                    logger.exception(
                        "[PLAYWRIGHT] "
                        "Ошибка очистки runtime."
                    )

        # ====================================================
        # 2. SOURCE CACHE
        # ====================================================

        source_executable = (
            PocketMarket
            ._find_chromium_executable(
                source_path
            )
        )

        if source_executable:

            logger.info(
                "[PLAYWRIGHT] Chromium найден "
                "в source cache: %s",
                source_executable,
            )

            runtime_executable = (
                PocketMarket
                ._copy_chromium_to_runtime(
                    source_executable,
                    runtime_path,
                )
            )

            os.environ[
                "PLAYWRIGHT_BROWSERS_PATH"
            ] = runtime_path

            logger.info(
                "[PLAYWRIGHT] "
                "PLAYWRIGHT_BROWSERS_PATH=%s",
                runtime_path,
            )

            PocketMarket._launch_test_browser(
                runtime_executable
            )

            logger.info(
                "[PLAYWRIGHT] Chromium успешно "
                "запущен из runtime."
            )

            return runtime_path

        # ====================================================
        # 3. INSTALL
        # ====================================================

        logger.warning(
            "[PLAYWRIGHT] Chromium не найден. "
            "Запускаю установку..."
        )

        PocketMarket._install_chromium(
            runtime_path
        )

        os.environ[
            "PLAYWRIGHT_BROWSERS_PATH"
        ] = runtime_path

        runtime_executable = (
            PocketMarket
            ._find_chromium_executable(
                runtime_path
            )
        )

        if not runtime_executable:

            runtime_executable = (
                PocketMarket
                ._get_chromium_executable()
            )

        if not runtime_executable:

            raise RuntimeError(
                "Chromium отсутствует "
                "после установки."
            )

        PocketMarket._make_executable(
            runtime_executable
        )

        logger.info(
            "[PLAYWRIGHT] Installed Chromium: %s",
            runtime_executable,
        )

        PocketMarket._launch_test_browser(
            runtime_executable
        )

        logger.info(
            "[PLAYWRIGHT] Chromium успешно "
            "запущен после установки."
        )

        return runtime_path

    # ========================================================
    # LOGIN PROCESS
    # ========================================================

    async def _run_login_process(
        self,
        email: str,
        password: str,
        demo: bool,
        browser_path: str,
    ) -> str:

        logger.info(
            "[AUTO LOGIN] Создаю отдельный process..."
        )

        ctx = multiprocessing.get_context(
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
                True,
                LOGIN_LIBRARY_TIMEOUT,
                browser_path,
            ),
            daemon=True,
        )

        try:

            process.start()

        except Exception as exc:

            try:
                result_queue.close()
            except Exception:
                pass

            raise RuntimeError(
                "Не удалось запустить login process: "
                f"{exc}"
            ) from exc

        logger.info(
            "[AUTO LOGIN] "
            "Login process запущен. PID=%s",
            process.pid,
        )

        loop = asyncio.get_running_loop()

        deadline = (
            loop.time()
            + AUTO_LOGIN_TIMEOUT
        )

        try:

            while True:

                if loop.time() >= deadline:

                    logger.error(
                        "[AUTO LOGIN] "
                        "❌ Login process timeout."
                    )

                    raise asyncio.TimeoutError

                try:

                    result_type, result_value = (
                        result_queue.get_nowait()
                    )

                except queue.Empty:

                    result_type = None
                    result_value = None

                if result_type == "ok":

                    ssid = str(
                        result_value
                    ).strip()

                    if not ssid:

                        raise RuntimeError(
                            "Login process "
                            "вернул пустой SSID."
                        )

                    logger.info(
                        "[AUTO LOGIN] "
                        "✅ SSID получен."
                    )

                    return ssid

                if result_type == "error":

                    raise RuntimeError(
                        str(result_value)
                    )

                if not process.is_alive():

                    exit_code = (
                        process.exitcode
                    )

                    raise RuntimeError(
                        "Login process завершился "
                        "без результата. "
                        f"Exit code: {exit_code}"
                    )

                await asyncio.sleep(
                    0.25
                )

        except asyncio.TimeoutError:

            logger.error(
                "[AUTO LOGIN] "
                "❌ HARD TIMEOUT %s сек.",
                AUTO_LOGIN_TIMEOUT,
            )

            raise

        finally:

            if process.is_alive():

                logger.warning(
                    "[AUTO LOGIN] "
                    "Останавливаю login process "
                    "PID=%s...",
                    process.pid,
                )

                if (
                    os.name == "posix"
                    and process.pid
                ):

                    try:

                        os.killpg(
                            process.pid,
                            signal.SIGTERM,
                        )

                    except Exception as exc:

                        logger.warning(
                            "[AUTO LOGIN] "
                            "Не удалось остановить "
                            "process group: %s",
                            exc,
                        )

                try:
                    process.terminate()
                except Exception:
                    pass

                try:
                    process.join(
                        timeout=3
                    )
                except Exception:
                    pass

            if process.is_alive():

                logger.error(
                    "[AUTO LOGIN] "
                    "Process всё ещё жив. kill()."
                )

                try:
                    if (
                        os.name == "posix"
                        and process.pid
                    ):
                        os.killpg(
                            process.pid,
                            signal.SIGKILL,
                        )
                except Exception:
                    pass

                try:
                    process.kill()
                except Exception:
                    pass

                try:
                    process.join(
                        timeout=2
                    )
                except Exception:
                    pass

            try:
                result_queue.cancel_join_thread()
            except Exception:
                pass

            try:
                result_queue.close()
            except Exception:
                pass

            logger.info(
                "[AUTO LOGIN] "
                "Login process cleanup завершён."
            )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> str:

        logger.info(
            "[AUTO LOGIN] ====================================="
        )

        logger.info(
            "[AUTO LOGIN] START"
        )

        logger.info(
            "[AUTO LOGIN] STEP 1/5: "
            "Проверяю PO_EMAIL..."
        )

        if not config.po_email:

            raise RuntimeError(
                "PO_EMAIL не задан."
            )

        logger.info(
            "[AUTO LOGIN] STEP 1 OK: Email задан."
        )

        logger.info(
            "[AUTO LOGIN] STEP 2/5: "
            "Проверяю PO_PASSWORD..."
        )

        if not config.po_password:

            raise RuntimeError(
                "PO_PASSWORD не задан."
            )

        logger.info(
            "[AUTO LOGIN] STEP 2 OK: Password задан."
        )

        logger.info(
            "[AUTO LOGIN] Demo: %s",
            config.po_demo,
        )

        # ====================================================
        # PLAYWRIGHT
        # ====================================================

        logger.info(
            "[AUTO LOGIN] STEP 3/5: "
            "Проверяю Playwright/Chromium..."
        )

        try:

            browser_path = await asyncio.wait_for(
                asyncio.to_thread(
                    self._prepare_playwright_environment
                ),
                timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
            )

        except asyncio.TimeoutError as exc:

            self.last_error = (
                "Подготовка Playwright "
                f"превысила timeout "
                f"{PLAYWRIGHT_PREPARE_TIMEOUT} секунд."
            )

            logger.error(
                "[AUTO LOGIN] ❌ %s",
                self.last_error,
            )

            raise RuntimeError(
                self.last_error
            ) from exc

        except Exception as exc:

            self.last_error = str(
                exc
            )

            logger.exception(
                "[AUTO LOGIN] "
                "Playwright preparation failed."
            )

            raise RuntimeError(
                "Playwright не готов для "
                "автоматического входа Pocket Option: "
                f"{exc}"
            ) from exc

        logger.info(
            "[AUTO LOGIN] STEP 3 OK"
        )

        logger.info(
            "[AUTO LOGIN] Runtime browser path: %s",
            browser_path,
        )

        # ====================================================
        # LOGIN
        # ====================================================

        logger.info(
            "[AUTO LOGIN] STEP 4/5: "
            "Подготавливаю login process..."
        )

        logger.info(
            "[AUTO LOGIN] STEP 4 OK"
        )

        logger.info(
            "[AUTO LOGIN] STEP 5/5: "
            "Запускаю вход в Pocket Option..."
        )

        try:

            ssid = await asyncio.wait_for(
                self._run_login_process(
                    email=config.po_email,
                    password=config.po_password,
                    demo=config.po_demo,
                    browser_path=browser_path,
                ),
                timeout=AUTO_LOGIN_TIMEOUT + 5,
            )

        except asyncio.TimeoutError as exc:

            self.last_error = (
                "Автоматическая авторизация "
                "Pocket Option превысила timeout."
            )

            logger.error(
                "[AUTO LOGIN] ❌ %s",
                self.last_error,
            )

            raise RuntimeError(
                self.last_error
            ) from exc

        except Exception as exc:

            error_text = str(
                exc
            )

            self.last_error = error_text

            error_lower = (
                error_text.lower()
            )

            if (
                "captcha" in error_lower
                or "recaptcha" in error_lower
            ):

                raise RuntimeError(
                    "Pocket Option потребовал "
                    "CAPTCHA/дополнительную проверку. "
                    f"Детали: {error_text}"
                ) from exc

            if (
                "etxtbsy" in error_lower
                or "text file busy" in error_lower
            ):

                raise RuntimeError(
                    "Chromium получил ETXTBSY "
                    "даже в runtime /tmp. "
                    f"Детали: {error_text}"
                ) from exc

            logger.exception(
                "[AUTO LOGIN] "
                "Pocket Option login failed."
            )

            raise RuntimeError(
                "Автоматическая авторизация "
                "Pocket Option не удалась: "
                f"{error_text}"
            ) from exc

        if not ssid:

            raise RuntimeError(
                "Pocket Option login "
                "не вернул SSID."
            )

        ssid = str(
            ssid
        ).strip()

        if not ssid:

            raise RuntimeError(
                "Pocket Option login "
                "вернул пустой SSID."
            )

        logger.info(
            "[AUTO LOGIN] "
            "✅ Pocket Option SSID получен."
        )

        return ssid

    # ========================================================
    # CREATE CLIENT
    # ========================================================

    async def _create_client(
        self,
        ssid: str,
    ) -> Any:

        logger.info(
            "[MARKET] CLIENT STEP 1/3: "
            "Импортирую PocketOptionAsync..."
        )

        try:

            from BinaryOptionsToolsV2.pocketoption import (
                PocketOptionAsync,
            )

        except Exception as exc:

            raise RuntimeError(
                "BinaryOptionsToolsV2 "
                "не импортируется: "
                f"{exc}"
            ) from exc

        logger.info(
            "[MARKET] CLIENT STEP 2/3: "
            "PocketOptionAsync импортирован."
        )

        logger.info(
            "[MARKET] CLIENT STEP 3/3: "
            "Создаю PocketOptionAsync..."
        )

        try:

            client = await asyncio.wait_for(
                asyncio.to_thread(
                    PocketOptionAsync,
                    ssid,
                ),
                timeout=CLIENT_CREATE_TIMEOUT,
            )

        except asyncio.TimeoutError as exc:

            logger.error(
                "[MARKET] ❌ PocketOptionAsync "
                "creation timeout: %s сек.",
                CLIENT_CREATE_TIMEOUT,
            )

            raise RuntimeError(
                "PocketOptionAsync завис при "
                "создании клиента. "
                f"Timeout: {CLIENT_CREATE_TIMEOUT} секунд."
            ) from exc

        except Exception as exc:

            logger.exception(
                "[MARKET] ❌ Ошибка создания "
                "PocketOptionAsync."
            )

            raise RuntimeError(
                "Не удалось создать "
                "PocketOptionAsync: "
                f"{exc}"
            ) from exc

        if client is None:

            raise RuntimeError(
                "PocketOptionAsync вернул None."
            )

        logger.info(
            "[MARKET] ✅ PocketOptionAsync "
            "клиент создан."
        )

        return client

    # ========================================================
    # SAFE METHOD
    # ========================================================

    @staticmethod
    async def _call_method(
        method: Any,
        *args: Any,
        timeout: int,
        method_name: str = "method",
    ) -> Any:

        if method is None:

            raise RuntimeError(
                f"{method_name}: method is None."
            )

        try:

            if inspect.iscoroutinefunction(
                method
            ):

                logger.debug(
                    "[MARKET] Awaiting async %s",
                    method_name,
                )

                result = method(
                    *args
                )

                if inspect.isawaitable(
                    result
                ):

                    return await asyncio.wait_for(
                        result,
                        timeout=timeout,
                    )

                return result

            logger.debug(
                "[MARKET] Running sync %s "
                "in thread",
                method_name,
            )

            result = await asyncio.wait_for(
                asyncio.to_thread(
                    method,
                    *args,
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

        except asyncio.TimeoutError as exc:

            logger.error(
                "[MARKET] %s timeout: %s сек.",
                method_name,
                timeout,
            )

            raise RuntimeError(
                f"{method_name} timeout "
                f"({timeout} секунд)."
            ) from exc

    # ========================================================
    # CONNECT
    # ========================================================

    async def _connect_impl(self) -> bool:

        logger.info(
            "[MARKET] ====================================="
        )

        logger.info(
            "[MARKET] CONNECT START"
        )

        self.connected = False
        self.last_error = None

        ssid = ""

        # =====================================================
        # STEP 1
        # =====================================================

        if config.po_ssid:

            ssid = (
                config.po_ssid.strip()
            )

            logger.info(
                "[MARKET] STEP 1/5: "
                "Использую PO_SSID."
            )

        else:

            if not config.po_auto_login:

                self.last_error = (
                    "PO_SSID не задан, "
                    "PO_AUTO_LOGIN выключен."
                )

                raise RuntimeError(
                    self.last_error
                )

            logger.info(
                "[MARKET] STEP 1/5: "
                "PO_SSID отсутствует."
            )

            logger.info(
                "[MARKET] "
                "Запускаю AUTO LOGIN..."
            )

            try:

                ssid = await asyncio.wait_for(
                    self.auto_login(),
                    timeout=AUTO_LOGIN_TIMEOUT + 15,
                )

            except asyncio.TimeoutError as exc:

                self.last_error = (
                    "Автоматический вход "
                    "Pocket Option timeout."
                )

                raise RuntimeError(
                    self.last_error
                ) from exc

            except Exception as exc:

                self.last_error = str(
                    exc
                )

                raise RuntimeError(
                    "Не удалось получить SSID "
                    "через автоматический вход: "
                    f"{exc}"
                ) from exc

        # =====================================================
        # STEP 2
        # =====================================================

        if not ssid:

            self.last_error = (
                "Не удалось получить "
                "Pocket Option SSID."
            )

            raise RuntimeError(
                self.last_error
            )

        logger.info(
            "[MARKET] STEP 2/5: "
            "✅ SSID получен."
        )

        # Никогда не выводим полный SSID в лог.
        logger.info(
            "[MARKET] SSID length: %s",
            len(ssid),
        )

        # =====================================================
        # STEP 3
        # =====================================================

        logger.info(
            "[MARKET] STEP 3/5: "
            "Создаю PocketOptionAsync..."
        )

        try:

            client = await asyncio.wait_for(
                self._create_client(
                    ssid
                ),
                timeout=CLIENT_CREATE_TIMEOUT + 5,
            )

        except Exception as exc:

            self.client = None
            self.ssid = None
            self.connected = False
            self.last_error = str(
                exc
            )

            raise

        self.client = client
        self.ssid = ssid

        logger.info(
            "[MARKET] STEP 3/5: "
            "✅ Client сохранён."
        )

        # =====================================================
        # STEP 4
        # =====================================================

        logger.info(
            "[MARKET] STEP 4/5: "
            "Ожидаю WebSocket initialization "
            "%s секунд...",
            WEBSOCKET_INIT_DELAY,
        )

        try:

            await asyncio.wait_for(
                asyncio.sleep(
                    WEBSOCKET_INIT_DELAY
                ),
                timeout=CLIENT_INIT_TIMEOUT,
            )

        except asyncio.TimeoutError as exc:

            raise RuntimeError(
                "WebSocket initialization "
                "timeout."
            ) from exc

        logger.info(
            "[MARKET] STEP 4/5: "
            "Ожидание завершено."
        )

        # =====================================================
        # STEP 5
        # =====================================================

        logger.info(
            "[MARKET] STEP 5/5: "
            "Проверяю connection health..."
        )

        balance_method = getattr(
            client,
            "balance",
            None,
        )

        if balance_method is not None:

            logger.info(
                "[MARKET] Вызываю balance()..."
            )

            try:

                balance = await self._call_method(
                    balance_method,
                    timeout=BALANCE_TIMEOUT,
                    method_name=(
                        "Pocket Option balance()"
                    ),
                )

                logger.info(
                    "[MARKET] "
                    "balance() health-check OK. "
                    "type=%s",
                    type(balance).__name__,
                )

            except Exception as exc:

                logger.warning(
                    "[MARKET] "
                    "balance() health-check "
                    "не прошёл: %s",
                    exc,
                )

                # Не считаем balance критическим,
                # если сам client существует.

        else:

            logger.warning(
                "[MARKET] "
                "balance() отсутствует. "
                "Продолжаю."
            )

        self.connected = True

        self.last_success = (
            datetime.now(
                timezone.utc
            )
        )

        self.last_error = None

        logger.info(
            "[MARKET] ====================================="
        )

        logger.info(
            "[MARKET] ✅ POCKET OPTION CONNECTED"
        )

        logger.info(
            "[MARKET] Demo: %s",
            config.po_demo,
        )

        logger.info(
            "[MARKET] ====================================="
        )

        return True

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self) -> bool:

        if self.is_connected:

            logger.info(
                "[MARKET] Уже подключён."
            )

            return True

        async with self.lock:

            if self.is_connected:

                logger.info(
                    "[MARKET] Уже подключён "
                    "после ожидания lock."
                )

                return True

            try:

                return await asyncio.wait_for(
                    self._connect_impl(),
                    timeout=CONNECT_HARD_TIMEOUT,
                )

            except asyncio.TimeoutError as exc:

                self.connected = False

                self.last_error = (
                    "Полное подключение Pocket Option "
                    f"превысило {CONNECT_HARD_TIMEOUT} секунд."
                )

                logger.error(
                    "[MARKET] ❌ CONNECT HARD TIMEOUT: %s",
                    self.last_error,
                )

                raise RuntimeError(
                    self.last_error
                ) from exc

            except asyncio.CancelledError:

                logger.warning(
                    "[MARKET] Connect cancelled."
                )

                raise

            except Exception as exc:

                self.connected = False
                self.last_error = str(
                    exc
                )

                logger.exception(
                    "[MARKET] ❌ CONNECT FAILED: %s",
                    exc,
                )

                raise

    # ========================================================
    # RECONNECT
    # ========================================================

    async def reconnect(self) -> bool:

        logger.warning(
            "[MARKET] Переподключение..."
        )

        await self.close()

        await asyncio.sleep(
            1
        )

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
    # VALUE
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

        candle = cls._parse_candle(
            raw
        )

        return (
            [candle]
            if candle is not None
            else []
        )

    # ========================================================
    # NORMALIZE
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

        result = list(
            unique.values()
        )

        result.sort(
            key=lambda c: c.time
        )

        return result

    # ========================================================
    # REQUEST RAW CANDLES
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

        logger.info(
            "[MARKET] CANDLES STEP 1: "
            "Ищу метод получения свечей..."
        )

        methods = (
            "get_candles",
            "candles",
            "get_candle",
            "history",
            "get_history",
        )

        last_error: Exception | None = None

        for method_name in methods:

            method = getattr(
                self.client,
                method_name,
                None,
            )

            if method is None:
                continue

            logger.info(
                "[MARKET] Найден метод: %s",
                method_name,
            )

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

                    logger.info(
                        "[MARKET] "
                        "Вызываю %s args=%s",
                        method_name,
                        args,
                    )

                    result = await self._call_method(
                        method,
                        *args,
                        timeout=CANDLE_REQUEST_TIMEOUT,
                        method_name=(
                            f"{method_name}{args}"
                        ),
                    )

                    if result is not None:

                        logger.info(
                            "[MARKET] "
                            "%s вернул данные.",
                            method_name,
                        )

                        return result

                    logger.warning(
                        "[MARKET] "
                        "%s вернул None.",
                        method_name,
                    )

                except asyncio.CancelledError:

                    raise

                except TypeError as exc:

                    last_error = exc

                    logger.debug(
                        "[MARKET] "
                        "%s args=%s TypeError: %s",
                        method_name,
                        args,
                        exc,
                    )

                    continue

                except Exception as exc:

                    last_error = exc

                    logger.warning(
                        "[MARKET] "
                        "%s failed: %s",
                        method_name,
                        exc,
                    )

                    continue

        if last_error is not None:

            raise RuntimeError(
                "Не удалось получить свечи "
                "через BinaryOptionsToolsV2: "
                f"{last_error}"
            ) from last_error

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

        logger.info(
            "[MARKET] ====================================="
        )

        logger.info(
            "[MARKET] GET CANDLES START: "
            "asset=%s period=%s count=%s",
            asset,
            period,
            count,
        )

        if not self.is_connected:

            logger.info(
                "[MARKET] Client не подключён. "
                "Запускаю connect()..."
            )

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

            logger.info(
                "[MARKET] CANDLES STEP 2: "
                "Запрашиваю raw candles..."
            )

            raw = await asyncio.wait_for(
                self._request_raw_candles(
                    asset=asset,
                    period=period,
                    count=count,
                ),
                timeout=CANDLE_REQUEST_TIMEOUT * 2,
            )

            logger.info(
                "[MARKET] CANDLES STEP 3: "
                "Парсю ответ..."
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

            logger.info(
                "[MARKET] CANDLES STEP 4: "
                "Получено свечей: %s",
                len(candles),
            )

            if candles:

                self.last_success = (
                    datetime.now(
                        timezone.utc
                    )
                )

                self.last_error = None

                logger.info(
                    "[MARKET] "
                    "✅ CANDLES SUCCESS: "
                    "%s candles for %s",
                    len(candles),
                    asset,
                )

            else:

                logger.warning(
                    "[MARKET] "
                    "⚠️ CANDLES EMPTY: %s",
                    asset,
                )

            return candles

        except asyncio.CancelledError:

            raise

        except asyncio.TimeoutError as exc:

            self.last_error = (
                f"Получение свечей {asset} "
                "превысило timeout."
            )

            logger.error(
                "[MARKET] ❌ Candle request timeout: %s",
                asset,
            )

            raise RuntimeError(
                self.last_error
            ) from exc

        except Exception as exc:

            self.last_error = str(
                exc
            )

            logger.exception(
                "[MARKET] ❌ Ошибка получения "
                "свечей %s: %s",
                asset,
                exc,
            )

            raise

    # ========================================================
    # COMPATIBILITY METHOD
    # ========================================================

    async def candles(
        self,
        asset: str,
        period: int = 60,
        count: int = 100,
    ) -> list[Candle]:

        """
        Совместимость с main.py.

        Некоторые версии main.py используют:
            market.candles(...)

        Основной метод:
            get_candles(...)
        """

        logger.info(
            "[MARKET] candles() compatibility "
            "wrapper: %s period=%s count=%s",
            asset,
            period,
            count,
        )

        try:

            return await asyncio.wait_for(
                self.get_candles(
                    asset=asset,
                    period=period,
                    count=count,
                ),
                timeout=CANDLES_HARD_TIMEOUT,
            )

        except asyncio.TimeoutError as exc:

            self.last_error = (
                f"market.candles() timeout "
                f"({CANDLES_HARD_TIMEOUT} сек.)"
            )

            logger.error(
                "[MARKET] ❌ %s",
                self.last_error,
            )

            raise RuntimeError(
                self.last_error
            ) from exc

    # ========================================================
    # CANDLE DATA
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

        logger.info(
            "[MARKET TEST] ====================================="
        )

        try:

            logger.info(
                "[MARKET TEST] "
                "Проверка рынка %s...",
                asset,
            )

            if not self.is_connected:

                market_connect_timeout = (
                    AUTO_LOGIN_TIMEOUT
                    + CONNECT_TIMEOUT
                    + BALANCE_TIMEOUT
                    + MARKET_TEST_CONNECT_EXTRA
                )

                logger.info(
                    "[MARKET TEST] "
                    "Connect timeout: %s сек.",
                    market_connect_timeout,
                )

                await asyncio.wait_for(
                    self.connect(),
                    timeout=market_connect_timeout,
                )

            candles = await asyncio.wait_for(
                self.get_candles(
                    asset=asset,
                    period=period,
                    count=count,
                ),
                timeout=CANDLE_REQUEST_TIMEOUT * 3,
            )

            if not candles:

                logger.warning(
                    "[MARKET TEST] "
                    "Свечи не получены."
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
                    "[MARKET TEST] "
                    "Обнаружены некорректные свечи."
                )

                return False

            logger.info(
                "[MARKET TEST] OK: "
                "%s candles for %s",
                len(candles),
                asset,
            )

            return True

        except asyncio.CancelledError:

            raise

        except asyncio.TimeoutError:

            self.last_error = (
                "Проверка рынка "
                "превысила timeout."
            )

            logger.error(
                "[MARKET TEST] TIMEOUT."
            )

            return False

        except Exception as exc:

            self.last_error = str(
                exc
            )

            logger.exception(
                "[MARKET TEST] Failed: %s",
                exc,
            )

            return False

    # ========================================================
    # STATUS
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

                logger.info(
                    "[MARKET] close(): "
                    "client отсутствует."
                )

                return

            logger.info(
                "[MARKET] Начинаю закрытие client..."
            )

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

                    logger.info(
                        "[MARKET] Закрываю client "
                        "через %s()...",
                        method_name,
                    )

                    if inspect.iscoroutinefunction(
                        method
                    ):

                        result = method()

                        if inspect.isawaitable(
                            result
                        ):

                            await asyncio.wait_for(
                                result,
                                timeout=CLIENT_CLOSE_TIMEOUT,
                            )

                    else:

                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                method
                            ),
                            timeout=CLIENT_CLOSE_TIMEOUT,
                        )

                        if inspect.isawaitable(
                            result
                        ):

                            await asyncio.wait_for(
                                result,
                                timeout=CLIENT_CLOSE_TIMEOUT,
                            )

                    logger.info(
                        "[MARKET] Client closed "
                        "using %s().",
                        method_name,
                    )

                    break

                except asyncio.CancelledError:

                    raise

                except Exception as exc:

                    logger.warning(
                        "[MARKET] Ошибка %s(): %s",
                        method_name,
                        exc,
                    )

            self.last_error = None

            logger.info(
                "[MARKET] Client cleanup завершён."
            )


# ============================================================
# GLOBAL INSTANCE
# ============================================================

market = PocketMarket()
