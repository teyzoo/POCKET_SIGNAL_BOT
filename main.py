from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from fastapi import FastAPI

import config
from database import (
    close_database,
    ensure_user,
    get_access_users,
    get_pair_stats,
    get_recent_signals,
    get_signal_stats,
    get_user,
    get_user_stats,
    init_db,
    mark_expired_signals,
    save_signal,
    update_user,
)
from market import PocketMarket
from signals import SignalEngine


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("POCKET_SIGNAL_BOT")


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()


# ============================================================
# CORE
# ============================================================

market = PocketMarket()
engine = SignalEngine()

AUTO_SIGNALS = True
MARKET_READY = False

MARKET_CONNECT_LOCK = asyncio.Lock()

USER_ANALYSIS_LOCKS: dict[int, asyncio.Lock] = {}

LAST_KEYS: set[str] = set()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Pocket Option Signal Bot",
)


def market_is_connected() -> bool:
    return bool(
        getattr(
            market,
            "connected",
            False,
        )
        and getattr(
            market,
            "client",
            None,
        ) is not None
    )


def sync_market_ready() -> bool:
    global MARKET_READY

    actual = market_is_connected()

    if MARKET_READY and not actual:
        logger.warning(
            "[MARKET] MARKET_READY=True, "
            "но PocketMarket уже отключён."
        )

    MARKET_READY = actual

    return actual


def mark_market_disconnected(
    reason: str = "",
) -> None:
    global MARKET_READY

    if MARKET_READY:
        logger.warning(
            "[MARKET] Connection lost%s",
            f": {reason}" if reason else ".",
        )

    MARKET_READY = False


@app.get("/")
async def root():
    connected = sync_market_ready()

    return {
        "status": "ok",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected": connected,
    }


@app.get("/health")
async def health():
    connected = sync_market_ready()

    return {
        "status": "healthy",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected": connected,
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# CALLBACK HELPERS
# ============================================================

async def safe_callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> bool:
    try:
        await callback.answer(
            text=text,
            show_alert=show_alert,
        )
        return True

    except Exception as exc:
        error_text = str(exc).lower()

        if (
            "query is too old" in error_text
            or "response timeout expired" in error_text
            or "query id is invalid" in error_text
            or "query_id_invalid" in error_text
        ):
            logger.debug(
                "Callback expired/invalid: %s",
                exc,
            )
        else:
            logger.warning(
                "Callback answer failed: %s",
                exc,
            )

        return False


# ============================================================
# USER LOCK
# ============================================================

def get_user_lock(
    user_id: int,
) -> asyncio.Lock:
    lock = USER_ANALYSIS_LOCKS.get(
        user_id
    )

    if lock is None:
        lock = asyncio.Lock()
        USER_ANALYSIS_LOCKS[user_id] = lock

    return lock


# ============================================================
# CONFIG HELPERS
# ============================================================

def get_config_pairs() -> list[tuple[str, str]]:
    pairs = getattr(
        config,
        "pairs",
        None,
    )

    if not pairs:
        raise RuntimeError(
            "В config.py отсутствует список pairs."
        )

    return list(pairs)


def get_config_timeframes() -> list[int]:
    values = getattr(
        config,
        "timeframes",
        None,
    )

    if not values:
        raise RuntimeError(
            "В config.py отсутствует список timeframes."
        )

    return [
        int(x)
        for x in values
    ]


def pair_name(
    symbol: str,
) -> str:
    for name, internal in get_config_pairs():
        if internal == symbol:
            return name

    return symbol


# ============================================================
# SIGNAL TEXT
# ============================================================

def direction_text(
    direction: str,
) -> str:
    value = str(
        direction
    ).upper()

    if value in {
        "UP",
        "CALL",
        "BUY",
        "ВВЕРХ",
        "ВЫШЕ",
    }:
        return "🟢 ВВЕРХ"

    if value in {
        "DOWN",
        "PUT",
        "SELL",
        "ВНИЗ",
        "НИЖЕ",
    }:
        return "🔴 ВНИЗ"

    return value


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📈 Сигнал",
                    callback_data="signal",
                ),
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data="settings",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🟢 Автосигналы"
                        if AUTO_SIGNALS
                        else "🔴 Автосигналы"
                    ),
                    callback_data="auto_toggle",
                ),
            ],
        ],
    )


def owner_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Пользователи",
                    callback_data="owner_users",
                ),
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="owner_stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📈 Сигналы",
                    callback_data="owner_signals",
                ),
                InlineKeyboardButton(
                    text="💱 Пары",
                    callback_data="owner_pairs",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Автосканер",
                    callback_data="owner_auto",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="back_main",
                ),
            ],
        ],
    )


def signal_pair_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🌐 ЛЮБАЯ ПАРА",
                callback_data="sigp:ANY",
            ),
        ],
    ]

    row: list[InlineKeyboardButton] = []

    for name, symbol in get_config_pairs():
        row.append(
            InlineKeyboardButton(
                text=name,
                callback_data=f"sigp:{symbol}",
            )
        )

        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="back_main",
            ),
        ],
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows,
    )


def signal_time_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for timeframe in get_config_timeframes():
        row.append(
            InlineKeyboardButton(
                text=f"{timeframe} мин",
                callback_data=f"sigt:{timeframe}",
            )
        )

        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="🌐 ЛЮБОЕ ВРЕМЯ",
                callback_data="sigt:ANY",
            ),
        ],
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад к паре",
                callback_data="signal",
            ),
        ],
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows,
    )


# ============================================================
# MESSAGE HELPERS
# ============================================================

async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    try:
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=reply_markup,
            )
            return

    except Exception as exc:
        logger.debug(
            "Не удалось edit_text: %s",
            exc,
        )

    try:
        if callback.message:
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
            )

    except Exception:
        logger.exception(
            "Не удалось отправить сообщение"
        )


async def progress_message(
    callback: CallbackQuery,
    text: str,
):
    try:
        if callback.message:
            await callback.message.edit_text(
                text
            )

    except Exception as exc:
        error_text = str(exc).lower()

        if "message is not modified" in error_text:
            return

        logger.debug(
            "Не удалось обновить progress: %s",
            exc,
        )


async def progress_message_object(
    message: Message,
    text: str,
):
    try:
        await message.edit_text(
            text
        )

    except Exception as exc:
        error_text = str(exc).lower()

        if "message is not modified" in error_text:
            return

        logger.debug(
            "Не удалось обновить progress message: %s",
            exc,
        )


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(
    signal,
) -> str:
    reasons = getattr(
        signal,
        "reasons",
        None,
    ) or []

    reasons_text = "\n".join(
        f"• {reason}"
        for reason in reasons[:8]
    )

    text = (
        "📈 <b>СИЛЬНЫЙ OTC-СИГНАЛ</b>\n\n"
        f"💱 <b>Пара:</b> "
        f"{pair_name(signal.pair)}\n"
        f"📌 <b>Направление:</b> "
        f"{direction_text(signal.direction)}\n"
        f"⏱ <b>Экспирация:</b> "
        f"{signal.timeframe} мин\n"
        f"🎯 <b>Техническая уверенность:</b> "
        f"{float(signal.probability):.1f}%\n"
        f"⭐ <b>Quality Score:</b> "
        f"{float(signal.quality):.1f}/100\n\n"
        f"🕐 <b>Вход:</b> "
        f"{signal.entry_time.strftime('%H:%M:%S')} UTC\n"
        f"⏰ <b>Закрытие:</b> "
        f"{signal.close_time.strftime('%H:%M:%S')} UTC"
    )

    if reasons_text:
        text += (
            "\n\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{reasons_text}"
        )

    text += (
        "\n\n"
        "⚠️ Технический сигнал не гарантирует прибыль."
    )

    return text


# ============================================================
# MARKET CONNECTION
# ============================================================

async def ensure_market_ready(
    status_callback: Optional[
        Callable[[str], Awaitable[None]]
    ] = None,
) -> bool:
    global MARKET_READY

    if market_is_connected():
        MARKET_READY = True

        if status_callback:
            await status_callback(
                (
                    "📡 <b>POCKET OPTION ПОДКЛЮЧЁН</b>\n\n"
                    "✅ Рыночное соединение уже установлено.\n\n"
                    "📊 Получаю актуальные рыночные данные..."
                )
            )

        return True

    MARKET_READY = False

    async with MARKET_CONNECT_LOCK:
        if market_is_connected():
            MARKET_READY = True
            return True

        MARKET_READY = False

        try:
            logger.info(
                "[MARKET] Connecting to Pocket Option..."
            )

            if status_callback:
                await status_callback(
                    (
                        "🔌 <b>ПОДКЛЮЧЕНИЕ К POCKET OPTION</b>\n\n"
                        "⏳ Авторизация и установка соединения...\n\n"
                        "🌐 Запускаю рыночный источник."
                    )
                )

            connected = await market.connect()

            if not connected:
                MARKET_READY = False

                raise RuntimeError(
                    "PocketMarket.connect() вернул False"
                )

            if not market_is_connected():
                MARKET_READY = False

                raise RuntimeError(
                    "PocketMarket.connect() завершился, "
                    "но market.connected=False"
                )

            MARKET_READY = True

            logger.info(
                "[MARKET] Pocket Option market connected"
            )

            if status_callback:
                await status_callback(
                    (
                        "📡 <b>POCKET OPTION ПОДКЛЮЧЁН</b>\n\n"
                        "✅ Соединение с рыночным источником установлено.\n\n"
                        "📊 Получаю актуальные рыночные данные..."
                    )
                )

            return True

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            MARKET_READY = False

            logger.exception(
                "[MARKET] Pocket Option connection failed: %s",
                exc,
            )

            if status_callback:
                await status_callback(
                    (
                        "❌ <b>ОШИБКА ПОДКЛЮЧЕНИЯ</b>\n\n"
                        "Не удалось установить соединение "
                        "с Pocket Option.\n\n"
                        "🔄 Автоматически повторю подключение."
                    )
                )

            return False


# ============================================================
# MARKET RECOVERY
# ============================================================

async def recover_market(
    status_callback: Optional[
        Callable[[str], Awaitable[None]]
    ] = None,
    attempts: int = 3,
) -> bool:
    mark_market_disconnected(
        "market recovery requested"
    )

    for attempt in range(
        1,
        attempts + 1,
    ):
        if market_is_connected():
            global MARKET_READY
            MARKET_READY = True
            return True

        if status_callback:
            await status_callback(
                (
                    "🔄 <b>ВОССТАНОВЛЕНИЕ СОЕДИНЕНИЯ</b>\n\n"
                    f"Попытка: <b>{attempt}/{attempts}</b>\n\n"
                    "📡 Переподключаю Pocket Option..."
                )
            )

        connected = await ensure_market_ready(
            status_callback=status_callback
        )

        if connected and market_is_connected():
            logger.info(
                "[MARKET] Connection recovered."
            )
            return True

        if attempt < attempts:
            await asyncio.sleep(
                min(
                    5 * attempt,
                    15,
                )
            )

    logger.warning(
        "[MARKET] Recovery attempts exhausted."
    )

    return False


# ============================================================
# MARKET WATCHDOG
# ============================================================

async def connect_market_with_retry():
    global MARKET_READY

    logger.info(
        "[MARKET] Connection watchdog started"
    )

    delay = 5

    while True:
        try:
            if market_is_connected():
                MARKET_READY = True
                delay = 5

                await asyncio.sleep(10)

                if not market_is_connected():
                    mark_market_disconnected(
                        "PocketMarket reported disconnected"
                    )

                continue

            MARKET_READY = False

            logger.warning(
                "[MARKET] Market unavailable. Starting reconnect..."
            )

            connected = await ensure_market_ready()

            if connected and market_is_connected():
                MARKET_READY = True
                delay = 5

                logger.info(
                    "[MARKET] Connection established/recovered."
                )

                await asyncio.sleep(5)

                continue

        except asyncio.CancelledError:
            raise

        except Exception:
            MARKET_READY = False

            logger.exception(
                "[MARKET] Watchdog error"
            )

        logger.info(
            "[MARKET] Next reconnect attempt in %s sec",
            delay,
        )

        await asyncio.sleep(
            delay
        )

        delay = min(
            delay * 2,
            60,
        )


# ============================================================
# MARKET DATA
# ============================================================

async def get_market_data(
    pair: str,
    required_candles: int,
):
    if not market_is_connected():
        mark_market_disconnected(
            f"candle request while disconnected: {pair}"
        )

        raise RuntimeError(
            "Рынок отключён"
        )

    try:
        candles = await market.candles(
            pair,
            minutes=1,
            limit=max(
                200,
                min(
                    required_candles,
                    1600,
                ),
            ),
        )

        if not candles:
            if not market_is_connected():
                mark_market_disconnected(
                    f"market disconnected during candles: {pair}"
                )

                raise RuntimeError(
                    "Рынок отключился во время получения свечей"
                )

            raise RuntimeError(
                "Пустой ответ рынка"
            )

        if len(candles) < 60:
            raise RuntimeError(
                f"Недостаточно свечей: {len(candles)}"
            )

        return candles

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        if not market_is_connected():
            mark_market_disconnected(
                f"{pair}: {exc}"
            )

        logger.exception(
            "Market data failed for %s",
            pair,
        )

        raise


# ============================================================
# MARKET SCANNER
# ============================================================

async def scan_market(
    callback: CallbackQuery,
    pairs: list[str],
    timeframes: list[int],
):
    max_timeframe = max(
        timeframes
    )

    required_candles = max(
        240,
        max_timeframe * 60 + 180,
    )

    total = len(pairs)
    completed = 0

    best_signal = None
    successful_pairs = 0

    total_checks = (
        len(pairs) * len(timeframes)
    )

    await progress_message(
        callback,
        (
            "🧠 <b>АНАЛИЗ РЫНКА НАЧАТ</b>\n\n"
            f"💱 Пары: <b>{total}</b>\n"
            f"⏱ Таймфреймы: "
            f"<b>{', '.join(map(str, timeframes))} мин</b>\n\n"
            "📊 Рассчитываю технические индикаторы...\n"
            "🔎 Ищу сильную точку входа..."
        ),
    )

    for pair_index, pair in enumerate(
        pairs,
        start=1,
    ):
        if not market_is_connected():
            mark_market_disconnected(
                f"scanner before pair {pair}"
            )

            recovered = await recover_market(
                attempts=3,
            )

            if not recovered:
                await progress_message(
                    callback,
                    (
                        "⚠️ <b>РЫНОК ВРЕМЕННО НЕДОСТУПЕН</b>\n\n"
                        "Не удалось восстановить соединение.\n\n"
                        "🤖 Фоновый watchdog продолжит "
                        "автоматические попытки подключения."
                    ),
                )
                break

        await progress_message(
            callback,
            (
                "📊 <b>ПОЛУЧЕНИЕ РЫНОЧНЫХ ДАННЫХ</b>\n\n"
                f"💱 Пара: <b>{pair_name(pair)}</b>\n"
                f"📊 Пары: <b>{pair_index}/{total}</b>\n\n"
                "📡 Получаю актуальные 1-минутные свечи...\n"
                "⏳ Ожидаю корректные рыночные данные..."
            ),
        )

        candles = None

        for data_attempt in range(
            1,
            3,
        ):
            try:
                candles = await get_market_data(
                    pair,
                    required_candles,
                )

                successful_pairs += 1

                logger.info(
                    "Received %s candles for %s",
                    len(candles),
                    pair,
                )

                break

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                logger.warning(
                    "Could not get market data for %s "
                    "(attempt %s/2): %s",
                    pair,
                    data_attempt,
                    exc,
                )

                if market_is_connected():
                    break

                mark_market_disconnected(
                    f"data request failed for {pair}"
                )

                recovered = await recover_market(
                    attempts=3,
                )

                if not recovered:
                    candles = None
                    break

        if not candles:
            if not market_is_connected():
                break

            continue

        for timeframe in timeframes:
            if not market_is_connected():
                mark_market_disconnected(
                    f"scanner during analysis {pair}/{timeframe}"
                )

                await progress_message(
                    callback,
                    (
                        "🔄 <b>СОЕДИНЕНИЕ С РЫНКОМ ПОТЕРЯНО</b>\n\n"
                        "Текущий анализ остановлен.\n\n"
                        "🤖 Фоновый watchdog восстанавливает рынок."
                    ),
                )

                break

            completed += 1

            progress_percent = (
                completed
                / max(
                    total_checks,
                    1,
                )
            ) * 100

            await progress_message(
                callback,
                (
                    "🧠 <b>ТЕХНИЧЕСКИЙ АНАЛИЗ</b>\n\n"
                    f"💱 <b>{pair_name(pair)}</b>\n"
                    f"📊 Пара: <b>{pair_index}/{total}</b>\n"
                    f"⏱ Таймфрейм: <b>{timeframe} мин</b>\n"
                    f"🔄 Проверка: "
                    f"<b>{completed}/{total_checks}</b> "
                    f"({progress_percent:.0f}%)\n\n"
                    "📈 Рассчитываю:\n"
                    "• EMA\n"
                    "• RSI\n"
                    "• MACD\n"
                    "• Bollinger Bands\n"
                    "• Stochastic\n"
                    "• Momentum\n"
                    "• ATR\n\n"
                    "🔎 Ищу сильную точку входа..."
                ),
            )

            try:
                signal = engine.analyze(
                    pair,
                    timeframe,
                    candles,
                )

            except Exception:
                logger.exception(
                    "Signal engine failed: %s / %s",
                    pair,
                    timeframe,
                )
                continue

            if signal is None:
                continue

            await progress_message(
                callback,
                (
                    "🎯 <b>КАНДИДАТ НА СИГНАЛ НАЙДЕН</b>\n\n"
                    f"💱 {pair_name(pair)}\n"
                    f"⏱ {timeframe} мин\n\n"
                    "🧠 Проверяю качество сигнала...\n"
                    "📊 Сравниваю с другими найденными точками..."
                ),
            )

            if best_signal is None:
                best_signal = signal
                continue

            current_quality = float(
                signal.quality
            )

            best_quality = float(
                best_signal.quality
            )

            if current_quality > best_quality:
                best_signal = signal

            elif (
                current_quality == best_quality
                and float(signal.probability)
                > float(
                    best_signal.probability
                )
            ):
                best_signal = signal

        if not market_is_connected():
            break

    if best_signal is not None:
        await progress_message(
            callback,
            (
                "✅ <b>АНАЛИЗ ЗАВЕРШЁН</b>\n\n"
                "🟢 <b>СИЛЬНЫЙ СИГНАЛ НАЙДЕН</b>\n\n"
                f"💱 {pair_name(best_signal.pair)}\n"
                f"📌 {direction_text(best_signal.direction)}\n"
                f"⏱ {best_signal.timeframe} мин\n"
                f"🎯 Уверенность: "
                f"{float(best_signal.probability):.1f}%\n"
                f"⭐ Quality Score: "
                f"{float(best_signal.quality):.1f}/100\n\n"
                "📤 Подготавливаю сигнал..."
            ),
        )

    else:
        if not market_is_connected():
            await progress_message(
                callback,
                (
                    "🔄 <b>АНАЛИЗ ПРИОСТАНОВЛЕН</b>\n\n"
                    "Соединение с Pocket Option было потеряно.\n\n"
                    "🤖 Фоновый watchdog автоматически "
                    "пытается восстановить рынок.\n\n"
                    "После восстановления можно запустить "
                    "анализ повторно."
                ),
            )
        else:
            await progress_message(
                callback,
                (
                    "⚪ <b>АНАЛИЗ ЗАВЕРШЁН</b>\n\n"
                    "Сильного сигнала сейчас нет.\n\n"
                    "📊 Рынок проверен.\n"
                    "🧠 Технический анализ выполнен.\n"
                    "🔎 Сильная точка входа не подтверждена.\n\n"
                    "Слабый сигнал отправлять не буду."
                ),
            )

    return (
        best_signal,
        successful_pairs,
    )


# ============================================================
# DATABASE SIGNAL
# ============================================================

async def save_best_signal(
    signal,
):
    try:
        await save_signal(
            pair=signal.pair,
            timeframe=int(
                signal.timeframe
            ),
            direction=signal.direction,
            probability=float(
                signal.probability
            ),
            quality=float(
                signal.quality
            ),
            entry_time=signal.entry_time,
            close_time=signal.close_time,
            reasons=list(
                signal.reasons
            ),
        )

    except Exception:
        logger.exception(
            "Could not save signal"
        )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message,
):
    if not message.from_user:
        return

    try:
        await ensure_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=(
                message.from_user.first_name
                or ""
            ),
        )

    except Exception:
        logger.exception(
            "Failed to register user"
        )

        await message.answer(
            "⚠️ Не удалось зарегистрировать "
            "пользователя. Попробуй ещё раз."
        )

        return

    await message.answer(
        "🚀 <b>Pocket Option Signal Bot</b>\n\n"
        "Бот анализирует OTC-рынок и ищет "
        "сильные технические ситуации.\n\n"
        "📈 Нажми <b>Сигнал</b>, чтобы начать анализ.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# BACK MAIN
# ============================================================

@dp.callback_query(F.data == "back_main")
async def back_main(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    await safe_edit(
        callback,
        (
            "🚀 <b>Pocket Option Signal Bot</b>\n\n"
            "Выбери действие:"
        ),
        main_keyboard(),
    )


# ============================================================
# SIGNAL MENU
# ============================================================

@dp.callback_query(F.data == "signal")
async def signal_menu(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    connected = sync_market_ready()

    status = (
        "🟢 Рыночный источник подключён."
        if connected
        else "🟡 Рыночный источник подключается."
    )

    await safe_edit(
        callback,
        (
            "📈 <b>НАСТРОЙКА СИГНАЛА</b>\n\n"
            "💱 <b>Выбери OTC-пару:</b>\n\n"
            "🌐 <b>ЛЮБАЯ ПАРА</b> — бот проверит "
            "все доступные OTC-пары и выберет "
            "лучший сигнал.\n\n"
            f"{status}"
        ),
        signal_pair_keyboard(),
    )


# ============================================================
# SIGNAL PAIR
# ============================================================

@dp.callback_query(
    F.data.startswith("sigp:")
)
async def signal_pair_selected(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    selected = callback.data.split(
        ":",
        1,
    )[1]

    valid_pairs = {
        symbol
        for _, symbol in get_config_pairs()
    }

    if selected == "ANY":
        database_pair = "ANY"
        display = "🌐 Любая пара"

    elif selected in valid_pairs:
        database_pair = selected
        display = pair_name(
            selected
        )

    else:
        await safe_edit(
            callback,
            "❌ Некорректная OTC-пара.",
            main_keyboard(),
        )
        return

    try:
        await update_user(
            callback.from_user.id,
            pair=database_pair,
        )

    except Exception:
        logger.exception(
            "Could not save selected pair"
        )

    await safe_edit(
        callback,
        (
            "⏱ <b>ВЫБОР ЭКСПИРАЦИИ</b>\n\n"
            f"💱 <b>Пара:</b> {display}\n\n"
            "Выбери время закрытия сигнала:"
        ),
        signal_time_keyboard(),
    )


# ============================================================
# SIGNAL TIME
# ============================================================

@dp.callback_query(
    F.data.startswith("sigt:")
)
async def signal_time_selected(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    user_id = callback.from_user.id

    selected_time = callback.data.split(
        ":",
        1,
    )[1]

    lock = get_user_lock(
        user_id
    )

    if lock.locked():
        await safe_edit(
            callback,
            (
                "⏳ <b>АНАЛИЗ УЖЕ ИДЁТ</b>\n\n"
                "Дождись завершения текущего анализа."
            ),
        )
        return

    async with lock:
        user = await get_user(
            user_id
        )

        if not user:
            await ensure_user(
                telegram_id=user_id,
                username=callback.from_user.username,
                first_name=(
                    callback.from_user.first_name
                    or ""
                ),
            )

            user = await get_user(
                user_id
            )

        if not user:
            await safe_edit(
                callback,
                "⚠️ Пользователь не найден.",
                main_keyboard(),
            )
            return

        selected_pair = (
            getattr(
                user,
                "pair",
                None,
            )
            or "ANY"
        )

        if selected_pair == "ANY":
            pairs = [
                symbol
                for _, symbol in get_config_pairs()
            ]
        else:
            pairs = [
                selected_pair
            ]

        valid_timeframes = (
            get_config_timeframes()
        )

        if selected_time == "ANY":
            timeframes = valid_timeframes

        else:
            try:
                timeframe = int(
                    selected_time
                )

            except ValueError:
                await safe_edit(
                    callback,
                    "❌ Некорректное время.",
                    main_keyboard(),
                )
                return

            if timeframe not in valid_timeframes:
                await safe_edit(
                    callback,
                    "❌ Некорректное время.",
                    main_keyboard(),
                )
                return

            timeframes = [
                timeframe
            ]

        async def telegram_status(
            text: str,
        ):
            await progress_message(
                callback,
                text,
            )

        await telegram_status(
            (
                "🔌 <b>ПОДКЛЮЧЕНИЕ К POCKET OPTION</b>\n\n"
                "⏳ Авторизация и установка соединения...\n\n"
                "🌐 Подготавливаю рыночный источник."
            )
        )

        if not await ensure_market_ready(
            status_callback=telegram_status
        ):
            await safe_edit(
                callback,
                (
                    "⚠️ <b>РЫНОЧНЫЕ ДАННЫЕ "
                    "НЕ ПОЛУЧЕНЫ</b>\n\n"
                    "Не удалось подключиться "
                    "к Pocket Option.\n\n"
                    "🤖 Фоновый watchdog продолжит "
                    "автоматические попытки подключения."
                ),
                main_keyboard(),
            )
            return

        if not market_is_connected():
            await safe_edit(
                callback,
                (
                    "⚠️ <b>РЫНОК НЕДОСТУПЕН</b>\n\n"
                    "Соединение было потеряно "
                    "перед началом анализа.\n\n"
                    "🔄 Watchdog автоматически "
                    "восстанавливает соединение."
                ),
                main_keyboard(),
            )
            return

        await telegram_status(
            (
                "📡 <b>POCKET OPTION ПОДКЛЮЧЁН</b>\n\n"
                "✅ Рыночное соединение установлено.\n\n"
                "📊 Получаю актуальные рыночные данные..."
            )
        )

        await asyncio.sleep(
            0.2
        )

        best_signal, successful_pairs = (
            await scan_market(
                callback,
                pairs,
                timeframes,
            )
        )

        if successful_pairs == 0:
            if not market_is_connected():
                await safe_edit(
                    callback,
                    (
                        "🔄 <b>РЫНОЧНОЕ СОЕДИНЕНИЕ ПОТЕРЯНО</b>\n\n"
                        "Во время анализа Pocket Option "
                        "разорвал соединение.\n\n"
                        "🤖 Бот уже пытается восстановить "
                        "соединение автоматически.\n\n"
                        "Запусти анализ повторно после "
                        "восстановления рынка."
                    ),
                    main_keyboard(),
                )
            else:
                await safe_edit(
                    callback,
                    (
                        "⚠️ <b>РЫНОЧНЫЕ ДАННЫЕ "
                        "НЕ ПОЛУЧЕНЫ</b>\n\n"
                        "Бот не получил корректные свечи "
                        "ни по одной из проверяемых "
                        "OTC-пар.\n\n"
                        "Попробуй повторить анализ."
                    ),
                    main_keyboard(),
                )

            return

        if best_signal is None:
            await safe_edit(
                callback,
                (
                    "⚪ <b>СИЛЬНОГО OTC-СИГНАЛА НЕТ</b>\n\n"
                    f"💱 Пара: "
                    f"{'Любая' if selected_pair == 'ANY' else pair_name(selected_pair)}\n"
                    f"⏱ Время: "
                    f"{'Любое' if selected_time == 'ANY' else selected_time + ' мин'}\n\n"
                    f"🎯 Минимальная техническая "
                    f"уверенность: "
                    f"<b>{float(config.MIN_PROBABILITY):.1f}%</b>\n"
                    f"⭐ Минимальный Quality Score: "
                    f"<b>{float(config.MIN_SIGNAL_SCORE):.1f}</b>\n\n"
                    "📊 Рынок проанализирован.\n"
                    "Слабый сигнал специально не выдаётся."
                ),
                main_keyboard(),
            )
            return

        await telegram_status(
            (
                "✅ <b>АНАЛИЗ ЗАВЕРШЁН</b>\n\n"
                "🟢 <b>СИЛЬНЫЙ СИГНАЛ НАЙДЕН</b>\n\n"
                "📊 Формирую итоговое сообщение..."
            )
        )

        await save_best_signal(
            best_signal
        )

        await asyncio.sleep(
            0.15
        )

        await safe_edit(
            callback,
            format_signal(
                best_signal
            ),
            main_keyboard(),
        )


# ============================================================
# SETTINGS
# ============================================================

@dp.callback_query(
    F.data == "settings"
)
async def settings_handler(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    user = await get_user(
        callback.from_user.id
    )

    if not user:
        await ensure_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=(
                callback.from_user.first_name
                or ""
            ),
        )

        user = await get_user(
            callback.from_user.id
        )

    if not user:
        await safe_edit(
            callback,
            "⚠️ Не удалось загрузить профиль.",
            main_keyboard(),
        )
        return

    pair = (
        getattr(
            user,
            "pair",
            None,
        )
        or "ANY"
    )

    timeframe = int(
        getattr(
            user,
            "timeframe",
            None,
        )
        or 5
    )

    auto = bool(
        getattr(
            user,
            "auto_signals",
            True,
        )
    )

    pair_display = (
        "🌐 Любая"
        if pair == "ANY"
        else pair_name(pair)
    )

    await safe_edit(
        callback,
        (
            "⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"💱 Пара: <b>{pair_display}</b>\n"
            f"⏱ Экспирация: "
            f"<b>{timeframe} мин</b>\n"
            f"🤖 Автосигналы: "
            f"<b>{'ВКЛ' if auto else 'ВЫКЛ'}</b>\n\n"
            "Пара и экспирация меняются через "
            "раздел 📈 Сигнал."
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📈 Выбрать сигнал",
                        callback_data="signal",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🤖 Переключить автосигналы",
                        callback_data="auto_toggle",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Главное меню",
                        callback_data="back_main",
                    )
                ],
            ]
        ),
    )


# ============================================================
# AUTO TOGGLE
# ============================================================

@dp.callback_query(
    F.data == "auto_toggle"
)
async def auto_toggle(
    callback: CallbackQuery,
):
    global AUTO_SIGNALS

    await safe_callback_answer(
        callback
    )

    user_id = callback.from_user.id

    user = await get_user(
        user_id
    )

    if not user:
        await ensure_user(
            telegram_id=user_id,
            username=callback.from_user.username,
            first_name=(
                callback.from_user.first_name
                or ""
            ),
        )

        user = await get_user(
            user_id
        )

    if not user:
        await safe_edit(
            callback,
            "⚠️ Профиль не найден.",
            main_keyboard(),
        )
        return

    new_value = not bool(
        getattr(
            user,
            "auto_signals",
            True,
        )
    )

    try:
        await update_user(
            user_id,
            auto_signals=new_value,
        )

    except Exception:
        logger.exception(
            "Could not update auto_signals"
        )

        await safe_edit(
            callback,
            "⚠️ Не удалось изменить настройку.",
            main_keyboard(),
        )
        return

    AUTO_SIGNALS = new_value

    await safe_edit(
        callback,
        (
            "🤖 <b>АВТОСИГНАЛЫ</b>\n\n"
            f"Статус: "
            f"<b>{'🟢 ВКЛЮЧЕНЫ' if new_value else '🔴 ВЫКЛЮЧЕНЫ'}</b>\n\n"
            "Настройка сохранена."
        ),
        main_keyboard(),
    )


# ============================================================
# OWNER
# ============================================================

def is_owner(
    user_id: int,
) -> bool:
    return int(
        user_id
    ) == int(
        config.OWNER_ID
    )


# ============================================================
# ADMIN COMMAND
# ============================================================

@dp.message(
    F.text == "/admin"
)
async def admin_command(
    message: Message,
):
    if not message.from_user:
        return

    if not is_owner(
        message.from_user.id
    ):
        await message.answer(
            "⛔ Доступ запрещён."
        )
        return

    await message.answer(
        "👑 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        "Выбери раздел:",
        reply_markup=owner_keyboard(),
    )


# ============================================================
# OWNER USERS
# ============================================================

@dp.callback_query(
    F.data == "owner_users"
)
async def owner_users(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    if not is_owner(
        callback.from_user.id
    ):
        await safe_edit(
            callback,
            "⛔ Доступ запрещён.",
        )
        return

    try:
        stats = await get_user_stats()

        await safe_edit(
            callback,
            (
                "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
                f"👤 Всего: "
                f"<b>{stats['total']}</b>\n"
                f"🟢 Активных: "
                f"<b>{stats['active']}</b>\n"
                f"🔴 Заблокировано: "
                f"<b>{stats['blocked']}</b>"
            ),
            owner_keyboard(),
        )

    except Exception:
        logger.exception(
            "Owner users error"
        )

        await safe_edit(
            callback,
            "⚠️ Не удалось получить статистику.",
            owner_keyboard(),
        )


# ============================================================
# OWNER STATS
# ============================================================

@dp.callback_query(
    F.data == "owner_stats"
)
async def owner_stats(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    if not is_owner(
        callback.from_user.id
    ):
        await safe_edit(
            callback,
            "⛔ Доступ запрещён.",
        )
        return

    try:
        stats = await get_signal_stats()

        await safe_edit(
            callback,
            (
                "📊 <b>СТАТИСТИКА СИГНАЛОВ</b>\n\n"
                f"📈 Всего: "
                f"<b>{stats['total']}</b>\n"
                f"🟢 WIN: "
                f"<b>{stats['wins']}</b>\n"
                f"🔴 LOSS: "
                f"<b>{stats['losses']}</b>\n"
                f"📌 Решено: "
                f"<b>{stats['decided']}</b>\n"
                f"🎯 WINRATE: "
                f"<b>{stats['winrate']:.2f}%</b>"
            ),
            owner_keyboard(),
        )

    except Exception:
        logger.exception(
            "Owner stats error"
        )

        await safe_edit(
            callback,
            "⚠️ Не удалось получить статистику.",
            owner_keyboard(),
        )


# ============================================================
# OWNER SIGNALS
# ============================================================

@dp.callback_query(
    F.data == "owner_signals"
)
async def owner_signals(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    if not is_owner(
        callback.from_user.id
    ):
        await safe_edit(
            callback,
            "⛔ Доступ запрещён.",
        )
        return

    try:
        signals = await get_recent_signals(
            limit=10
        )

        if not signals:
            text = (
                "📈 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>\n\n"
                "Сигналов пока нет."
            )

        else:
            lines = [
                "📈 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>\n"
            ]

            for signal in signals:
                result = (
                    signal.result
                    or signal.status
                    or "PENDING"
                )

                lines.append(
                    f"#{signal.id} "
                    f"{pair_name(signal.pair)} "
                    f"{direction_text(signal.direction)} "
                    f"{signal.timeframe}м "
                    f"Q:{float(signal.quality):.1f} "
                    f"— <b>{result}</b>"
                )

            text = "\n".join(
                lines
            )

        await safe_edit(
            callback,
            text,
            owner_keyboard(),
        )

    except Exception:
        logger.exception(
            "Owner signals error"
        )

        await safe_edit(
            callback,
            "⚠️ Не удалось получить сигналы.",
            owner_keyboard(),
        )


# ============================================================
# OWNER PAIRS
# ============================================================

@dp.callback_query(
    F.data == "owner_pairs"
)
async def owner_pairs(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    if not is_owner(
        callback.from_user.id
    ):
        await safe_edit(
            callback,
            "⛔ Доступ запрещён.",
        )
        return

    try:
        stats = await get_pair_stats()

        lines = [
            "💱 <b>СТАТИСТИКА ПО ПАРАМ</b>\n"
        ]

        if not stats:
            lines.append(
                "Сигналов пока нет."
            )

        else:
            for pair, count in stats:
                lines.append(
                    f"• {pair_name(pair)} — "
                    f"<b>{count}</b>"
                )

        await safe_edit(
            callback,
            "\n".join(lines),
            owner_keyboard(),
        )

    except Exception:
        logger.exception(
            "Owner pairs error"
        )

        await safe_edit(
            callback,
            "⚠️ Не удалось получить пары.",
            owner_keyboard(),
        )


# ============================================================
# OWNER AUTO
# ============================================================

@dp.callback_query(
    F.data == "owner_auto"
)
async def owner_auto(
    callback: CallbackQuery,
):
    await safe_callback_answer(
        callback
    )

    if not is_owner(
        callback.from_user.id
    ):
        await safe_edit(
            callback,
            "⛔ Доступ запрещён.",
        )
        return

    connected = sync_market_ready()

    await safe_edit(
        callback,
        (
            "🤖 <b>АВТОСКАНЕР</b>\n\n"
            f"Глобальный статус: "
            f"<b>{'🟢 ВКЛ' if AUTO_SIGNALS else '🔴 ВЫКЛ'}</b>\n\n"
            f"Рынок: "
            f"<b>{'🟢 подключён' if connected else '🔴 не подключён'}</b>\n\n"
            f"Интервал: "
            f"<b>{config.SCAN_INTERVAL} сек</b>"
        ),
        owner_keyboard(),
    )


# ============================================================
# AUTO SCANNER
# ============================================================

async def auto_scanner_loop():
    global LAST_KEYS

    logger.info(
        "Auto scanner started"
    )

    while True:
        try:
            await asyncio.sleep(
                max(
                    10,
                    int(
                        config.SCAN_INTERVAL
                    ),
                )
            )

            if not AUTO_SIGNALS:
                continue

            if not market_is_connected():
                sync_market_ready()

                logger.debug(
                    "[AUTO] Market unavailable. Skipping cycle."
                )

                continue

            users = await get_access_users()

            for user in users:
                try:
                    if not market_is_connected():
                        mark_market_disconnected(
                            "auto scanner"
                        )

                        logger.warning(
                            "[AUTO] Market disconnected. "
                            "Stopping current cycle."
                        )

                        break

                    if not bool(
                        getattr(
                            user,
                            "auto_signals",
                            True,
                        )
                    ):
                        continue

                    selected_pair = (
                        getattr(
                            user,
                            "pair",
                            None,
                        )
                        or "ANY"
                    )

                    timeframe = int(
                        getattr(
                            user,
                            "timeframe",
                            None,
                        )
                        or 5
                    )

                    pairs = (
                        [
                            symbol
                            for _, symbol
                            in get_config_pairs()
                        ]
                        if selected_pair == "ANY"
                        else [
                            selected_pair
                        ]
                    )

                    best_signal = None
                    market_lost = False

                    for pair in pairs:
                        if not market_is_connected():
                            mark_market_disconnected(
                                f"auto scanner {pair}"
                            )

                            market_lost = True

                            break

                        try:
                            candles = await get_market_data(
                                pair,
                                max(
                                    240,
                                    timeframe * 60 + 180,
                                ),
                            )

                            signal = engine.analyze(
                                pair,
                                timeframe,
                                candles,
                            )

                        except asyncio.CancelledError:
                            raise

                        except Exception as exc:
                            logger.warning(
                                "Auto analysis failed "
                                "%s/%s: %s",
                                pair,
                                timeframe,
                                exc,
                            )

                            if not market_is_connected():
                                mark_market_disconnected(
                                    f"auto analysis {pair}"
                                )

                                market_lost = True
                                break

                            continue

                        if signal is None:
                            continue

                        if (
                            best_signal is None
                            or float(
                                signal.quality
                            )
                            > float(
                                best_signal.quality
                            )
                        ):
                            best_signal = signal

                    if market_lost:
                        break

                    if best_signal is None:
                        continue

                    key = (
                        f"{user.telegram_id}:"
                        f"{best_signal.pair}:"
                        f"{best_signal.timeframe}:"
                        f"{best_signal.direction}:"
                        f"{best_signal.entry_time.isoformat()}"
                    )

                    if key in LAST_KEYS:
                        continue

                    LAST_KEYS.add(
                        key
                    )

                    if len(
                        LAST_KEYS
                    ) > 5000:
                        LAST_KEYS = set(
                            list(
                                LAST_KEYS
                            )[-2500:]
                        )

                    await save_best_signal(
                        best_signal
                    )

                    await bot.send_message(
                        user.telegram_id,
                        format_signal(
                            best_signal
                        ),
                    )

                except asyncio.CancelledError:
                    raise

                except Exception:
                    logger.exception(
                        "Auto scanner user error"
                    )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Auto scanner loop error"
            )

            await asyncio.sleep(
                10
            )


# ============================================================
# RESULT CHECKER
# ============================================================

async def result_checker_loop():
    logger.info(
        "Result checker started"
    )

    while True:
        try:
            await asyncio.sleep(
                30
            )

            expired = (
                await mark_expired_signals()
            )

            if expired:
                logger.info(
                    "Marked %s signals as expired",
                    expired,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Result checker error"
            )

            await asyncio.sleep(
                10
            )


# ============================================================
# HTTP SERVER
# ============================================================

async def start_http_server():
    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    logger.info(
        "[HTTP] Starting FastAPI on 0.0.0.0:%s",
        port,
    )

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=False,
        )
    )

    await server.serve()


# ============================================================
# TELEGRAM POLLING
# ============================================================

async def start_telegram():
    await bot.delete_webhook(
        drop_pending_updates=False
    )

    logger.info(
        "[TELEGRAM] Starting polling..."
    )

    await dp.start_polling(
        bot
    )


# ============================================================
# START BOT
# ============================================================

async def start_bot():
    global MARKET_READY

    await init_db()

    # --------------------------------------------------------
    # IMPORTANT:
    # HTTP запускается сразу.
    # Pocket Option login не блокирует Render health/port.
    # --------------------------------------------------------

    http_task = asyncio.create_task(
        start_http_server()
    )

    market_task = asyncio.create_task(
        connect_market_with_retry()
    )

    auto_task = asyncio.create_task(
        auto_scanner_loop()
    )

    result_task = asyncio.create_task(
        result_checker_loop()
    )

    tasks = [
        http_task,
        market_task,
        auto_task,
        result_task,
    ]

    try:
        # Даём Uvicorn возможность открыть порт
        # до тяжёлой авторизации Pocket Option.
        await asyncio.sleep(
            0.5
        )

        await start_telegram()

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        MARKET_READY = False

        try:
            await market.close()

        except Exception:
            logger.exception(
                "Market close error"
            )

        try:
            await bot.session.close()

        except Exception:
            logger.exception(
                "Bot session close error"
            )

        try:
            await close_database()

        except Exception:
            logger.exception(
                "Database close error"
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(
            start_bot()
        )

    except KeyboardInterrupt:
        pass
