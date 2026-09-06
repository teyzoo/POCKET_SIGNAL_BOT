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

CONNECT_TIMEOUT = 30
AUTO_LOGIN_TIMEOUT = 120
BALANCE_TIMEOUT = 15
CANDLE_REQUEST_TIMEOUT = 20
CLIENT_CLOSE_TIMEOUT = 5
WEBSOCKET_INIT_DELAY = 5

# Подготовка Playwright теперь обычно быстрая,
# потому что браузер переносится в /tmp.
PLAYWRIGHT_PREPARE_TIMEOUT = 120

LOGIN_LIBRARY_TIMEOUT = 90

MARKET_TEST_CONNECT_EXTRA = 30

# Runtime directory.
#
# НЕ используем напрямую:
# /opt/render/project/src/.cache/ms-playwright
#
# Потому что на Render там иногда возникает:
# ETXTBSY = Text file busy
#
# Вместо этого Chromium копируется в /tmp.
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
# AUTO LOGIN PROCESS WORKER
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
    Авторизация Pocket Option в отдельном процессе.

    Важно:
    asyncio.wait_for() не может физически убить зависший
    Python thread.

    Поэтому login() запускается в отдельном process.
    """

    try:
        # В дочернем процессе обязательно устанавливаем
        # тот же runtime browser path.
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browser_path

        # На Linux создаём отдельную process group.
        # Это помогает корректно завершать дочерний
        # браузер Playwright вместе с worker.
        if os.name == "posix":
            try:
                os.setsid()
            except Exception:
                pass

        worker_logger = logging.getLogger(
            "pocket_market.login_worker"
        )

        worker_logger.info(
            "[LOGIN WORKER] PLAYWRIGHT_BROWSERS_PATH=%s",
            browser_path,
        )

        worker_logger.info(
            "[LOGIN WORKER] Импортирую BinaryOptionsToolsV2..."
        )

        from BinaryOptionsToolsV2.pocketoption.tools.login import (
            login,
        )

        worker_logger.info(
            "[LOGIN WORKER] Запускаю Pocket Option login..."
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
                (
                    "ok",
                    str(ssid),
                )
            )
        else:
            result_queue.put(
                (
                    "error",
                    "login() вернул пустой SSID.",
                )
            )

    except BaseException as exc:
        try:
            result_queue.put(
                (
                    "error",
                    f"{type(exc).__name__}: {exc}",
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
        Находит исходную директорию Playwright.

        Приоритет:

        1. POCKET_PLAYWRIGHT_SOURCE_PATH
        2. старый PLAYWRIGHT_BROWSERS_PATH,
           если он не указывает на runtime /tmp
        3. Render .cache/ms-playwright
        4. локальный .cache/ms-playwright
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

        if configured_path:
            configured_path = os.path.abspath(
                os.path.expanduser(
                    configured_path
                )
            )

            runtime_path = os.path.abspath(
                RUNTIME_PLAYWRIGHT_PATH
            )

            if configured_path != runtime_path:
                return configured_path

        render_path = (
            "/opt/render/project/src/"
            ".cache/ms-playwright"
        )

        if os.path.isdir(
            render_path
        ):
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
    # FIND BROWSER EXECUTABLES
    # ========================================================

    @staticmethod
    def _find_browser_executables(
        browser_path: str,
    ) -> list[str]:

        found: list[str] = []

        if not os.path.isdir(
            browser_path
        ):
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

        for path in executables:

            filename = os.path.basename(
                path
            ).lower()

            if filename in (
                "chrome",
                "chromium",
            ):
                return path

        return None

    # ========================================================
    # GET PLAYWRIGHT CHROMIUM PATH
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
            "Playwright Chromium отсутствует. "
            "Запускаю установку Chromium в runtime..."
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
                f"Chromium: {exc}"
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

    # ========================================================
    # MAKE EXECUTABLE
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
    # COPY CHROMIUM TO /tmp
    # ========================================================

    @staticmethod
    def _copy_chromium_to_runtime(
        source_executable: str,
        runtime_path: str,
    ) -> str:
        """
        Копирует ВСЮ папку chromium-* из Render cache
        в /tmp.

        Нельзя копировать только chrome:
        Chromium требует дополнительные ресурсы рядом
        с executable.
        """

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

        # Обычно:
        #
        # .../ms-playwright/
        #   chromium-1234/
        #       chrome-linux64/
        #           chrome
        #
        # Нам нужно скопировать chromium-1234 целиком.

        browser_root = source_dir

        for _ in range(4):

            parent = os.path.dirname(
                browser_root
            )

            if not parent:
                break

            base = os.path.basename(
                browser_root
            ).lower()

            if base.startswith(
                "chromium-"
            ):
                break

            browser_root = parent

        base_name = os.path.basename(
            browser_root
        )

        if not base_name.lower().startswith(
            "chromium-"
        ):

            # Fallback: копируем chrome-linux64
            # в runtime.
            browser_root = source_dir
            base_name = os.path.basename(
                browser_root
            )

        destination_root = os.path.join(
            runtime_path,
            base_name,
        )

        logger.info(
            "[PLAYWRIGHT] Копирую Chromium:"
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

        # Если уже есть предыдущая копия,
        # удаляем её и создаём чистую.
        if os.path.exists(
            destination_root
        ):

            try:

                shutil.rmtree(
                    destination_root
                )

            except Exception as exc:

                raise RuntimeError(
                    "Не удалось очистить старую "
                    "runtime-копию Chromium: "
                    f"{exc}"
                ) from exc

        try:

            shutil.copytree(
                browser_root,
                destination_root,
            )

        except Exception as exc:

            raise RuntimeError(
                "Не удалось скопировать Chromium "
                f"в /tmp: {exc}"
            ) from exc

        # Исправляем права на все executable-файлы.
        for root, dirs, files in os.walk(
            destination_root
        ):

            _ = dirs

            for filename in files:

                path = os.path.join(
                    root,
                    filename,
                )

                if filename in (
                    "chrome",
                    "chrome-headless-shell",
                    "chromium",
                ):

                    PocketMarket._make_executable(
                        path
                    )

        runtime_executable = os.path.join(
            destination_root,
            os.path.relpath(
                source_executable,
                browser_root,
            ),
        )

        if not os.path.isfile(
            runtime_executable
        ):

            # Fallback search.
            runtime_executable = (
                PocketMarket
                ._find_chromium_executable(
                    runtime_path
                )
            ) or ""

        if not runtime_executable:

            raise RuntimeError(
                "Chromium скопирован в /tmp, "
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
    # SAFE CHROMIUM TEST
    # ========================================================

    @staticmethod
    def _launch_test_browser(
        executable_path: str | None = None,
    ) -> None:
        """
        Проверяет реальный запуск Chromium.

        Для Render используются:
        - no-sandbox
        - disable-dev-shm-usage
        - disable-gpu
        - no-zygote
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
                    "[PLAYWRIGHT] Chromium "
                    "успешно запущен."
                )

                # Небольшой реальный smoke test.
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

    # ========================================================
    # PREPARE PLAYWRIGHT
    # ========================================================

    @staticmethod
    def _prepare_playwright_environment() -> str:
        """
        Главная подготовка Playwright.

        Ключевая схема:

        Render cache
             ↓
        Chromium executable
             ↓
        копия ВСЕЙ папки chromium-* 
             ↓
        /tmp/pocket-option-ms-playwright
             ↓
        PLAYWRIGHT_BROWSERS_PATH=/tmp/...
             ↓
        login()
        """

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
        # УЖЕ РАБОЧИЙ RUNTIME
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
                    executable_path=runtime_executable
                )

                logger.info(
                    "[PLAYWRIGHT] Runtime Chromium "
                    "успешно прошёл smoke test."
                )

                return runtime_path

            except Exception as exc:

                logger.warning(
                    "[PLAYWRIGHT] Runtime Chromium "
                    "не запустился: %s",
                    exc,
                )

                # Удаляем битую runtime-копию.
                try:

                    for name in os.listdir(
                        runtime_path
                    ):

                        path = os.path.join(
                            runtime_path,
                            name,
                        )

                        if os.path.isdir(
                            path
                        ):

                            shutil.rmtree(
                                path,
                                ignore_errors=True,
                            )

                        elif os.path.isfile(
                            path
                        ):

                            try:
                                os.remove(path)
                            except Exception:
                                pass

                except Exception:

                    logger.exception(
                        "Ошибка очистки runtime "
                        "Playwright directory."
                    )

        # ====================================================
        # ИЩЕМ CHROMIUM В SOURCE
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
                "в Render cache: %s",
                source_executable,
            )

            # =================================================
            # КРИТИЧЕСКИЙ FIX ETXTBSY
            # =================================================

            runtime_executable = (
                PocketMarket
                ._copy_chromium_to_runtime(
                    source_executable=source_executable,
                    runtime_path=runtime_path,
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

            try:

                PocketMarket._launch_test_browser(
                    executable_path=runtime_executable
                )

            except Exception as exc:

                logger.warning(
                    "[PLAYWRIGHT] "
                    "Скопированный Chromium "
                    "не запустился: %s",
                    exc,
                )

                # Попробуем Playwright's own executable
                # path из runtime.
                runtime_executable_2 = (
                    PocketMarket
                    ._get_chromium_executable()
                )

                if (
                    runtime_executable_2
                    and os.path.isfile(
                        runtime_executable_2
                    )
                ):

                    PocketMarket._make_executable(
                        runtime_executable_2
                    )

                    PocketMarket._launch_test_browser(
                        executable_path=runtime_executable_2
                    )

                    return runtime_path

                raise

            logger.info(
                "[PLAYWRIGHT] Chromium успешно "
                "запущен из /tmp."
            )

            return runtime_path

        # ====================================================
        # CHROMIUM НЕ НАЙДЕН — INSTALL В /tmp
        # ====================================================

        logger.warning(
            "[PLAYWRIGHT] Chromium не найден "
            "ни в runtime, ни в source cache."
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
                "Playwright Chromium отсутствует "
                "после установки."
            )

        if not os.path.isfile(
            runtime_executable
        ):

            raise RuntimeError(
                "Chromium executable не найден: "
                f"{runtime_executable}"
            )

        PocketMarket._make_executable(
            runtime_executable
        )

        logger.info(
            "[PLAYWRIGHT] Installed Chromium: %s",
            runtime_executable,
        )

        PocketMarket._launch_test_browser(
            executable_path=runtime_executable
        )

        return runtime_path

    # ========================================================
    # RUN LOGIN PROCESS
    # ========================================================

    async def _run_login_process(
        self,
        email: str,
        password: str,
        demo: bool,
        browser_path: str,
    ) -> str:

        logger.info(
            "[AUTO LOGIN] Создаю отдельный process "
            "для Pocket Option login..."
        )

        try:

            ctx = multiprocessing.get_context(
                "spawn"
            )

        except Exception as exc:

            raise RuntimeError(
                "Не удалось создать multiprocessing "
                "context: "
                f"{exc}"
            ) from exc

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
            "[AUTO LOGIN] Login process started. PID=%s",
            process.pid,
        )

        loop = asyncio.get_running_loop()

        deadline = (
            loop.time()
            + AUTO_LOGIN_TIMEOUT
        )

        try:

            while loop.time() < deadline:

                # ---------------------------------------------
                # CHECK RESULT
                # ---------------------------------------------

                try:

                    result_type, result_value = (
                        result_queue.get_nowait()
                    )

                except queue.Empty:

                    result_type = None
                    result_value = None

                if result_type == "ok":

                    logger.info(
                        "[AUTO LOGIN] Login process "
                        "вернул SSID."
                    )

                    return str(
                        result_value
                    ).strip()

                if result_type == "error":

                    raise RuntimeError(
                        str(
                            result_value
                        )
                    )

                # ---------------------------------------------
                # PROCESS EXITED
                # ---------------------------------------------

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

            # ---------------------------------------------
            # HARD TIMEOUT
            # ---------------------------------------------

            logger.error(
                "[AUTO LOGIN] Login process "
                "превысил hard timeout %s секунд.",
                AUTO_LOGIN_TIMEOUT,
            )

            raise asyncio.TimeoutError

        finally:

            if process.is_alive():

                logger.warning(
                    "[AUTO LOGIN] Завершаю зависший "
                    "login process PID=%s...",
                    process.pid,
                )

                # На Linux пытаемся завершить всю process group,
                # включая Chromium, запущенный Playwright.
                if (
                    os.name == "posix"
                    and process.pid
                ):

                    try:

                        import signal

                        os.killpg(
                            process.pid,
                            signal.SIGTERM,
                        )

                    except Exception as exc:

                        logger.warning(
                            "[AUTO LOGIN] "
                            "Не удалось завершить "
                            "process group: %s",
                            exc,
                        )

                try:

                    process.terminate()

                except Exception:

                    logger.exception(
                        "Ошибка terminate login process."
                    )

                try:

                    process.join(
                        timeout=3
                    )

                except Exception:

                    logger.exception(
                        "Ошибка ожидания завершения "
                        "login process."
                    )

                if process.is_alive():

                    logger.error(
                        "[AUTO LOGIN] Login process "
                        "не завершился после terminate()."
                    )

                    try:

                        process.kill()

                    except Exception:

                        logger.exception(
                            "Ошибка kill login process."
                        )

                    try:

                        process.join(
                            timeout=2
                        )

                    except Exception:

                        pass

            else:

                try:

                    process.join(
                        timeout=1
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

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(self) -> str:

        logger.info(
            "[AUTO LOGIN] STEP 1/5: "
            "Проверяю PO_EMAIL..."
        )

        if not config.po_email:

            raise RuntimeError(
                "PO_EMAIL не задан."
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
            "[AUTO LOGIN] Email задан."
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

        browser_path: str

        try:

            browser_path = await asyncio.wait_for(
                asyncio.to_thread(
                    self._prepare_playwright_environment
                ),
                timeout=PLAYWRIGHT_PREPARE_TIMEOUT,
            )

        except asyncio.CancelledError:

            raise

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
                "[AUTO LOGIN] ❌ "
                "Playwright preparation failed."
            )

            raise RuntimeError(
                "Playwright не готов для "
                "автоматического входа Pocket Option: "
                f"{exc}"
            ) from exc

        logger.info(
            "[AUTO LOGIN] Runtime browser path: %s",
            browser_path,
        )

        # ====================================================
        # LOGIN
        # ====================================================

        logger.info(
            "[AUTO LOGIN] STEP 4/5: "
            "Подготавливаю отдельный login process..."
        )

        logger.info(
            "[AUTO LOGIN] Backend: playwright"
        )

        logger.info(
            "[AUTO LOGIN] Library timeout: %s сек.",
            LOGIN_LIBRARY_TIMEOUT,
        )

        logger.info(
            "[AUTO LOGIN] Hard timeout: %s сек.",
            AUTO_LOGIN_TIMEOUT,
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

        except asyncio.CancelledError:

            raise

        except asyncio.TimeoutError as exc:

            self.last_error = (
                "Автоматическая авторизация "
                "Pocket Option превысила timeout "
                f"{AUTO_LOGIN_TIMEOUT} секунд."
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

            error_lower = (
                error_text.lower()
            )

            self.last_error = error_text

            # =================================================
            # CAPTCHA
            # =================================================

            if (
                "captcha" in error_lower
                or "recaptcha" in error_lower
            ):

                logger.error(
                    "[AUTO LOGIN] ❌ CAPTCHA/RECAPTCHA."
                )

                raise RuntimeError(
                    "Pocket Option потребовал "
                    "CAPTCHA/дополнительную проверку. "
                    "Автоматический вход остановлен. "
                    f"Детали: {error_text}"
                ) from exc

            # =================================================
            # ETXTBSY
            # =================================================

            if (
                "etxtbsy" in error_lower
                or "text file busy" in error_lower
            ):

                logger.error(
                    "[AUTO LOGIN] ❌ Chromium ETXTBSY "
                    "даже после переноса в /tmp."
                )

                raise RuntimeError(
                    "Chromium получил ETXTBSY даже "
                    "в runtime /tmp. "
                    f"Детали: {error_text}"
                ) from exc

            # =================================================
            # BROWSER
            # =================================================

            browser_errors = (
                "chromium distribution",
                "chrome is not found",
                "browser executable",
                "executable doesn't exist",
                "executable doesn't exist at",
                "browser_type.launch",
                "failed to launch",
                "target page",
                "browser closed",
            )

            if any(
                item in error_lower
                for item in browser_errors
            ):

                logger.error(
                    "[AUTO LOGIN] ❌ "
                    "Ошибка браузера: %s",
                    error_text,
                )

                raise RuntimeError(
                    "Playwright не смог запустить "
                    "Chromium для Pocket Option. "
                    f"Детали: {error_text}"
                ) from exc

            # =================================================
            # NETWORK
            # =================================================

            network_errors = (
                "firewall",
                "network",
                "connection",
                "timed out",
                "timeout",
                "net::",
                "dns",
                "connection refused",
                "connection reset",
            )

            if any(
                item in error_lower
                for item in network_errors
            ):

                logger.error(
                    "[AUTO LOGIN] ❌ "
                    "Сетевая ошибка: %s",
                    error_text,
                )

                raise RuntimeError(
                    "Pocket Option недоступен "
                    "из окружения Render или "
                    "соединение завершилось "
                    "по timeout. "
                    f"Детали: {error_text}"
                ) from exc

            logger.exception(
                "[AUTO LOGIN] ❌ "
                "Pocket Option automatic login failed."
            )

            raise RuntimeError(
                "Автоматическая авторизация "
                "Pocket Option не удалась: "
                f"{error_text}"
            ) from exc

        # ====================================================
        # SSID VALIDATION
        # ====================================================

        if not ssid:

            self.last_error = (
                "Pocket Option login "
                "не вернул SSID."
            )

            raise RuntimeError(
                self.last_error
            )

        ssid = str(
            ssid
        ).strip()

        if not ssid:

            self.last_error = (
                "Pocket Option login "
                "вернул пустой SSID."
            )

            raise RuntimeError(
                self.last_error
            )

        logger.info(
            "[AUTO LOGIN] ✅ "
            "Pocket Option SSID успешно получен."
        )

        return ssid

    # ========================================================
    # SAFE CLIENT CREATION
    # ========================================================

    async def _create_client(
        self,
        ssid: str,
    ) -> Any:

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
            "[MARKET] Создание PocketOptionAsync "
            "в отдельном thread..."
        )

        try:

            client = await asyncio.wait_for(
                asyncio.to_thread(
                    PocketOptionAsync,
                    ssid,
                ),
                timeout=CONNECT_TIMEOUT,
            )

        except asyncio.TimeoutError as exc:

            logger.error(
                "[MARKET] ❌ PocketOptionAsync "
                "creation timeout: %s сек.",
                CONNECT_TIMEOUT,
            )

            raise RuntimeError(
                "PocketOptionAsync завис при создании "
                f"клиента. Timeout: "
                f"{CONNECT_TIMEOUT} секунд."
            ) from exc

        except asyncio.CancelledError:

            raise

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

        return client

    # ========================================================
    # SAFE METHOD CALL
    # ========================================================

    @staticmethod
    async def _call_method(
        method: Any,
        *args: Any,
        timeout: int,
        method_name: str = "method",
    ) -> Any:

        try:

            if inspect.iscoroutinefunction(
                method
            ):

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

    async def connect(self) -> bool:

        async with self.lock:

            if (
                self.client is not None
                and self.connected
            ):

                logger.info(
                    "[MARKET] Уже подключён."
                )

                return True

            self.connected = False
            self.last_error = None

            ssid = ""

            # =================================================
            # STEP 1
            # =================================================

            if config.po_ssid:

                ssid = (
                    config.po_ssid.strip()
                )

                logger.info(
                    "[MARKET] STEP 1/5: "
                    "Использую PO_SSID из Render."
                )

            else:

                if not config.po_auto_login:

                    self.last_error = (
                        "PO_SSID не задан, а "
                        "PO_AUTO_LOGIN выключен."
                    )

                    raise RuntimeError(
                        self.last_error
                    )

                logger.warning(
                    "[MARKET] STEP 1/5: "
                    "PO_SSID отсутствует."
                )

                try:

                    ssid = await asyncio.wait_for(
                        self.auto_login(),
                        timeout=AUTO_LOGIN_TIMEOUT + 15,
                    )

                except asyncio.CancelledError:

                    raise

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

            # =================================================
            # STEP 2
            # =================================================

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
                "SSID получен."
            )

            # =================================================
            # STEP 3
            # =================================================

            logger.info(
                "[MARKET] STEP 3/5: "
                "Создаю PocketOptionAsync..."
            )

            try:

                client = await self._create_client(
                    ssid
                )

            except asyncio.CancelledError:

                raise

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
                "[MARKET] PocketOptionAsync "
                "клиент создан."
            )

            # =================================================
            # STEP 4
            # =================================================

            logger.info(
                "[MARKET] STEP 4/5: "
                "Ожидание инициализации "
                "Pocket Option WebSocket "
                "(%s сек.)...",
                WEBSOCKET_INIT_DELAY,
            )

            await asyncio.sleep(
                WEBSOCKET_INIT_DELAY
            )

            # =================================================
            # STEP 5
            # =================================================

            logger.info(
                "[MARKET] STEP 5/5: "
                "Проверяю соединение..."
            )

            balance_method = getattr(
                client,
                "balance",
                None,
            )

            if balance_method is not None:

                logger.info(
                    "[MARKET] Выполняю "
                    "balance() health-check..."
                )

                try:

                    await self._call_method(
                        balance_method,
                        timeout=BALANCE_TIMEOUT,
                        method_name=(
                            "Pocket Option balance()"
                        ),
                    )

                    logger.info(
                        "[MARKET] "
                        "Pocket Option connection "
                        "health-check OK."
                    )

                except asyncio.CancelledError:

                    raise

                except Exception as health_exc:

                    logger.warning(
                        "[MARKET] Pocket Option "
                        "balance health-check "
                        "не прошёл: %s",
                        health_exc,
                    )

            else:

                logger.warning(
                    "[MARKET] PocketOptionAsync "
                    "не содержит balance(). "
                    "Продолжаю."
                )

            # =================================================
            # CONNECTED
            # =================================================

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
                "[MARKET] ✅ POCKET OPTION CONNECTED"
            )

            logger.info(
                "[MARKET] Demo: %s",
                config.po_demo,
            )

            logger.info(
                "=============================================="
            )

            return True

    # ========================================================
    # RECONNECT
    # ========================================================

    async def reconnect(self) -> bool:

        logger.warning(
            "Переподключение к Pocket Option..."
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

        last_error: Exception | None = None

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

                    result = await self._call_method(
                        method,
                        *args,
                        timeout=CANDLE_REQUEST_TIMEOUT,
                        method_name=(
                            f"{method_name}{args}"
                        ),
                    )

                    if result is not None:

                        return result

                except asyncio.CancelledError:

                    raise

                except TypeError as exc:

                    last_error = exc
                    continue

                except Exception as exc:

                    last_error = exc

                    logger.warning(
                        "Market method %s failed: %s",
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

            raw = await asyncio.wait_for(
                self._request_raw_candles(
                    asset=asset,
                    period=period,
                    count=count,
                ),
                timeout=(
                    CANDLE_REQUEST_TIMEOUT * 2
                ),
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

        except asyncio.TimeoutError as exc:

            self.last_error = (
                f"Получение свечей {asset} "
                "превысило timeout."
            )

            logger.error(
                "[MARKET] Candle request timeout: %s",
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
                    "Expected connect timeout: "
                    "%s сек.",
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
                timeout=(
                    CANDLE_REQUEST_TIMEOUT * 3
                ),
            )

            if not candles:

                logger.warning(
                    "Market test: "
                    "свечи не получены."
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

        except asyncio.TimeoutError as exc:

            self.last_error = (
                "Проверка рынка "
                "превысила timeout."
            )

            logger.error(
                "[MARKET TEST] TIMEOUT: %s",
                exc,
            )

            return False

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

                    logger.info(
                        "Закрываю Pocket Option "
                        "client через %s()...",
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
                        "Pocket Option client "
                        "closed using %s().",
                        method_name,
                    )

                    break

                except asyncio.CancelledError:

                    raise

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
