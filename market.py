from __future__ import annotations

import asyncio
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
# CONSTANTS
# ============================================================

LOGIN_URL = "https://pocketoption.com/en/login/"

CONNECT_TIMEOUT = 90
AUTO_LOGIN_TIMEOUT = 150
BALANCE_TIMEOUT = 30
CANDLE_REQUEST_TIMEOUT = 30
CLIENT_CLOSE_TIMEOUT = 10
PLAYWRIGHT_PREPARE_TIMEOUT = 60

WEBSOCKET_INIT_DELAY = 5

RUNTIME_PLAYWRIGHT_PATH = (
    "/tmp/pocket-option-ms-playwright"
)


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
# PLAYWRIGHT SOURCES
# ============================================================

def _find_browser_executable(
    base_paths: list[Path],
) -> Optional[str]:

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
                        continue

        except Exception:

            logger.exception(
                "[PLAYWRIGHT] "
                "Ошибка поиска браузера: %s",
                base,
            )

    return None


def _get_playwright_sources() -> list[Path]:

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

    browser = _find_browser_executable(
        [runtime]
    )

    if browser:

        logger.info(
            "[PLAYWRIGHT] "
            "Chromium уже есть в runtime: %s",
            browser,
        )

        return browser

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
                "[PLAYWRIGHT] "
                "Chromium не найден: %s",
                source,
            )

            continue

        logger.info(
            "[PLAYWRIGHT] "
            "Найден Chromium в source: %s",
            browser,
        )

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

            runtime_browser = (
                _find_browser_executable(
                    [runtime]
                )
            )

            if runtime_browser:

                logger.info(
                    "[PLAYWRIGHT] "
                    "Runtime Chromium: %s",
                    runtime_browser,
                )

                return runtime_browser

        except Exception:

            logger.exception(
                "[PLAYWRIGHT] "
                "Ошибка копирования Chromium."
            )

            return browser

    logger.error(
        "[PLAYWRIGHT] "
        "Рабочий Chromium не найден."
    )

    return None


# ============================================================
# COOKIE -> SSID
# ============================================================

def _build_ssid_from_cookie(
    session_value: str,
    demo: bool,
    uid: int = 0,
) -> str:

    is_demo = 1 if demo else 0

    return (
        '42["auth",'
        '{"session":"'
        + session_value
        + '",'
        '"isDemo":'
        + str(is_demo)
        + ","
        '"uid":'
        + str(uid)
        + ","
        '"platform":2'
        "}]"
    )


# ============================================================
# CUSTOM PLAYWRIGHT LOGIN
# ============================================================

def _browser_login_sync(
    email: str,
    password: str,
    browser_executable: str,
    demo: bool,
) -> Optional[str]:

    logger.info(
        "[AUTO LOGIN] "
        "Запускаю собственный Playwright login."
    )

    try:

        from playwright.sync_api import (
            TimeoutError as PlaywrightTimeoutError,
        )

        from playwright.sync_api import (
            sync_playwright,
        )

    except Exception:

        logger.exception(
            "[AUTO LOGIN] "
            "Playwright не импортируется."
        )

        return None

    browser = None
    context = None

    try:

        with sync_playwright() as pw:

            # ------------------------------------------------
            # Chromium
            # ------------------------------------------------

            logger.info(
                "[AUTO LOGIN] "
                "Запускаю Chromium."
            )

            browser = pw.chromium.launch(
                headless=True,
                executable_path=browser_executable,
                args=[
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
                    "--disable-popup-blocking",
                    "--disable-prompt-on-repost",
                    "--disable-sync",
                    "--no-first-run",
                    "--no-zygote",
                    "--renderer-process-limit=1",
                    "--lang=en-US,en",
                ],
            )

            # ------------------------------------------------
            # Context
            # ------------------------------------------------

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/146.0.0.0 "
                    "Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                viewport={
                    "width": 1366,
                    "height": 768,
                },
                extra_http_headers={
                    "Accept-Language":
                        "en-US,en;q=0.9",
                },
            )

            # ------------------------------------------------
            # Do not modify site logic.
            # Just reduce the obvious automation flag.
            # ------------------------------------------------

            context.add_init_script(
                """
                Object.defineProperty(
                    navigator,
                    'webdriver',
                    {
                        get: () => undefined
                    }
                );
                """
            )

            page = context.new_page()

            # ------------------------------------------------
            # Page errors
            # ------------------------------------------------

            page.on(
                "pageerror",
                lambda exc: logger.warning(
                    "[AUTO LOGIN] Page error: %s",
                    exc,
                ),
            )

            page.on(
                "console",
                lambda msg: (
                    logger.debug(
                        "[AUTO LOGIN] Browser console: %s",
                        msg.text,
                    )
                    if msg.type in (
                        "error",
                        "warning",
                    )
                    else None
                ),
            )

            # ------------------------------------------------
            # Open login
            # ------------------------------------------------

            logger.info(
                "[AUTO LOGIN] "
                "Открываю %s",
                LOGIN_URL,
            )

            try:

                response = page.goto(
                    LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                if response:

                    logger.info(
                        "[AUTO LOGIN] "
                        "HTTP status: %s",
                        response.status,
                    )

            except PlaywrightTimeoutError:

                logger.warning(
                    "[AUTO LOGIN] "
                    "Page.goto timeout."
                )

            # ------------------------------------------------
            # Wait a little for JS
            # ------------------------------------------------

            page.wait_for_timeout(
                3000
            )

            logger.info(
                "[AUTO LOGIN] Current URL: %s",
                page.url,
            )

            # ------------------------------------------------
            # Detect obvious block/captcha
            # ------------------------------------------------

            body_text = ""

            try:

                body_text = (
                    page.locator(
                        "body"
                    ).inner_text(
                        timeout=5000
                    )
                    or ""
                )

            except Exception:
                pass

            lower_body = body_text.lower()

            blocked_words = (
                "captcha",
                "verify you are human",
                "access denied",
                "cloudflare",
                "security check",
                "checking your browser",
            )

            if any(
                word in lower_body
                for word in blocked_words
            ):

                logger.error(
                    "[AUTO LOGIN] "
                    "Pocket Option returned "
                    "a security/CAPTCHA page."
                )

                return None

            # ------------------------------------------------
            # Find email input
            # ------------------------------------------------

            email_selectors = [
                'input[name="email"]',
                'input[type="email"]',
                'input[autocomplete="email"]',
                'input[placeholder*="email" i]',
            ]

            email_locator = None

            for selector in email_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if locator.count() > 0:

                        email_locator = locator

                        logger.info(
                            "[AUTO LOGIN] "
                            "Email field found: %s",
                            selector,
                        )

                        break

                except Exception:
                    continue

            if email_locator is None:

                logger.error(
                    "[AUTO LOGIN] "
                    "Email field not found."
                )

                try:

                    logger.error(
                        "[AUTO LOGIN] "
                        "Page title: %s",
                        page.title(),
                    )

                except Exception:
                    pass

                return None

            # ------------------------------------------------
            # Password input
            # ------------------------------------------------

            password_selectors = [
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]',
            ]

            password_locator = None

            for selector in password_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if locator.count() > 0:

                        password_locator = locator

                        logger.info(
                            "[AUTO LOGIN] "
                            "Password field found: %s",
                            selector,
                        )

                        break

                except Exception:
                    continue

            if password_locator is None:

                logger.error(
                    "[AUTO LOGIN] "
                    "Password field not found."
                )

                return None

            # ------------------------------------------------
            # Fill
            # ------------------------------------------------

            logger.info(
                "[AUTO LOGIN] "
                "Заполняю email/password."
            )

            email_locator.fill(
                email,
                timeout=30000,
            )

            password_locator.fill(
                password,
                timeout=30000,
            )

            # ------------------------------------------------
            # Remember
            # ------------------------------------------------

            remember_selectors = [
                'input[name="remember"]',
                'input[type="checkbox"]',
            ]

            for selector in remember_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if locator.count() > 0:

                        if not locator.is_checked():

                            locator.check(
                                timeout=2000
                            )

                        break

                except Exception:
                    continue

            # ------------------------------------------------
            # Submit
            # ------------------------------------------------

            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Sign in")',
                'button:has-text("Login")',
            ]

            submitted = False

            for selector in submit_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if locator.count() > 0:

                        logger.info(
                            "[AUTO LOGIN] "
                            "Нажимаю кнопку: %s",
                            selector,
                        )

                        locator.click(
                            timeout=10000
                        )

                        submitted = True

                        break

                except Exception:
                    continue

            if not submitted:

                logger.error(
                    "[AUTO LOGIN] "
                    "Кнопка входа не найдена."
                )

                return None

            # ------------------------------------------------
            # Wait for redirect / session
            # ------------------------------------------------

            logger.info(
                "[AUTO LOGIN] "
                "Ожидаю авторизацию..."
            )

            deadline = (
                asyncio.get_event_loop
                if False
                else None
            )

            # Playwright-side polling.
            for _ in range(60):

                page.wait_for_timeout(
                    1000
                )

                # --------------------------------------------
                # Check cookie
                # --------------------------------------------

                cookies = context.cookies()

                for cookie in cookies:

                    if (
                        cookie.get("name")
                        == "po_session"
                    ):

                        value = (
                            cookie.get("value")
                            or ""
                        ).strip()

                        if value:

                            logger.info(
                                "[AUTO LOGIN] "
                                "po_session получен."
                            )

                            return (
                                _build_ssid_from_cookie(
                                    value,
                                    demo=demo,
                                )
                            )

                # --------------------------------------------
                # Check URL
                # --------------------------------------------

                current_url = page.url

                if "/login/" not in current_url:

                    logger.info(
                        "[AUTO LOGIN] "
                        "Login redirect: %s",
                        current_url,
                    )

                # --------------------------------------------
                # Detect login error
                # --------------------------------------------

                try:

                    errors = page.locator(
                        ".error, "
                        ".alert, "
                        ".form-error, "
                        "[role='alert']"
                    )

                    if errors.count() > 0:

                        text = (
                            errors.first
                            .text_content(
                                timeout=1000
                            )
                            or ""
                        ).strip()

                        if text:

                            logger.warning(
                                "[AUTO LOGIN] "
                                "Login page error: %s",
                                text[:500],
                            )

                except Exception:
                    pass

            logger.error(
                "[AUTO LOGIN] "
                "Авторизация завершилась "
                "без po_session."
            )

            logger.error(
                "[AUTO LOGIN] Final URL: %s",
                page.url,
            )

            return None

    except Exception:

        logger.exception(
            "[AUTO LOGIN] "
            "Custom Playwright login exception."
        )

        return None

    finally:

        try:

            if context is not None:
                context.close()

        except Exception:
            pass

        try:

            if browser is not None:
                browser.close()

        except Exception:
            pass


# ============================================================
# MARKET
# ============================================================

class PocketMarket:

    def __init__(self):

        self.client: Optional[Any] = None

        self.connected = False

        self.ssid: Optional[str] = None

        self._login_lock = asyncio.Lock()

        self._connect_lock = asyncio.Lock()

        logger.info(
            "[MARKET] PocketMarket создан."
        )

    # ========================================================
    # AUTO LOGIN
    # ========================================================

    async def auto_login(
        self,
    ) -> Optional[str]:

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
                "[AUTO LOGIN] "
                "Подготавливаю Playwright..."
            )

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
                    "[AUTO LOGIN] "
                    "Chromium не найден."
                )

                return None

            logger.info(
                "[AUTO LOGIN] "
                "Playwright готов."
            )

            logger.info(
                "[AUTO LOGIN] "
                "Browser executable: %s",
                browser_executable,
            )

            demo = bool(
                getattr(
                    config,
                    "PO_DEMO",
                    True,
                )
            )

            try:

                ssid = await asyncio.wait_for(
                    asyncio.to_thread(
                        _browser_login_sync,
                        str(email),
                        str(password),
                        browser_executable,
                        demo,
                    ),
                    timeout=AUTO_LOGIN_TIMEOUT,
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[AUTO LOGIN] "
                    "Custom login timeout: %s sec",
                    AUTO_LOGIN_TIMEOUT,
                )

                return None

            except Exception:

                logger.exception(
                    "[AUTO LOGIN] "
                    "Custom login failed."
                )

                return None

            if not ssid:

                logger.error(
                    "[AUTO LOGIN] "
                    "Login не получил SSID."
                )

                return None

            self.ssid = ssid

            logger.info(
                "[AUTO LOGIN] "
                "SSID успешно получен."
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
                "STEP 3/5: "
                "Создаю PocketOptionAsync client."
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

    async def _check_connection(
        self,
    ) -> bool:

        if self.client is None:
            return False

        try:

            balance_method = getattr(
                self.client,
                "balance",
                None,
            )

            if not callable(
                balance_method
            ):

                logger.warning(
                    "[MARKET] "
                    "balance() отсутствует."
                )

                return True

            result = await asyncio.wait_for(
                balance_method(),
                timeout=BALANCE_TIMEOUT,
            )

            logger.info(
                "[MARKET] "
                "Connection check OK. "
                "Balance=%s",
                result,
            )

            return True

        except asyncio.TimeoutError:

            logger.warning(
                "[MARKET] "
                "balance() timeout."
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

    async def connect(
        self,
    ) -> bool:

        async with self._connect_lock:

            self.connected = False

            logger.info(
                "[MARKET] "
                "Подключение к Pocket Option..."
            )

            # ------------------------------------------------
            # Close previous client
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
            # Existing SSID
            # ------------------------------------------------

            ssid = getattr(
                config,
                "PO_SSID",
                None,
            )

            if ssid:

                ssid = str(
                    ssid
                ).strip()

                logger.info(
                    "[MARKET] "
                    "STEP 1/5: "
                    "Использую PO_SSID."
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
            # Client
            # ------------------------------------------------

            try:

                created = await asyncio.wait_for(
                    self._create_client(
                        ssid
                    ),
                    timeout=CONNECT_TIMEOUT,
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[MARKET] "
                    "STEP 3/5 TIMEOUT."
                )

                return False

            if not created:

                logger.error(
                    "[MARKET] "
                    "STEP 3/5 FAILED."
                )

                return False

            # ------------------------------------------------
            # Connection check
            # ------------------------------------------------

            logger.info(
                "[MARKET] "
                "STEP 4/5: "
                "Проверяю соединение."
            )

            connected = await (
                self._check_connection()
            )

            if not connected:

                logger.warning(
                    "[MARKET] "
                    "Первый connection check failed."
                )

                await asyncio.sleep(3)

                connected = await (
                    self._check_connection()
                )

            if not connected:

                logger.error(
                    "[MARKET] "
                    "STEP 4/5 FAILED."
                )

                await self.disconnect()

                return False

            # ------------------------------------------------
            # Ready
            # ------------------------------------------------

            self.connected = True

            logger.info(
                "[MARKET] "
                "STEP 5/5: "
                "Pocket Option connected."
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

        if (
            not self.connected
            or self.client is None
        ):

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

            if callable(
                get_candles
            ):

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

            elif callable(
                candles_method
            ):

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

        if result is None:
            return []

        if isinstance(
            result,
            tuple,
        ):

            result = result[0]

        if not isinstance(
            result,
            list,
        ):

            try:
                result = list(result)

            except Exception:
                return []

        normalized: list[
            dict[str, Any]
        ] = []

        for candle in result:

            if isinstance(
                candle,
                dict,
            ):

                item = dict(
                    candle
                )

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

            timestamp = (
                item.get(
                    "timestamp"
                )
                or item.get(
                    "time"
                )
                or item.get(
                    "timestamp_ms"
                )
            )

            if timestamp is None:
                continue

            try:

                timestamp = float(
                    timestamp
                )

                if timestamp > 10_000_000_000:

                    timestamp /= 1000.0

            except Exception:

                continue

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

        normalized.sort(
            key=lambda x:
            x["timestamp"]
        )

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
    # GET CANDLES
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

    async def reconnect(
        self,
    ) -> bool:

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

            if callable(
                reconnect_method
            ):

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

    async def disconnect(
        self,
    ) -> None:

        self.connected = False

        client = self.client

        if client is None:
            return

        self.client = None

        shutdown = getattr(
            client,
            "shutdown",
            None,
        )

        if callable(
            shutdown
        ):

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

        close = getattr(
            client,
            "close",
            None,
        )

        if callable(
            close
        ):

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
    # CLOSE
    # ========================================================

    async def close(
        self,
    ) -> None:

        await self.disconnect()

    # ========================================================
    # STATE
    # ========================================================

    def is_connected(
        self,
    ) -> bool:

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

        if not callable(
            method
        ):

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

                balance = (
                    await market.balance()
                )

                print(
                    "BALANCE:",
                    balance,
                )

                try:

                    candles = (
                        await market.candles(
                            "EURUSD_otc",
                            minutes=1,
                            limit=10,
                        )
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
