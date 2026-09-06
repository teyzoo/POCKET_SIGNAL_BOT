from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

import config


logger = logging.getLogger("pocket_market")


# ============================================================
# CONFIG
# ============================================================

LOGIN_URL = "https://pocketoption.com/en/login/"
BASE_URL = "https://pocketoption.com/"

RUNTIME_PLAYWRIGHT_PATH = "/tmp/pocket-option-ms-playwright"

CONNECT_TIMEOUT = 75
NETWORK_TEST_TIMEOUT = 15
BROWSER_START_TIMEOUT = 25
PAGE_LOAD_TIMEOUT = 25
AUTO_LOGIN_TIMEOUT = 90
BALANCE_TIMEOUT = 20
CANDLE_REQUEST_TIMEOUT = 25
CLIENT_CLOSE_TIMEOUT = 10

WEBSOCKET_INIT_DELAY = 5

MIN_CANDLES = 20


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
# GENERIC HELPERS
# ============================================================

def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return value
    return value


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        ts = float(value)

        # milliseconds -> seconds
        if ts > 10_000_000_000:
            ts /= 1000.0

        return ts
    except Exception:
        return None


# ============================================================
# PLAYWRIGHT PATHS
# ============================================================

def _get_playwright_sources() -> list[Path]:
    paths: list[Path] = []

    custom = os.getenv("POCKET_PLAYWRIGHT_SOURCE_PATH")

    if custom:
        paths.append(Path(custom))

    env_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")

    if env_path:
        paths.append(Path(env_path))

    paths.extend(
        [
            Path("/opt/render/project/src/.cache/ms-playwright"),
            Path("/opt/render/.cache/ms-playwright"),
            Path("./.cache/ms-playwright"),
        ]
    )

    result: list[Path] = []
    seen: set[str] = set()

    for path in paths:
        try:
            key = str(path.expanduser().resolve())
        except Exception:
            key = str(path)

        if key in seen:
            continue

        seen.add(key)
        result.append(path)

    return result


def _find_executable(
    base_paths: list[Path],
    names: tuple[str, ...],
) -> Optional[str]:

    for base in base_paths:

        if not base.exists():
            continue

        try:
            for name in names:

                # First check direct path.
                direct = base / name

                if (
                    direct.is_file()
                    and os.access(direct, os.X_OK)
                ):
                    return str(direct.resolve())

                # Then recursively search.
                for path in base.rglob(name):

                    try:
                        if (
                            path.is_file()
                            and os.access(path, os.X_OK)
                        ):
                            return str(path.resolve())
                    except Exception:
                        continue

        except Exception:
            logger.exception(
                "[PLAYWRIGHT] Ошибка поиска executable: %s",
                base,
            )

    return None


def prepare_playwright_environment() -> dict[str, Optional[str]]:
    """
    Finds both Chromium and Firefox.

    Returns:
        {
            "chromium": "...",
            "firefox": "..."
        }
    """

    runtime = Path(RUNTIME_PLAYWRIGHT_PATH)

    runtime.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "[PLAYWRIGHT] Runtime path: %s",
        runtime,
    )

    chromium_names = (
        "chrome",
        "chromium",
        "chromium-browser",
    )

    firefox_names = (
        "firefox",
    )

    chromium = _find_executable(
        [runtime],
        chromium_names,
    )

    firefox = _find_executable(
        [runtime],
        firefox_names,
    )

    if chromium or firefox:
        logger.info(
            "[PLAYWRIGHT] Runtime browser(s): chromium=%s firefox=%s",
            chromium,
            firefox,
        )

        return {
            "chromium": chromium,
            "firefox": firefox,
        }

    for source in _get_playwright_sources():

        if not source.exists():
            continue

        logger.info(
            "[PLAYWRIGHT] Проверяю source: %s",
            source,
        )

        chromium = _find_executable(
            [source],
            chromium_names,
        )

        firefox = _find_executable(
            [source],
            firefox_names,
        )

        if not chromium and not firefox:
            continue

        try:
            # Clean runtime before copying.
            for item in runtime.iterdir():

                try:
                    if item.is_dir():
                        shutil.rmtree(
                            item,
                            ignore_errors=True,
                        )
                    else:
                        item.unlink(
                            missing_ok=True,
                        )
                except Exception:
                    pass

            shutil.copytree(
                source,
                runtime,
                dirs_exist_ok=True,
            )

        except Exception:
            logger.exception(
                "[PLAYWRIGHT] Не удалось скопировать browser runtime."
            )

        chromium = _find_executable(
            [runtime],
            chromium_names,
        )

        firefox = _find_executable(
            [runtime],
            firefox_names,
        )

        logger.info(
            "[PLAYWRIGHT] Prepared: chromium=%s firefox=%s",
            chromium,
            firefox,
        )

        return {
            "chromium": chromium,
            "firefox": firefox,
        }

    logger.error(
        "[PLAYWRIGHT] Ни Chromium, ни Firefox не найдены."
    )

    return {
        "chromium": None,
        "firefox": None,
    }


# ============================================================
# NETWORK DIAGNOSTICS
# ============================================================

def _http_probe_sync(
    url: str,
    timeout: int = NETWORK_TEST_TIMEOUT,
) -> tuple[bool, Optional[int], str, float]:

    started = time.monotonic()

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/146.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            status = int(
                getattr(response, "status", 0)
            )

            # Only read a small amount.
            response.read(1024)

            elapsed = time.monotonic() - started

            return (
                True,
                status,
                "",
                elapsed,
            )

    except urllib.error.HTTPError as exc:

        elapsed = time.monotonic() - started

        return (
            True,
            int(exc.code),
            f"HTTPError: {exc}",
            elapsed,
        )

    except Exception as exc:

        elapsed = time.monotonic() - started

        return (
            False,
            None,
            f"{type(exc).__name__}: {exc}",
            elapsed,
        )


def _dns_probe_sync() -> tuple[bool, str]:

    try:
        started = time.monotonic()

        addresses = socket.getaddrinfo(
            "pocketoption.com",
            443,
            type=socket.SOCK_STREAM,
        )

        elapsed = time.monotonic() - started

        unique = sorted(
            {
                item[4][0]
                for item in addresses
                if item and item[4]
            }
        )

        return (
            True,
            (
                f"DNS OK ({elapsed:.2f}s): "
                f"{', '.join(unique[:8])}"
            ),
        )

    except Exception as exc:

        return (
            False,
            f"DNS ERROR: {type(exc).__name__}: {exc}",
        )


async def network_diagnostics() -> bool:

    logger.info(
        "[NETWORK] Проверяю DNS pocketoption.com..."
    )

    dns_ok, dns_message = await asyncio.to_thread(
        _dns_probe_sync
    )

    if dns_ok:
        logger.info(
            "[NETWORK] %s",
            dns_message,
        )
    else:
        logger.error(
            "[NETWORK] %s",
            dns_message,
        )

        return False

    logger.info(
        "[NETWORK] Проверяю HTTPS %s",
        BASE_URL,
    )

    ok, status, error, elapsed = await asyncio.to_thread(
        _http_probe_sync,
        BASE_URL,
        NETWORK_TEST_TIMEOUT,
    )

    if ok:
        logger.info(
            "[NETWORK] HTTPS OK: status=%s time=%.2fs",
            status,
            elapsed,
        )
        return True

    logger.error(
        "[NETWORK] HTTPS FAILED: time=%.2fs error=%s",
        elapsed,
        error,
    )

    return False


# ============================================================
# COOKIE -> SSID
# ============================================================

def _build_ssid_from_cookie(
    session_value: str,
    demo: bool,
    uid: int = 0,
) -> str:

    payload = {
        "session": session_value,
        "isDemo": 1 if demo else 0,
        "uid": int(uid),
        "platform": 2,
    }

    return (
        '42["auth",'
        + json.dumps(
            payload,
            separators=(",", ":"),
        )
        + "]"
    )


# ============================================================
# PLAYWRIGHT LOGIN
# ============================================================

def _browser_login_sync(
    email: str,
    password: str,
    browsers: dict[str, Optional[str]],
    demo: bool,
) -> Optional[str]:

    logger.info(
        "[AUTO LOGIN] Запускаю собственный Playwright login."
    )

    try:
        from playwright.sync_api import (
            TimeoutError as PlaywrightTimeoutError,
        )
        from playwright.sync_api import sync_playwright

    except Exception:
        logger.exception(
            "[AUTO LOGIN] Playwright не импортируется."
        )
        return None

    browser_configs: list[
        tuple[str, Any, Optional[str]]
    ] = []

    chromium = browsers.get("chromium")
    firefox = browsers.get("firefox")

    if chromium:
        browser_configs.append(
            (
                "chromium",
                "chromium",
                chromium,
            )
        )

    if firefox:
        browser_configs.append(
            (
                "firefox",
                "firefox",
                firefox,
            )
        )

    if not browser_configs:
        logger.error(
            "[AUTO LOGIN] Нет доступного браузера."
        )
        return None

    for browser_name, browser_type_name, executable in browser_configs:

        browser = None
        context = None

        try:

            logger.info(
                "[AUTO LOGIN] Browser backend: %s",
                browser_name,
            )

            with sync_playwright() as pw:

                browser_type = getattr(
                    pw,
                    browser_type_name,
                )

                launch_kwargs: dict[str, Any] = {
                    "headless": True,
                    "timeout": BROWSER_START_TIMEOUT * 1000,
                }

                if executable:
                    launch_kwargs[
                        "executable_path"
                    ] = executable

                if browser_name == "chromium":

                    launch_kwargs["args"] = [
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
                        "--disable-sync",
                        "--no-first-run",
                        "--no-zygote",
                        "--lang=en-US",
                    ]

                logger.info(
                    "[AUTO LOGIN] Запускаю %s.",
                    browser_name,
                )

                browser = browser_type.launch(
                    **launch_kwargs
                )

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
                    viewport={
                        "width": 1366,
                        "height": 768,
                    },
                    extra_http_headers={
                        "Accept-Language":
                            "en-US,en;q=0.9",
                    },
                )

                page = context.new_page()

                page.set_default_timeout(
                    10000
                )

                page.set_default_navigation_timeout(
                    PAGE_LOAD_TIMEOUT * 1000
                )

                page.on(
                    "pageerror",
                    lambda exc: logger.warning(
                        "[AUTO LOGIN] Page error: %s",
                        exc,
                    ),
                )

                page.on(
                    "requestfailed",
                    lambda request: logger.warning(
                        "[AUTO LOGIN] Request failed: %s -> %s",
                        request.url[:300],
                        request.failure,
                    ),
                )

                # ------------------------------------------------
                # Open login page
                # ------------------------------------------------

                logger.info(
                    "[AUTO LOGIN] Открываю %s",
                    LOGIN_URL,
                )

                response = None

                try:

                    response = page.goto(
                        LOGIN_URL,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT * 1000,
                    )

                    if response:
                        logger.info(
                            "[AUTO LOGIN] HTTP status: %s",
                            response.status,
                        )

                except PlaywrightTimeoutError:

                    logger.warning(
                        "[AUTO LOGIN] "
                        "goto timeout (%ss).",
                        PAGE_LOAD_TIMEOUT,
                    )

                except Exception as exc:

                    logger.error(
                        "[AUTO LOGIN] "
                        "goto error: %s",
                        exc,
                    )

                logger.info(
                    "[AUTO LOGIN] Current URL: %s",
                    page.url,
                )

                # If browser is still blank, this backend did not
                # manage to reach the website.
                if page.url in (
                    "",
                    "about:blank",
                ):

                    logger.error(
                        "[AUTO LOGIN] "
                        "%s остался на about:blank.",
                        browser_name,
                    )

                    continue

                try:
                    page.wait_for_timeout(
                        2000
                    )
                except Exception:
                    pass

                # ------------------------------------------------
                # Read page
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

                if any(
                    marker in lower_body
                    for marker in (
                        "access denied",
                        "verify you are human",
                        "security check",
                        "checking your browser",
                        "captcha",
                    )
                ):

                    logger.error(
                        "[AUTO LOGIN] "
                        "Сайт вернул страницу защиты/CAPTCHA."
                    )

                    return None

                # ------------------------------------------------
                # Already authenticated?
                # ------------------------------------------------

                cookies = context.cookies()

                for cookie in cookies:

                    if cookie.get("name") == "po_session":

                        session_value = (
                            cookie.get("value")
                            or ""
                        ).strip()

                        if session_value:

                            logger.info(
                                "[AUTO LOGIN] "
                                "po_session уже существует."
                            )

                            return _build_ssid_from_cookie(
                                session_value,
                                demo=demo,
                            )

                # ------------------------------------------------
                # Email
                # ------------------------------------------------

                email_selectors = [
                    'input[name="email"]',
                    'input[type="email"]',
                    'input[autocomplete="email"]',
                    'input[placeholder*="email" i]',
                    'input[placeholder*="Email"]',
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
                            "[AUTO LOGIN] Page title: %s",
                            page.title(),
                        )
                    except Exception:
                        pass

                    # Try the other browser backend.
                    continue

                # ------------------------------------------------
                # Password
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

                    continue

                # ------------------------------------------------
                # Fill
                # ------------------------------------------------

                logger.info(
                    "[AUTO LOGIN] "
                    "Заполняю email/password."
                )

                email_locator.fill(
                    email,
                    timeout=10000,
                )

                password_locator.fill(
                    password,
                    timeout=10000,
                )

                # ------------------------------------------------
                # Submit
                # ------------------------------------------------

                submit_selectors = [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Sign in")',
                    'button:has-text("Login")',
                    'button:has-text("Log in")',
                ]

                submitted = False

                for selector in submit_selectors:

                    try:

                        locator = page.locator(
                            selector
                        ).first

                        if locator.count() <= 0:
                            continue

                        if not locator.is_visible(
                            timeout=1000
                        ):
                            continue

                        logger.info(
                            "[AUTO LOGIN] "
                            "Нажимаю: %s",
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

                    continue

                # ------------------------------------------------
                # Wait for authentication
                # ------------------------------------------------

                logger.info(
                    "[AUTO LOGIN] "
                    "Ожидаю авторизацию..."
                )

                for second in range(1, 61):

                    try:
                        page.wait_for_timeout(
                            1000
                        )
                    except Exception:
                        break

                    try:
                        cookies = context.cookies()
                    except Exception:
                        cookies = []

                    for cookie in cookies:

                        if cookie.get("name") != "po_session":
                            continue

                        session_value = (
                            cookie.get("value")
                            or ""
                        ).strip()

                        if not session_value:
                            continue

                        logger.info(
                            "[AUTO LOGIN] "
                            "po_session получен на %ss.",
                            second,
                        )

                        return _build_ssid_from_cookie(
                            session_value,
                            demo=demo,
                        )

                    if second in (
                        5,
                        10,
                        20,
                        30,
                        45,
                        60,
                    ):

                        logger.info(
                            "[AUTO LOGIN] "
                            "Ожидание сессии: %ss, URL=%s",
                            second,
                            page.url,
                        )

                    # Look for obvious login errors.
                    try:

                        error_locators = page.locator(
                            ".error, "
                            ".alert, "
                            ".form-error, "
                            "[role='alert']"
                        )

                        count = error_locators.count()

                        if count > 0:

                            text = (
                                error_locators
                                .first
                                .text_content(
                                    timeout=1000
                                )
                                or ""
                            ).strip()

                            if text:
                                logger.warning(
                                    "[AUTO LOGIN] "
                                    "Login error: %s",
                                    text[:500],
                                )

                    except Exception:
                        pass

                logger.error(
                    "[AUTO LOGIN] "
                    "po_session не получен."
                )

                logger.error(
                    "[AUTO LOGIN] Final URL: %s",
                    page.url,
                )

        except Exception as exc:

            logger.exception(
                "[AUTO LOGIN] "
                "%s backend failed: %s",
                browser_name,
                exc,
            )

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

    logger.error(
        "[AUTO LOGIN] "
        "Все browser backends завершились неудачно."
    )

    return None


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

        self._last_network_check = 0.0

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

            # ------------------------------------------------
            # Network test
            # ------------------------------------------------

            now = time.monotonic()

            # Don't hammer the website with diagnostics.
            if now - self._last_network_check > 30:

                self._last_network_check = now

                network_ok = await network_diagnostics()

                if not network_ok:

                    logger.error(
                        "[AUTO LOGIN] "
                        "Pocket Option недоступен из Render."
                    )

                    return None

            # ------------------------------------------------
            # Browser
            # ------------------------------------------------

            logger.info(
                "[AUTO LOGIN] "
                "Подготавливаю Playwright..."
            )

            try:

                browsers = await asyncio.wait_for(
                    asyncio.to_thread(
                        prepare_playwright_environment
                    ),
                    timeout=30,
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

            if not browsers.get("chromium") and not browsers.get(
                "firefox"
            ):

                logger.error(
                    "[AUTO LOGIN] "
                    "Browser executable не найден."
                )

                return None

            logger.info(
                "[AUTO LOGIN] Playwright готов."
            )

            logger.info(
                "[AUTO LOGIN] Chromium: %s",
                browsers.get("chromium"),
            )

            logger.info(
                "[AUTO LOGIN] Firefox: %s",
                browsers.get("firefox"),
            )

            demo = bool(
                getattr(
                    config,
                    "PO_DEMO",
                    True,
                )
            )

            # ------------------------------------------------
            # Login
            # ------------------------------------------------

            try:

                ssid = await asyncio.wait_for(
                    asyncio.to_thread(
                        _browser_login_sync,
                        str(email),
                        str(password),
                        browsers,
                        demo,
                    ),
                    timeout=AUTO_LOGIN_TIMEOUT,
                )

            except asyncio.TimeoutError:

                logger.error(
                    "[AUTO LOGIN] "
                    "Общий login timeout: %ss",
                    AUTO_LOGIN_TIMEOUT,
                )

                return None

            except Exception:

                logger.exception(
                    "[AUTO LOGIN] "
                    "Login exception."
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
                "Жду инициализацию WebSocket %ss...",
                WEBSOCKET_INIT_DELAY,
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

            # Client exists, but there is no balance method.
            return True

        try:

            result = await asyncio.wait_for(
                balance_method(),
                timeout=BALANCE_TIMEOUT,
            )

            logger.info(
                "[MARKET] "
                "Connection check OK. Balance=%s",
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
            # SSID or auto login
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
                    "STEP 1/5: Использую PO_SSID."
                )

            else:

                logger.info(
                    "[MARKET] "
                    "STEP 1/5: Автоматический login."
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

                await self.disconnect()

                return False

            if not created:

                logger.error(
                    "[MARKET] "
                    "STEP 3/5 FAILED."
                )

                await self.disconnect()

                return False

            # ------------------------------------------------
            # Check connection
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

                await asyncio.sleep(
                    2
                )

                connected = await self._check_connection()

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
                "STEP 5/5: Pocket Option connected."
            )

            logger.info(
                "[MARKET] MARKET READY."
            )

            return True

    # ========================================================
    # CANDLE NORMALIZATION
    # ========================================================

    def _normalize_candle(
        self,
        candle: Any,
    ) -> Optional[dict[str, Any]]:

        if candle is None:
            return None

        if isinstance(
            candle,
            dict,
        ):
            item = dict(candle)

        else:

            item = {}

            for name in (
                "timestamp",
                "time",
                "timestamp_ms",
                "from",
                "at",
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

        timestamp = (
            item.get("timestamp")
            or item.get("time")
            or item.get("timestamp_ms")
            or item.get("from")
            or item.get("at")
        )

        timestamp = _normalize_timestamp(
            timestamp
        )

        if timestamp is None:
            return None

        open_price = _to_float(
            item.get("open")
        )

        high_price = _to_float(
            item.get("high")
        )

        low_price = _to_float(
            item.get("low")
        )

        close_price = _to_float(
            item.get("close")
        )

        if (
            open_price is None
            or high_price is None
            or low_price is None
            or close_price is None
        ):
            return None

        volume = _to_float(
            item.get(
                "volume",
                0,
            )
        )

        if volume is None:
            volume = 0.0

        return {
            "timestamp": timestamp,
            "datetime": timestamp,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }

    # ========================================================
    # CANDLE RESULT EXTRACTION
    # ========================================================

    def _extract_candle_items(
        self,
        result: Any,
    ) -> list[Any]:

        if result is None:
            return []

        # Common API wrappers.
        if isinstance(
            result,
            dict,
        ):

            for key in (
                "data",
                "candles",
                "result",
                "history",
                "items",
            ):

                if key in result:

                    value = result[key]

                    if isinstance(
                        value,
                        dict,
                    ):

                        nested = self._extract_candle_items(
                            value
                        )

                        if nested:
                            return nested

                    if isinstance(
                        value,
                        (list, tuple),
                    ):

                        return list(value)

            # A single candle dict.
            if any(
                key in result
                for key in (
                    "open",
                    "high",
                    "low",
                    "close",
                )
            ):
                return [result]

            return []

        if isinstance(
            result,
            tuple,
        ):

            # Some APIs return (data, something).
            for part in result:

                extracted = self._extract_candle_items(
                    part
                )

                if extracted:
                    return extracted

            return []

        if isinstance(
            result,
            list,
        ):
            return result

        try:
            return list(result)
        except Exception:
            return []

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

        pair = str(
            pair
        ).strip()

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

        period_seconds = (
            minutes * 60
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

        result = None

        try:

            # ------------------------------------------------
            # Official-style get_candles
            # ------------------------------------------------

            if callable(
                get_candles
            ):

                logger.debug(
                    "[MARKET] "
                    "get_candles(%s, %s, %s)",
                    pair,
                    period_seconds,
                    limit,
                )

                result = await asyncio.wait_for(
                    get_candles(
                        pair,
                        period_seconds,
                        limit,
                    ),
                    timeout=CANDLE_REQUEST_TIMEOUT,
                )

            # ------------------------------------------------
            # Compatibility fallback
            # ------------------------------------------------

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
                        minutes,
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

        raw_items = self._extract_candle_items(
            result
        )

        normalized: list[
            dict[str, Any]
        ] = []

        for candle in raw_items:

            item = self._normalize_candle(
                candle
            )

            if item is not None:
                normalized.append(
                    item
                )

        # Remove duplicate timestamps.
        unique: dict[
            float,
            dict[str, Any]
        ] = {}

        for item in normalized:
            unique[
                float(item["timestamp"])
            ] = item

        normalized = list(
            unique.values()
        )

        normalized.sort(
            key=lambda item: float(
                item["timestamp"]
            )
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

            value = int(
                result
            )

            # milliseconds -> seconds
            if value > 10_000_000_000:
                value //= 1000

            return value

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

        logger.info(
            "[MARKET] "
            "Запрашивается reconnect."
        )

        # First try library reconnect.
        if (
            self.client is not None
            and self.ssid
        ):

            reconnect_method = getattr(
                self.client,
                "reconnect",
                None,
            )

            if callable(
                reconnect_method
            ):

                try:

                    logger.info(
                        "[MARKET] "
                        "Пробую reconnect() библиотеки."
                    )

                    await asyncio.wait_for(
                        reconnect_method(),
                        timeout=CONNECT_TIMEOUT,
                    )

                    await asyncio.sleep(
                        3
                    )

                    if await self._check_connection():

                        self.connected = True

                        logger.info(
                            "[MARKET] "
                            "Library reconnect OK."
                        )

                        return True

                except Exception:

                    logger.exception(
                        "[MARKET] "
                        "Library reconnect failed."
                    )

        # Full reconnect.
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

        self.client = None

        if client is None:
            return

        # shutdown()
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

                result = shutdown()

                if asyncio.iscoroutine(
                    result
                ):

                    await asyncio.wait_for(
                        result,
                        timeout=CLIENT_CLOSE_TIMEOUT,
                    )

                logger.info(
                    "[MARKET] "
                    "PocketOption client closed."
                )

                return

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

        # close()
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
    # CLOSE ALIAS
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

            # Some clients return a numeric value.
            value = _to_float(
                result
            )

            if value is not None:
                return value

            # Some wrappers may return a dict.
            if isinstance(
                result,
                dict,
            ):

                for key in (
                    "balance",
                    "amount",
                    "value",
                ):

                    value = _to_float(
                        result.get(key)
                    )

                    if value is not None:
                        return value

            return None

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

            network_ok = await network_diagnostics()

            print(
                "NETWORK:",
                network_ok,
            )

            if not network_ok:
                return

            connected = await market.connect()

            print(
                "CONNECTED:",
                connected,
            )

            if not connected:
                return

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
                        "LAST:",
                        candles[-1],
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
