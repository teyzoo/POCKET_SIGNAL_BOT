from __future__ import annotations

import asyncio
import logging
import os
import time

from datetime import datetime, timedelta, timezone
from typing import Optional

from zoneinfo import ZoneInfo

import uvicorn

from aiogram import (
    Bot,
    Dispatcher,
    F,
)

from aiogram.client.default import (
    DefaultBotProperties,
)

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
    get_pending_signals,
    get_signal_stats,
    get_user,
    init_db,
    save_signal,
    settle_signal_by_price,
    update_user,
)

from market import PocketMarket
from signals import SignalEngine


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "POCKET_SIGNAL_BOT"
)


# ============================================================
# TIMEZONE
# ============================================================

MSK = ZoneInfo(
    "Europe/Moscow"
)


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

USER_ANALYSIS_LOCKS: dict[
    int,
    asyncio.Lock,
] = {}

USER_SELECTED_PAIRS: dict[
    int,
    str,
] = {}


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
        "market_connected": market_is_connected(),
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected": market_is_connected(),
        "time": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# MARKET
# ============================================================

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


async def ensure_market_ready() -> bool:

    global MARKET_READY

    if market_is_connected():

        MARKET_READY = True

        return True

    async with MARKET_CONNECT_LOCK:

        if market_is_connected():

            MARKET_READY = True

            return True

        try:

            logger.info(
                "[MARKET] Подключение к источнику рынка..."
            )

            connected = await market.connect()

            if not connected:

                MARKET_READY = False

                return False

            if not market_is_connected():

                MARKET_READY = False

                return False

            MARKET_READY = True

            logger.info(
                "[MARKET] ✅ Market connected"
            )

            return True

        except Exception as exc:

            MARKET_READY = False

            logger.exception(
                "[MARKET] Connection error: %s",
                exc,
            )

            return False


# ============================================================
# CONFIG
# ============================================================

def get_config_pairs():

    pairs = getattr(
        config,
        "pairs",
        None,
    )

    if not pairs:

        raise RuntimeError(
            "В config.py отсутствует pairs."
        )

    return list(pairs)


def get_config_timeframes():

    values = getattr(
        config,
        "timeframes",
        None,
    )

    if not values:

        raise RuntimeError(
            "В config.py отсутствует timeframes."
        )

    result = []

    for value in values:

        try:

            timeframe = int(value)

        except (
            TypeError,
            ValueError,
        ):

            continue

        if timeframe > 0:

            result.append(
                timeframe
            )

    if not result:

        raise RuntimeError(
            "В config.py нет корректных timeframes."
        )

    return result


def pair_name(
    symbol: str,
) -> str:

    for name, internal in get_config_pairs():

        if internal == symbol:

            return name

    return symbol


# ============================================================
# DIRECTION
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
# TIME
# ============================================================

def utc_time(
    value: datetime,
) -> datetime:

    if value.tzinfo is None:

        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


def msk_time(
    value: datetime,
) -> str:

    return utc_time(
        value
    ).astimezone(
        MSK
    ).strftime(
        "%H:%M"
    )


# ============================================================
# CLOSE PRICE FROM M1
# ============================================================

def candle_close_at_or_before(
    candles,
    target_time: datetime,
) -> float | None:

    if not candles:

        return None

    target_time = utc_time(
        target_time
    )

    valid = []

    for candle in candles:

        candle_start = utc_time(
            candle.time
        )

        candle_close = (
            candle_start
            + timedelta(
                minutes=1
            )
        )

        if candle_close <= target_time:

            valid.append(
                candle
            )

    if not valid:

        return None

    candle = max(
        valid,
        key=lambda x: utc_time(
            x.time
        ),
    )

    return float(
        candle.close
    )


# ============================================================
# SIGNAL FORMAT
# ============================================================

async def pair_history_text(
    pair: str,
) -> str:

    try:

        stats = await get_pair_stats()

        for item in stats:

            if item["pair"] != pair:

                continue

            decided = (
                item["wins"]
                + item["losses"]
            )

            if decided <= 0:

                break

            return (
                f"📊 <b>История пары:</b> "
                f"{item['winrate']:.1f}% "
                f"({item['wins']} WIN / "
                f"{item['losses']} LOSS)"
            )

    except Exception as exc:

        logger.warning(
            "[STATS] %s",
            exc,
        )

    return (
        "📊 <b>История пары:</b> "
        "недостаточно закрытых сигналов"
    )


async def format_signal(
    signal,
) -> str:

    reasons = (
        getattr(
            signal,
            "reasons",
            None,
        )
        or []
    )

    reasons_text = "\n".join(
        f"• {reason}"
        for reason in reasons[:8]
    )

    history = await pair_history_text(
        signal.pair
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

        "🕐 <b>Вход:</b> ПО ЗАЯВКЕ\n"

        f"⏰ <b>Закрытие:</b> "
        f"{msk_time(signal.close_time)} МСК\n\n"

        f"{history}"
    )

    if reasons_text:

        text += (
            "\n\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{reasons_text}"
        )

    text += (
        "\n\n"
        "⚠️ Техническая оценка не является "
        "гарантией результата."
    )

    return text


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard():

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


# ============================================================
# PAIR KEYBOARD
# ============================================================

def signal_pair_keyboard():

    rows = [
        [
            InlineKeyboardButton(
                text="🌐 ЛЮБАЯ ПАРА",
                callback_data="sigp:ANY",
            )
        ]
    ]

    row = []

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


# ============================================================
# TIME KEYBOARD
# ============================================================

def signal_time_keyboard():

    rows = []
    row = []

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
# SAFE TELEGRAM
# ============================================================

async def safe_callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
):

    try:

        await callback.answer(
            text=text,
            show_alert=show_alert,
        )

    except Exception as exc:

        logger.debug(
            "Callback error: %s",
            exc,
        )


async def safe_edit(
    message: Message,
    text: str,
    reply_markup=None,
):

    try:

        await message.edit_text(
            text,
            reply_markup=reply_markup,
        )

    except Exception as exc:

        if (
            "message is not modified"
            not in str(exc).lower()
        ):

            logger.debug(
                "[TELEGRAM] edit error: %s",
                exc,
            )


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

        USER_ANALYSIS_LOCKS[
            user_id
        ] = lock

    return lock


# ============================================================
# START
# ============================================================

@dp.message(
    CommandStart()
)
async def start_handler(
    message: Message,
):

    await ensure_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    USER_SELECTED_PAIRS[
        message.from_user.id
    ] = "ANY"

    await message.answer(
        (
            "📈 <b>POCKET SIGNAL BOT</b>\n\n"
            "Добро пожаловать.\n\n"
            "Выберите действие:"
        ),
        reply_markup=main_keyboard(),
    )


# ============================================================
# SIGNAL BUTTON
# ============================================================

@dp.callback_query(
    F.data == "signal"
)
async def signal_button(
    callback: CallbackQuery,
):

    await safe_callback_answer(
        callback
    )

    if callback.message:

        await safe_edit(
            callback.message,
            (
                "📈 <b>ВЫБЕРИТЕ ПАРУ</b>\n\n"
                "Выберите конкретную OTC-пару "
                "или автоматический поиск."
            ),
            signal_pair_keyboard(),
        )


# ============================================================
# PAIR SELECTION
# ============================================================

@dp.callback_query(
    F.data.startswith("sigp:")
)
async def signal_pair(
    callback: CallbackQuery,
):

    await safe_callback_answer(
        callback
    )

    if not callback.message:

        return

    user_id = callback.from_user.id

    selected_pair = (
        callback.data.split(
            ":",
            1
        )[1]
    )

    if selected_pair == "ANY":

        USER_SELECTED_PAIRS[
            user_id
        ] = "ANY"

        pair_text = (
            "🌐 <b>ЛЮБАЯ ПАРА</b>"
        )

    else:

        USER_SELECTED_PAIRS[
            user_id
        ] = selected_pair

        pair_text = (
            f"💱 <b>{pair_name(selected_pair)}</b>"
        )

    await safe_edit(
        callback.message,
        (
            "⏱ <b>ВЫБЕРИТЕ ВРЕМЯ ЭКСПИРАЦИИ</b>\n\n"
            f"{pair_text}\n\n"
            "Можно выбрать конкретную экспирацию "
            "или проверить все доступные варианты."
        ),
        signal_time_keyboard(),
    )


# ============================================================
# SIGNAL SCAN
# ============================================================

async def scan_market(
    pairs: list[str],
    timeframes: list[int],
    message: Message,
) -> object | None:

    total = (
        len(pairs)
        * len(timeframes)
    )

    if total <= 0:

        return None

    checked = 0

    best_signal = None
    best_candles = None

    last_progress_update = 0.0

    async def update_progress(
        pair: str | None = None,
        timeframe: int | None = None,
        stage: str = "Анализ",
        force: bool = False,
    ):

        nonlocal last_progress_update

        now = time.monotonic()

        # Не отправляем Telegram сотни edit подряд.
        if (
            not force
            and now - last_progress_update < 0.8
        ):

            return

        last_progress_update = now

        current = ""

        if pair is not None:

            current += (
                f"💱 <b>Пара:</b> "
                f"{pair_name(pair)}\n"
            )

        if timeframe is not None:

            current += (
                f"⏱ <b>Экспирация:</b> "
                f"{timeframe} мин\n"
            )

        await safe_edit(
            message,
            (
                "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                f"{current}"
                f"⚙️ <b>Этап:</b> {stage}\n\n"
                f"📊 <b>Проверено:</b> "
                f"{checked}/{total}"
            ),
        )

    await update_progress(
        stage="Подключение к рынку...",
        force=True,
    )

    if not await ensure_market_ready():

        await safe_edit(
            message,
            (
                "❌ <b>НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ "
                "К ИСТОЧНИКУ РЫНКА</b>\n\n"
                "Источник рыночных данных сейчас "
                "недоступен.\n\n"
                "Попробуйте ещё раз через несколько секунд."
            ),
        )

        return None

    await update_progress(
        stage="Рынок подключён ✅",
        force=True,
    )

    for pair in pairs:

        await update_progress(
            pair=pair,
            stage="Получение M1-свечей...",
            force=True,
        )

        candles = None

        try:

            candle_limit = int(
                getattr(
                    config,
                    "MARKET_CANDLE_LIMIT",
                    1600,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            candle_limit = 1600

        try:

            candles = await market.candles(
                pair,
                limit=candle_limit,
            )

        except Exception as exc:

            logger.exception(
                "[SCAN] Ошибка свечей %s: %s",
                pair,
                exc,
            )

            candles = None

        if not candles:

            logger.warning(
                "[SCAN] Нет свечей для %s",
                pair,
            )

            checked += len(timeframes)

            await update_progress(
                pair=pair,
                stage="Свечи недоступны",
                force=True,
            )

            continue

        for timeframe in timeframes:

            checked += 1

            await update_progress(
                pair=pair,
                timeframe=timeframe,
                stage="Расчёт индикаторов...",
            )

            try:

                signal = engine.analyze(
                    pair,
                    timeframe,
                    candles,
                )

            except Exception as exc:

                logger.exception(
                    "[SCAN] Ошибка анализа "
                    "%s / %s мин: %s",
                    pair,
                    timeframe,
                    exc,
                )

                signal = None

            if signal is None:

                continue

            try:

                quality = float(
                    getattr(
                        signal,
                        "quality",
                        0.0,
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                quality = 0.0

            try:

                probability = float(
                    getattr(
                        signal,
                        "probability",
                        0.0,
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                probability = 0.0

            if best_signal is None:

                best_signal = signal
                best_candles = candles

                logger.info(
                    "[SCAN] Первый кандидат: "
                    "%s %s мин quality=%.1f probability=%.1f",
                    pair,
                    timeframe,
                    quality,
                    probability,
                )

            else:

                try:

                    best_quality = float(
                        getattr(
                            best_signal,
                            "quality",
                            0.0,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    best_quality = 0.0

                try:

                    best_probability = float(
                        getattr(
                            best_signal,
                            "probability",
                            0.0,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    best_probability = 0.0

                # Сначала Quality Score.
                # При равном качестве — техническая уверенность.
                if (
                    quality,
                    probability,
                ) > (
                    best_quality,
                    best_probability,
                ):

                    best_signal = signal
                    best_candles = candles

                    logger.info(
                        "[SCAN] Новый лучший кандидат: "
                        "%s %s мин quality=%.1f probability=%.1f",
                        pair,
                        timeframe,
                        quality,
                        probability,
                    )

    await update_progress(
        stage="Анализ завершён",
        force=True,
    )

    return best_signal


# ============================================================
# TIME SELECTION
# ============================================================

@dp.callback_query(
    F.data.startswith("sigt:")
)
async def signal_time(
    callback: CallbackQuery,
):

    await safe_callback_answer(
        callback
    )

    if not callback.message:

        return

    user_id = callback.from_user.id

    timeframe_raw = callback.data.split(
        ":",
        1
    )[1]

    selected_pair = (
        USER_SELECTED_PAIRS.get(
            user_id,
            "ANY",
        )
    )

    # ========================================================
    # ГЛАВНОЕ ИСПРАВЛЕНИЕ
    # ========================================================
    #
    # Раньше:
    #
    #   ANY -> взять user.timeframe
    #
    # Поэтому если у пользователя было сохранено 20,
    # кнопка "ЛЮБОЕ ВРЕМЯ" фактически превращалась
    # в "20 минут".
    #
    # Теперь:
    #
    #   ANY -> ВСЕ timeframe из config.timeframes
    #
    # ========================================================

    if timeframe_raw == "ANY":

        timeframes = get_config_timeframes()

        time_label = (
            "ЛЮБОЕ ВРЕМЯ"
        )

        timeframes_label = ", ".join(
            f"{x} мин"
            for x in timeframes
        )

    else:

        try:

            selected_timeframe = int(
                timeframe_raw
            )

        except (
            TypeError,
            ValueError,
        ):

            await safe_edit(
                callback.message,
                (
                    "❌ <b>Некорректная экспирация.</b>\n\n"
                    "Вернитесь назад и выберите время ещё раз."
                ),
            )

            return

        timeframes = [
            selected_timeframe
        ]

        time_label = (
            f"{selected_timeframe} мин"
        )

        timeframes_label = (
            f"{selected_timeframe} мин"
        )

    # ========================================================
    # PAIRS
    # ========================================================

    if selected_pair == "ANY":

        pairs = [
            symbol
            for _, symbol
            in get_config_pairs()
        ]

        pair_label = "ЛЮБАЯ ПАРА"

    else:

        pairs = [
            selected_pair
        ]

        pair_label = pair_name(
            selected_pair
        )

    total_combinations = (
        len(pairs)
        * len(timeframes)
    )

    logger.info(
        "[SIGNAL] User %s requested: "
        "pair=%s timeframes=%s combinations=%s",
        user_id,
        pair_label,
        timeframes,
        total_combinations,
    )

    lock = get_user_lock(
        user_id
    )

    async with lock:

        # ----------------------------------------------------
        # INITIAL SCREEN
        # ----------------------------------------------------

        await safe_edit(
            callback.message,
            (
                "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                f"💱 <b>Пара:</b> {pair_label}\n"
                f"⏱ <b>Экспирация:</b> {time_label}\n"
                f"📋 <b>Варианты:</b> "
                f"{timeframes_label}\n\n"
                f"📊 <b>Комбинаций:</b> "
                f"{total_combinations}\n\n"
                "🔌 Подключение к рынку..."
            ),
        )

        # ----------------------------------------------------
        # SCAN
        # ----------------------------------------------------

        try:

            signal = await scan_market(
                pairs=pairs,
                timeframes=timeframes,
                message=callback.message,
            )

        except Exception as exc:

            logger.exception(
                "[SIGNAL] Scan error: %s",
                exc,
            )

            await safe_edit(
                callback.message,
                (
                    "❌ <b>ОШИБКА АНАЛИЗА</b>\n\n"
                    f"<code>{str(exc)[:1000]}</code>\n\n"
                    "Попробуйте повторить проверку."
                ),
            )

            return

        # ----------------------------------------------------
        # NO SIGNAL
        # ----------------------------------------------------

        if signal is None:

            if timeframe_raw == "ANY":

                checked_timeframes = (
                    timeframes_label
                )

            else:

                checked_timeframes = (
                    f"{timeframes[0]} мин"
                )

            await safe_edit(
                callback.message,
                (
                    "⚪ <b>СИЛЬНОГО СИГНАЛА "
                    "СЕЙЧАС НЕТ</b>\n\n"
                    f"💱 <b>Пара:</b> {pair_label}\n"
                    f"⏱ <b>Проверены экспирации:</b> "
                    f"{checked_timeframes}\n\n"
                    "Я не буду выдавать слабый сигнал "
                    "только ради того, чтобы что-то показать."
                ),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔄 Проверить снова",
                                callback_data="signal",
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

            return

        # ----------------------------------------------------
        # SAVE SIGNAL
        # ----------------------------------------------------

        try:

            await save_signal(
                signal
            )

        except Exception as exc:

            logger.exception(
                "[SIGNAL] Save error: %s",
                exc,
            )

        # ----------------------------------------------------
        # FINAL SIGNAL
        # ----------------------------------------------------

        text = await format_signal(
            signal
        )

        await safe_edit(
            callback.message,
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔄 Новый сигнал",
                            callback_data="signal",
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
# BACK MAIN
# ============================================================

@dp.callback_query(
    F.data == "back_main"
)
async def back_main(
    callback: CallbackQuery,
):

    await safe_callback_answer(
        callback
    )

    if callback.message:

        await safe_edit(
            callback.message,
            (
                "📈 <b>POCKET SIGNAL BOT</b>\n\n"
                "Выберите действие:"
            ),
            main_keyboard(),
        )


# ============================================================
# AUTO SIGNALS
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

    AUTO_SIGNALS = not AUTO_SIGNALS

    try:

        await update_user(
            callback.from_user.id,
            auto_signals=AUTO_SIGNALS,
        )

    except Exception as exc:

        logger.warning(
            "[AUTO] Failed to save setting: %s",
            exc,
        )

    if callback.message:

        await safe_edit(
            callback.message,
            (
                "📈 <b>POCKET SIGNAL BOT</b>\n\n"
                "Настройки обновлены."
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

    if not callback.message:

        return

    try:

        user = await get_user(
            callback.from_user.id
        )

    except Exception:

        user = None

    if user:

        timeframe = getattr(
            user,
            "timeframe",
            None,
        )

        pair = getattr(
            user,
            "pair",
            None,
        )

        auto = getattr(
            user,
            "auto_signals",
            AUTO_SIGNALS,
        )

    else:

        timeframe = None
        pair = None
        auto = AUTO_SIGNALS

    pair_display = (
        pair_name(pair)
        if pair
        else "Любая пара"
    )

    timeframe_display = (
        f"{timeframe} мин"
        if timeframe
        else "Не задано"
    )

    await safe_edit(
        callback.message,
        (
            "⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"💱 <b>Пара:</b> {pair_display}\n"
            f"⏱ <b>Время:</b> {timeframe_display}\n"
            f"🔔 <b>Автосигналы:</b> "
            f"{'ВКЛ' if auto else 'ВЫКЛ'}\n\n"
            "Для ручного сигнала используйте "
            "кнопку «📈 Сигнал»."
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📈 Новый сигнал",
                        callback_data="signal",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="back_main",
                    )
                ],
            ]
        ),
    )


# ============================================================
# SETTLE SIGNALS
# ============================================================

async def settle_pending_signals():

    try:

        pending = await get_pending_signals()

    except Exception as exc:

        logger.warning(
            "[SETTLE] Failed to load pending signals: %s",
            exc,
        )

        return

    if not pending:

        return

    for signal in pending:

        try:

            close_time = utc_time(
                signal.close_time
            )

            now = datetime.now(
                timezone.utc
            )

            if now < close_time:

                continue

            candles = await market.candles(
                signal.pair,
                limit=100,
            )

            if not candles:

                continue

            close_price = (
                candle_close_at_or_before(
                    candles,
                    close_time,
                )
            )

            if close_price is None:

                continue

            await settle_signal_by_price(
                signal.id,
                close_price,
            )

            logger.info(
                "[SETTLE] Signal %s settled at %.8f",
                signal.id,
                close_price,
            )

        except Exception as exc:

            logger.exception(
                "[SETTLE] Signal %s error: %s",
                getattr(
                    signal,
                    "id",
                    "?",
                ),
                exc,
            )


# ============================================================
# AUTO SIGNAL LOOP
# ============================================================

async def auto_signal_loop():

    logger.info(
        "[AUTO] Auto signal loop started"
    )

    while True:

        try:

            if AUTO_SIGNALS:

                users = await get_access_users()

                if users:

                    pairs = get_config_pairs()

                    timeframes = get_config_timeframes()

                    # Автоматический режим использует
                    # конфигурационный набор.
                    #
                    # Берём лучший сигнал среди пар
                    # и доступных экспираций.

                    pair_symbols = [
                        symbol
                        for _, symbol
                        in pairs
                    ]

                    # Используем отдельный технический
                    # поиск без Telegram message.
                    #
                    # Здесь не вызываем scan_market(),
                    # потому что ему нужен Message.

                    best_signal = None

                    for pair in pair_symbols:

                        try:

                            candle_limit = int(
                                getattr(
                                    config,
                                    "MARKET_CANDLE_LIMIT",
                                    1600,
                                )
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            candle_limit = 1600

                        try:

                            candles = await market.candles(
                                pair,
                                limit=candle_limit,
                            )

                        except Exception as exc:

                            logger.warning(
                                "[AUTO] candles %s: %s",
                                pair,
                                exc,
                            )

                            continue

                        if not candles:

                            continue

                        for timeframe in timeframes:

                            try:

                                candidate = engine.analyze(
                                    pair,
                                    timeframe,
                                    candles,
                                )

                            except Exception as exc:

                                logger.warning(
                                    "[AUTO] analyze %s/%s: %s",
                                    pair,
                                    timeframe,
                                    exc,
                                )

                                continue

                            if candidate is None:

                                continue

                            if best_signal is None:

                                best_signal = candidate

                                continue

                            try:

                                candidate_key = (
                                    float(
                                        getattr(
                                            candidate,
                                            "quality",
                                            0.0,
                                        )
                                    ),
                                    float(
                                        getattr(
                                            candidate,
                                            "probability",
                                            0.0,
                                        )
                                    ),
                                )

                            except Exception:

                                candidate_key = (
                                    0.0,
                                    0.0,
                                )

                            try:

                                best_key = (
                                    float(
                                        getattr(
                                            best_signal,
                                            "quality",
                                            0.0,
                                        )
                                    ),
                                    float(
                                        getattr(
                                            best_signal,
                                            "probability",
                                            0.0,
                                        )
                                    ),
                                )

                            except Exception:

                                best_key = (
                                    0.0,
                                    0.0,
                                )

                            if candidate_key > best_key:

                                best_signal = candidate

                    if best_signal is not None:

                        try:

                            await save_signal(
                                best_signal
                            )

                        except Exception as exc:

                            logger.warning(
                                "[AUTO] save error: %s",
                                exc,
                            )

                        try:

                            text = await format_signal(
                                best_signal
                            )

                        except Exception as exc:

                            logger.warning(
                                "[AUTO] format error: %s",
                                exc,
                            )

                            text = None

                        if text:

                            for user in users:

                                try:

                                    telegram_id = int(
                                        getattr(
                                            user,
                                            "telegram_id",
                                            0,
                                        )
                                    )

                                    if not telegram_id:

                                        continue

                                    await bot.send_message(
                                        telegram_id,
                                        text,
                                    )

                                except Exception as exc:

                                    logger.warning(
                                        "[AUTO] send error: %s",
                                        exc,
                                    )

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.exception(
                "[AUTO] Loop error: %s",
                exc,
            )

        await asyncio.sleep(
            60
        )


# ============================================================
# SETTLEMENT LOOP
# ============================================================

async def settlement_loop():

    logger.info(
        "[SETTLE] Settlement loop started"
    )

    while True:

        try:

            if market_is_connected():

                await settle_pending_signals()

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.exception(
                "[SETTLE] Loop error: %s",
                exc,
            )

        await asyncio.sleep(
            30
        )


# ============================================================
# STARTUP
# ============================================================

async def on_startup():

    logger.info(
        "🚀 POCKET_SIGNAL_BOT starting..."
    )

    try:

        await init_db()

        logger.info(
            "✅ Database initialized"
        )

    except Exception as exc:

        logger.exception(
            "❌ Database initialization failed: %s",
            exc,
        )

    try:

        await ensure_market_ready()

    except Exception as exc:

        logger.exception(
            "Market startup error: %s",
            exc,
        )


# ============================================================
# SHUTDOWN
# ============================================================

async def on_shutdown():

    logger.info(
        "🛑 POCKET_SIGNAL_BOT shutting down..."
    )

    try:

        await close_database()

    except Exception as exc:

        logger.warning(
            "Database shutdown error: %s",
            exc,
        )


# ============================================================
# BOT RUNNER
# ============================================================

async def run_bot():

    await on_startup()

    auto_task = asyncio.create_task(
        auto_signal_loop()
    )

    settlement_task = asyncio.create_task(
        settlement_loop()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        auto_task.cancel()
        settlement_task.cancel()

        await asyncio.gather(
            auto_task,
            settlement_task,
            return_exceptions=True,
        )

        await on_shutdown()


# ============================================================
# WEB SERVER
# ============================================================

def run_web():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


# ============================================================
# ENTRYPOINT
# ============================================================

async def main():

    await run_bot()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
