from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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
        parse_mode=ParseMode.HTML
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
    title="Pocket Option Signal Bot"
)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected": MARKET_READY,
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected": MARKET_READY,
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
    """
    Безопасное подтверждение Telegram callback.

    Telegram callback_query имеет короткий срок жизни.
    Если обработчик сначала выполняет тяжёлую операцию,
    callback может стать просроченным.

    Поэтому все обработчики вызывают эту функцию сразу
    после получения callback.

    Ошибки:
      - query is too old
      - response timeout expired
      - query ID is invalid

    НЕ должны ломать обработчик.
    """

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

def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_ANALYSIS_LOCKS.get(user_id)

    if lock is None:
        lock = asyncio.Lock()
        USER_ANALYSIS_LOCKS[user_id] = lock

    return lock


# ============================================================
# CONFIG HELPERS
# ============================================================

def get_config_pairs() -> list[tuple[str, str]]:
    pairs = getattr(config, "pairs", None)

    if not pairs:
        raise RuntimeError(
            "В config.py отсутствует список pairs."
        )

    return list(pairs)


def get_config_timeframes() -> list[int]:
    values = getattr(config, "timeframes", None)

    if not values:
        raise RuntimeError(
            "В config.py отсутствует список timeframes."
        )

    return [int(x) for x in values]


def pair_name(symbol: str) -> str:
    for name, internal in get_config_pairs():
        if internal == symbol:
            return name

    return symbol


# ============================================================
# SIGNAL TEXT
# ============================================================

def direction_text(direction: str) -> str:
    value = str(direction).upper()

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
        ]
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
        ]
    )


def signal_pair_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🌐 ЛЮБАЯ ПАРА",
                callback_data="sigp:ANY",
            )
        ]
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
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
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
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад к паре",
                callback_data="signal",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# ============================================================
# MESSAGE HELPERS
# ============================================================

async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """
    Безопасное редактирование сообщения.

    Если сообщение уже изменилось, было удалено или Telegram
    вернул ошибку — пытаемся отправить новое сообщение.
    """

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

    except Exception:
        logger.debug(
            "Не удалось обновить progress",
            exc_info=True,
        )


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(signal) -> str:
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
# MARKET
# ============================================================

async def ensure_market_ready() -> bool:
    global MARKET_READY

    if MARKET_READY:
        return True

    async with MARKET_CONNECT_LOCK:
        if MARKET_READY:
            return True

        try:
            logger.info(
                "Connecting to Pocket Option..."
            )

            await market.connect()

            MARKET_READY = True

            logger.info(
                "Pocket Option market connected"
            )

            return True

        except Exception:
            MARKET_READY = False

            logger.exception(
                "Pocket Option connection failed"
            )

            return False


async def connect_market_with_retry():
    global MARKET_READY

    delay = 5

    while True:
        try:
            await ensure_market_ready()

            if MARKET_READY:
                return

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Market connector error"
            )

        await asyncio.sleep(delay)

        delay = min(
            delay * 2,
            60,
        )


async def get_market_data(
    pair: str,
    required_candles: int,
):
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
            raise RuntimeError(
                "Пустой ответ рынка"
            )

        if len(candles) < 60:
            raise RuntimeError(
                f"Недостаточно свечей: {len(candles)}"
            )

        return candles

    except Exception:
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

    for pair_index, pair in enumerate(
        pairs,
        start=1,
    ):
        await progress_message(
            callback,
            (
                "🔎 <b>АНАЛИЗ OTC-РЫНКА</b>\n\n"
                f"💱 Пара: "
                f"<b>{pair_name(pair)}</b>\n"
                f"📊 Пары: "
                f"<b>{pair_index}/{total}</b>\n"
                f"⏱ Таймфреймы: "
                f"<b>{', '.join(map(str, timeframes))} мин</b>\n\n"
                "Получение рыночных данных..."
            ),
        )

        try:
            candles = await get_market_data(
                pair,
                required_candles,
            )

            successful_pairs += 1

        except Exception:
            continue

        for timeframe in timeframes:
            completed += 1

            await progress_message(
                callback,
                (
                    "🔎 <b>ТЕХНИЧЕСКИЙ АНАЛИЗ</b>\n\n"
                    f"💱 {pair_name(pair)}\n"
                    f"⏱ {timeframe} мин\n"
                    f"📊 Проверка: "
                    f"<b>{completed}</b>\n\n"
                    "EMA • RSI • MACD • Bollinger • "
                    "Stochastic • Momentum • ATR"
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

    return (
        best_signal,
        successful_pairs,
    )


# ============================================================
# DATABASE SIGNAL
# ============================================================

async def save_best_signal(signal):
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
    # КРИТИЧНО:
    # подтверждаем callback ДО любых других операций.
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
    # callback подтверждается ПЕРВЫМ действием.
    await safe_callback_answer(
        callback
    )

    status = (
        "🟢 Рыночный источник подключён."
        if MARKET_READY
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

    user_id = callback.from_user.id

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
        display = pair_name(selected)

    else:
        await safe_edit(
            callback,
            "❌ Некорректная OTC-пара.",
            main_keyboard(),
        )
        return

    try:
        await update_user(
            user_id,
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
    # Самое первое действие.
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
                username=(
                    callback.from_user.username
                ),
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

        await progress_message(
            callback,
            (
                "🔌 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                "Подключение к Pocket Option..."
            ),
        )

        if not await ensure_market_ready():
            await safe_edit(
                callback,
                (
                    "⚠️ <b>РЫНОЧНЫЕ ДАННЫЕ "
                    "НЕ ПОЛУЧЕНЫ</b>\n\n"
                    "Не удалось подключиться "
                    "к Pocket Option.\n\n"
                    "Это ошибка источника данных, "
                    "а не отсутствие сигнала."
                ),
                main_keyboard(),
            )
            return

        best_signal, successful_pairs = (
            await scan_market(
                callback,
                pairs,
                timeframes,
            )
        )

        if successful_pairs == 0:
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
                    "Слабый сигнал специально не выдаётся."
                ),
                main_keyboard(),
            )
            return

        await save_best_signal(
            best_signal
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
            username=(
                callback.from_user.username
            ),
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
            username=(
                callback.from_user.username
            ),
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
    return int(user_id) == int(
        config.OWNER_ID
    )


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

    await safe_edit(
        callback,
        (
            "🤖 <b>АВТОСКАНЕР</b>\n\n"
            f"Глобальный статус: "
            f"<b>{'🟢 ВКЛ' if AUTO_SIGNALS else '🔴 ВЫКЛ'}</b>\n\n"
            f"Рынок: "
            f"<b>{'🟢 подключён' if MARKET_READY else '🔴 не подключён'}</b>\n\n"
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
                    int(config.SCAN_INTERVAL),
                )
            )

            if (
                not AUTO_SIGNALS
                or not MARKET_READY
            ):
                continue

            users = await get_access_users()

            for user in users:
                try:
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
                        else [selected_pair]
                    )

                    best_signal = None

                    for pair in pairs:
                        try:
                            candles = (
                                await get_market_data(
                                    pair,
                                    max(
                                        240,
                                        timeframe * 60
                                        + 180,
                                    ),
                                )
                            )

                            signal = engine.analyze(
                                pair,
                                timeframe,
                                candles,
                            )

                        except Exception as exc:
                            logger.warning(
                                "Auto analysis failed "
                                "%s/%s: %s",
                                pair,
                                timeframe,
                                exc,
                            )
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

                    LAST_KEYS.add(key)

                    if len(LAST_KEYS) > 5000:
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

            await asyncio.sleep(10)


# ============================================================
# RESULT CHECKER
# ============================================================

async def result_checker_loop():
    logger.info(
        "Result checker started"
    )

    while True:
        try:
            await asyncio.sleep(30)

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

            await asyncio.sleep(10)


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

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
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

    await dp.start_polling(
        bot
    )


# ============================================================
# START BOT
# ============================================================

async def start_bot():
    global MARKET_READY

    await init_db()

    tasks = [
        asyncio.create_task(
            start_http_server()
        ),
        asyncio.create_task(
            connect_market_with_retry()
        ),
        asyncio.create_task(
            auto_scanner_loop()
        ),
        asyncio.create_task(
            result_checker_loop()
        ),
    ]

    try:
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
