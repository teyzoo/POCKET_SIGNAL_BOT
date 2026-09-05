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
# BOT
# ============================================================

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()


# ============================================================
# SERVICES
# ============================================================

market = PocketMarket()
engine = SignalEngine()


# ============================================================
# GLOBAL STATE
# ============================================================

AUTO_SIGNALS = True

LAST_KEYS: set[str] = set()

USER_ANALYSIS_LOCKS: dict[int, asyncio.Lock] = {}

MARKET_READY = False

MARKET_CONNECT_LOCK = asyncio.Lock()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Pocket Option Signal Bot",
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
# HELPERS
# ============================================================

def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_ANALYSIS_LOCKS.get(user_id)

    if lock is None:
        lock = asyncio.Lock()
        USER_ANALYSIS_LOCKS[user_id] = lock

    return lock


def pair_name(symbol: str) -> str:
    for display_name, internal_name in config.pairs:
        if internal_name == symbol:
            return display_name

    return symbol


def pair_symbol(display_name: str) -> Optional[str]:
    for name, symbol in config.pairs:
        if name == display_name:
            return symbol

    return None


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


def timeframe_text(minutes: int) -> str:
    return f"{minutes} мин"


def get_config_pairs() -> list[tuple[str, str]]:
    pairs = getattr(config, "pairs", None)

    if not pairs:
        raise RuntimeError(
            "В config.py отсутствует список pairs."
        )

    return list(pairs)


def get_config_timeframes() -> list[int]:
    timeframes = getattr(config, "timeframes", None)

    if not timeframes:
        raise RuntimeError(
            "В config.py отсутствует список timeframes."
        )

    return [int(x) for x in timeframes]


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

    current_row: list[InlineKeyboardButton] = []

    for display_name, symbol in get_config_pairs():
        current_row.append(
            InlineKeyboardButton(
                text=display_name,
                callback_data=f"sigp:{symbol}",
            )
        )

        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

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
    timeframes = get_config_timeframes()

    rows: list[list[InlineKeyboardButton]] = []

    current_row: list[InlineKeyboardButton] = []

    for timeframe in timeframes:
        current_row.append(
            InlineKeyboardButton(
                text=f"{timeframe} мин",
                callback_data=f"sigt:{timeframe}",
            )
        )

        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

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
# UI
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
    except Exception:
        pass

    try:
        if callback.message:
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
            )
    except Exception:
        logger.exception(
            "Could not edit/send message"
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
        logger.exception(
            "Could not update progress message"
        )


def format_signal(signal) -> str:
    reasons = getattr(
        signal,
        "reasons",
        None,
    ) or []

    reasons_text = ""

    if reasons:
        reasons_text = "\n".join(
            f"• {reason}"
            for reason in reasons[:8]
        )

    entry_time = signal.entry_time

    close_time = signal.close_time

    text = (
        "📈 <b>СИЛЬНЫЙ OTC-СИГНАЛ</b>\n\n"

        f"💱 <b>Пара:</b> "
        f"{pair_name(signal.pair)}\n"

        f"📌 <b>Направление:</b> "
        f"{direction_text(signal.direction)}\n"

        f"⏱ <b>Экспирация:</b> "
        f"{signal.timeframe} мин\n"

        f"🎯 <b>Вероятность:</b> "
        f"{float(signal.probability):.1f}%\n"

        f"⭐ <b>Quality Score:</b> "
        f"{float(signal.quality):.1f}/100\n\n"

        f"🕐 <b>Вход:</b> "
        f"{entry_time.strftime('%H:%M:%S')} UTC\n"

        f"⏰ <b>Закрытие:</b> "
        f"{close_time.strftime('%H:%M:%S')} UTC"
    )

    if reasons_text:
        text += (
            "\n\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{reasons_text}"
        )

    text += (
        "\n\n"
        "⚠️ Сигнал основан на техническом "
        "анализе и не гарантирует прибыль."
    )

    return text


# ============================================================
# MARKET CONNECTION
# ============================================================

async def connect_market_with_retry():
    global MARKET_READY

    async with MARKET_CONNECT_LOCK:

        if MARKET_READY:
            return True

        delay = 5

        while True:

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

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                MARKET_READY = False

                logger.exception(
                    "Pocket Option connection failed: %s",
                    exc,
                )

                logger.warning(
                    "Retrying Pocket Option connection "
                    "in %s seconds",
                    delay,
                )

                await asyncio.sleep(delay)

                delay = min(
                    delay * 2,
                    60,
                )


async def ensure_market_ready() -> bool:
    global MARKET_READY

    if MARKET_READY:
        return True

    try:
        await market.connect()

        MARKET_READY = True

        return True

    except Exception as exc:

        MARKET_READY = False

        logger.exception(
            "Market connection error: %s",
            exc,
        )

        return False


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    if not message.from_user:
        return

    # ВАЖНО:
    # database.ensure_user() требует:
    # telegram_id
    # username
    # first_name

    try:

        await ensure_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name or "",
        )

    except Exception:

        logger.exception(
            "Failed to register Telegram user"
        )

        await message.answer(
            "⚠️ <b>Не удалось зарегистрировать пользователя.</b>\n\n"
            "Попробуй ещё раз через несколько секунд."
        )

        return

    await message.answer(
        "🚀 <b>Pocket Option Signal Bot</b>\n\n"

        "Бот анализирует OTC-рынок и ищет "
        "сильные технические ситуации.\n\n"

        "📈 Нажми <b>Сигнал</b>, чтобы выбрать "
        "OTC-пару и время экспирации.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# MAIN MENU
# ============================================================

@dp.callback_query(F.data == "back_main")
async def back_main(
    callback: CallbackQuery,
):

    await callback.answer()

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

    await callback.answer()

    if MARKET_READY:

        market_status = (
            "🟢 Рыночный источник подключён."
        )

    else:

        market_status = (
            "🟡 Рыночный источник ещё подключается."
        )

    await safe_edit(
        callback,
        (
            "📈 <b>НАСТРОЙКА СИГНАЛА</b>\n\n"

            "💱 <b>Выбери OTC-пару:</b>\n\n"

            "🌐 <b>ЛЮБАЯ ПАРА</b>\n"
            "Бот проверит доступные OTC-пары "
            "и выберет лучший результат.\n\n"

            f"{market_status}"
        ),
        signal_pair_keyboard(),
    )


# ============================================================
# SIGNAL PAIR SELECTED
# ============================================================

@dp.callback_query(
    F.data.startswith("sigp:")
)
async def signal_pair_selected(
    callback: CallbackQuery,
):

    await callback.answer()

    user_id = callback.from_user.id

    selected_pair = callback.data.split(
        ":",
        1,
    )[1]

    valid_pairs = {
        symbol
        for _, symbol in get_config_pairs()
    }

    if selected_pair == "ANY":

        display_pair = "🌐 Любая пара"

        database_pair = "ANY"

    elif selected_pair in valid_pairs:

        display_pair = pair_name(
            selected_pair
        )

        database_pair = selected_pair

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

            f"💱 <b>Пара:</b> "
            f"{display_pair}\n\n"

            "Выбери время закрытия сигнала:"
        ),
        signal_time_keyboard(),
    )


# ============================================================
# ANALYZE ONE SET
# ============================================================

async def analyze_pair_timeframe(
    pair: str,
    timeframe: int,
):
    """
    Реальный анализ:
    1. Получаем свечи.
    2. Проверяем количество данных.
    3. Передаём их SignalEngine.
    4. Возвращаем сигнал или None.

    Ошибка получения рынка НЕ превращается
    в "слабый сигнал".
    """

    candles = await market.candles(
        pair,
        minutes=1,
        limit=200,
    )

    if not candles:
        raise RuntimeError(
            f"Рыночные данные не получены: {pair}"
        )

    if len(candles) < 60:
        raise RuntimeError(
            f"Недостаточно свечей для {pair}: "
            f"{len(candles)}"
        )

    signal = engine.analyze(
        pair,
        timeframe,
        candles,
    )

    return signal


# ============================================================
# SIGNAL — TIME
# ============================================================

@dp.callback_query(
    F.data.startswith("sigt:")
)
async def signal_time_selected(
    callback: CallbackQuery,
):

    await callback.answer()

    user_id = callback.from_user.id

    selected_time = callback.data.split(
        ":",
        1,
    )[1]

    # --------------------------------------------------------
    # LOCK
    # --------------------------------------------------------

    lock = get_user_lock(user_id)

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

        # ----------------------------------------------------
        # USER
        # ----------------------------------------------------

        user = await get_user(
            user_id
        )

        if not user:

            try:

                await ensure_user(
                    telegram_id=user_id,
                    username=callback.from_user.username,
                    first_name=(
                        callback.from_user.first_name
                        or ""
                    ),
                )

            except Exception:

                logger.exception(
                    "Could not create user"
                )

                await safe_edit(
                    callback,
                    (
                        "⚠️ Не удалось создать "
                        "профиль пользователя."
                    ),
                )

                return

            user = await get_user(
                user_id
            )

        if not user:

            await safe_edit(
                callback,
                "⚠️ Пользователь не найден.",
            )

            return

        # ----------------------------------------------------
        # PAIR
        # ----------------------------------------------------

        selected_pair = (
            getattr(
                user,
                "pair",
                None,
            )
            or "ANY"
        )

        if selected_pair == "ANY":

            pairs_to_check = [
                symbol
                for _, symbol
                in get_config_pairs()
            ]

        else:

            pairs_to_check = [
                selected_pair
            ]

        # ----------------------------------------------------
        # TIMEFRAME
        # ----------------------------------------------------

        if selected_time == "ANY":

            timeframes_to_check = (
                get_config_timeframes()
            )

        else:

            try:

                selected_tf = int(
                    selected_time
                )

            except ValueError:

                await safe_edit(
                    callback,
                    "❌ Некорректное время.",
                    main_keyboard(),
                )

                return

            valid_timeframes = (
                get_config_timeframes()
            )

            if selected_tf not in valid_timeframes:

                await safe_edit(
                    callback,
                    "❌ Некорректное время.",
                    main_keyboard(),
                )

                return

            timeframes_to_check = [
                selected_tf
            ]

        # ----------------------------------------------------
        # MARKET CONNECTION
        # ----------------------------------------------------

        await progress_message(
            callback,
            (
                "🔌 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                "Подключение к Pocket Option..."
            ),
        )

        connected = await ensure_market_ready()

        if not connected:

            await safe_edit(
                callback,
                (
                    "⚠️ <b>РЫНОЧНЫЕ ДАННЫЕ НЕ ПОЛУЧЕНЫ</b>\n\n"
                    "Не удалось подключиться к "
                    "источнику Pocket Option.\n\n"
                    "Попробуй ещё раз через несколько секунд."
                ),
                main_keyboard(),
            )

            return

        # ----------------------------------------------------
        # ANALYSIS
        # ----------------------------------------------------

        total_checks = (
            len(pairs_to_check)
            * len(timeframes_to_check)
        )

        completed = 0

        best_signal = None

        market_data_received = False

        market_errors: list[str] = []

        for pair in pairs_to_check:

            for timeframe in timeframes_to_check:

                completed += 1

                await progress_message(
                    callback,
                    (
                        "🔎 <b>АНАЛИЗ OTC-РЫНКА</b>\n\n"

                        f"💱 Пара: "
                        f"<b>{pair_name(pair)}</b>\n"

                        f"⏱ Экспирация: "
                        f"<b>{timeframe} мин</b>\n\n"

                        f"📊 Проверка: "
                        f"<b>{completed}/{total_checks}</b>\n\n"

                        "Идёт технический анализ..."
                    ),
                )

                try:

                    signal = (
                        await analyze_pair_timeframe(
                            pair,
                            timeframe,
                        )
                    )

                    market_data_received = True

                except asyncio.CancelledError:

                    raise

                except Exception as exc:

                    logger.exception(
                        "Analysis failed for %s / %s",
                        pair,
                        timeframe,
                    )

                    market_errors.append(
                        f"{pair_name(pair)} "
                        f"{timeframe}м: {exc}"
                    )

                    continue

                if signal is None:
                    continue

                # --------------------------------------------
                # BEST SIGNAL
                # --------------------------------------------

                if best_signal is None:

                    best_signal = signal

                else:

                    if (
                        float(signal.quality)
                        > float(best_signal.quality)
                    ):

                        best_signal = signal

                    elif (
                        float(signal.quality)
                        == float(best_signal.quality)
                        and
                        float(signal.probability)
                        >
                        float(
                            best_signal.probability
                        )
                    ):

                        best_signal = signal

        # ----------------------------------------------------
        # NO MARKET DATA
        # ----------------------------------------------------

        if not market_data_received:

            await safe_edit(
                callback,
                (
                    "⚠️ <b>РЫНОЧНЫЕ ДАННЫЕ НЕ ПОЛУЧЕНЫ</b>\n\n"

                    "Бот не смог получить корректные "
                    "свечи Pocket Option.\n\n"

                    "Это не считается отсутствием сигнала."
                ),
                main_keyboard(),
            )

            return

        # ----------------------------------------------------
        # NO STRONG SIGNAL
        # ----------------------------------------------------

        if best_signal is None:

            await safe_edit(
                callback,
                (
                    "⚪ <b>СИЛЬНОГО OTC-СИГНАЛА НЕТ</b>\n\n"

                    f"💱 Пара: "
                    f"{'Любая' if selected_pair == 'ANY' else pair_name(selected_pair)}\n"

                    f"⏱ Время: "
                    f"{'Любое' if selected_time == 'ANY' else selected_time + ' мин'}\n\n"

                    f"🎯 Минимальная вероятность: "
                    f"<b>{float(config.MIN_PROBABILITY):.1f}%</b>\n"

                    f"⭐ Минимальный Quality Score: "
                    f"<b>{float(config.MIN_SIGNAL_SCORE):.1f}</b>\n\n"

                    "Я не буду выдавать слабый сигнал "
                    "только ради того, чтобы что-то показать."
                ),
                main_keyboard(),
            )

            return

        # ----------------------------------------------------
        # SAVE SIGNAL
        # ----------------------------------------------------

        try:

            await save_signal(
                pair=best_signal.pair,
                timeframe=best_signal.timeframe,
                direction=best_signal.direction,
                probability=float(
                    best_signal.probability
                ),
                quality=float(
                    best_signal.quality
                ),
                entry_time=best_signal.entry_time,
                close_time=best_signal.close_time,
                reasons=list(
                    best_signal.reasons
                ),
            )

        except Exception:

            logger.exception(
                "Could not save signal"
            )

        # ----------------------------------------------------
        # SEND SIGNAL
        # ----------------------------------------------------

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

    await callback.answer()

    user = await get_user(
        callback.from_user.id
    )

    if not user:

        try:

            await ensure_user(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=(
                    callback.from_user.first_name
                    or ""
                ),
            )

        except Exception:

            await safe_edit(
                callback,
                "⚠️ Не удалось загрузить профиль.",
            )

            return

        user = await get_user(
            callback.from_user.id
        )

    pair = (
        getattr(
            user,
            "pair",
            None,
        )
        or "ANY"
    )

    timeframe = (
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
            f"⏱ Экспирация: <b>{timeframe} мин</b>\n"
            f"🤖 Автосигналы: "
            f"<b>{'ВКЛ' if auto else 'ВЫКЛ'}</b>\n\n"

            "Настройки пары и времени "
            "можно изменить через раздел "
            "📈 Сигнал."
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

    await callback.answer()

    user_id = callback.from_user.id

    user = await get_user(
        user_id
    )

    if not user:

        try:

            await ensure_user(
                telegram_id=user_id,
                username=callback.from_user.username,
                first_name=(
                    callback.from_user.first_name
                    or ""
                ),
            )

        except Exception:

            await safe_edit(
                callback,
                "⚠️ Не удалось создать профиль.",
            )

            return

        user = await get_user(
            user_id
        )

    current = bool(
        getattr(
            user,
            "auto_signals",
            True,
        )
    )

    new_value = not current

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
# OWNER CHECK
# ============================================================

def is_owner(user_id: int) -> bool:
    return int(user_id) == int(
        config.OWNER_ID
    )


# ============================================================
# OWNER MENU
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


@dp.callback_query(
    F.data == "owner_users"
)
async def owner_users(
    callback: CallbackQuery,
):

    await callback.answer()

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

                f"👤 Всего: <b>{stats['total']}</b>\n"
                f"🟢 Активных: <b>{stats['active']}</b>\n"
                f"🔴 Заблокировано: <b>{stats['blocked']}</b>"
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

    await callback.answer()

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

                f"📈 Всего сигналов: "
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

    await callback.answer()

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
                    (
                        f"#{signal.id} "
                        f"{pair_name(signal.pair)} "
                        f"{direction_text(signal.direction)} "
                        f"{signal.timeframe}м "
                        f"Q:{float(signal.quality):.1f} "
                        f"— <b>{result}</b>"
                    )
                )

            text = "\n".join(lines)

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

    await callback.answer()

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

    await callback.answer()

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

            f"Интервал сканирования: "
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

            if not AUTO_SIGNALS:
                continue

            if not MARKET_READY:

                logger.warning(
                    "Auto scanner skipped: market not ready"
                )

                continue

            users = await get_access_users()

            if not users:
                continue

            for user in users:

                try:

                    user_pair = (
                        getattr(
                            user,
                            "pair",
                            None,
                        )
                        or "ANY"
                    )

                    user_timeframe = int(
                        getattr(
                            user,
                            "timeframe",
                            5,
                        )
                        or 5
                    )

                    if user_pair == "ANY":

                        pairs = [
                            symbol
                            for _, symbol
                            in get_config_pairs()
                        ]

                    else:

                        pairs = [
                            user_pair
                        ]

                    best_signal = None

                    for pair in pairs:

                        try:

                            signal = (
                                await analyze_pair_timeframe(
                                    pair,
                                    user_timeframe,
                                )
                            )

                        except Exception as exc:

                            logger.warning(
                                "Auto analysis failed "
                                "%s/%s: %s",
                                pair,
                                user_timeframe,
                                exc,
                            )

                            continue

                        if signal is None:
                            continue

                        if best_signal is None:

                            best_signal = signal

                        elif (
                            float(signal.quality)
                            >
                            float(best_signal.quality)
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
                            list(LAST_KEYS)[-2500:]
                        )

                    await save_signal(
                        pair=best_signal.pair,
                        timeframe=best_signal.timeframe,
                        direction=best_signal.direction,
                        probability=float(
                            best_signal.probability
                        ),
                        quality=float(
                            best_signal.quality
                        ),
                        entry_time=best_signal.entry_time,
                        close_time=best_signal.close_time,
                        reasons=list(
                            best_signal.reasons
                        ),
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
# MARKET CONNECTOR TASK
# ============================================================

async def market_connector_loop():

    try:

        await connect_market_with_retry()

    except asyncio.CancelledError:

        raise

    except Exception:

        logger.exception(
            "Market connector stopped unexpectedly"
        )


# ============================================================
# FASTAPI SERVER
# ============================================================

async def start_http_server():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    logger.info(
        "Starting HTTP server on 0.0.0.0:%s",
        port,
    )

    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )

    server = uvicorn.Server(
        server_config
    )

    await server.serve()


# ============================================================
# BOT RUNNER
# ============================================================

async def start_telegram():

    logger.info(
        "Starting Telegram polling..."
    )

    await bot.delete_webhook(
        drop_pending_updates=False
    )

    await dp.start_polling(
        bot
    )


# ============================================================
# MAIN
# ============================================================

async def start_bot():

    logger.info(
        "========================================"
    )

    logger.info(
        "POCKET SIGNAL BOT STARTING"
    )

    logger.info(
        "========================================"
    )

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    try:

        await init_db()

        logger.info(
            "Database initialized"
        )

    except Exception:

        logger.exception(
            "Database initialization failed"
        )

        raise

    # --------------------------------------------------------
    # BACKGROUND TASKS
    # --------------------------------------------------------

    tasks: list[asyncio.Task] = []

    try:

        # HTTP запускаем сразу.
        # Это важно для Render health check.

        tasks.append(
            asyncio.create_task(
                start_http_server()
            )
        )

        # Pocket Option подключается отдельно
        # и не блокирует HTTP/Telegram.

        tasks.append(
            asyncio.create_task(
                market_connector_loop()
            )
        )

        tasks.append(
            asyncio.create_task(
                auto_scanner_loop()
            )
        )

        tasks.append(
            asyncio.create_task(
                result_checker_loop()
            )
        )

        # Telegram polling — основная задача.

        await start_telegram()

    except asyncio.CancelledError:

        logger.info(
            "Shutdown requested"
        )

        raise

    except Exception:

        logger.exception(
            "Bot crashed"
        )

        raise

    finally:

        logger.info(
            "Stopping background tasks..."
        )

        for task in tasks:

            if not task.done():

                task.cancel()

        if tasks:

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

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

        logger.info(
            "POCKET SIGNAL BOT STOPPED"
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

        logger.info(
            "Stopped by keyboard interrupt"
        )
