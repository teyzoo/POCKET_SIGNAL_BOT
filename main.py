from __future__ import annotations

import asyncio
import logging
import os

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

# Главное исправление:
# pair больше НЕ хранится внутри Telegram Message.
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

        # Берём только полностью закрытую M1-свечу,
        # которая завершилась не позже целевого момента.
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
# CALLBACK
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

        # Telegram возвращает ошибку, если текст не изменился.
        if "message is not modified" not in str(exc).lower():

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
# PAIR
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

    pair = callback.data.split(
        ":",
        1,
    )[1]

    USER_SELECTED_PAIRS[
        callback.from_user.id
    ] = pair

    if callback.message:

        await safe_edit(
            callback.message,
            (
                "⏱ <b>ВЫБЕРИТЕ ЭКСПИРАЦИЮ</b>\n\n"
                f"💱 Пара: "
                f"{'ЛЮБАЯ' if pair == 'ANY' else pair_name(pair)}\n\n"
                "Вход выполняется ПО ЗАЯВКЕ."
            ),
            signal_time_keyboard(),
        )


# ============================================================
# TIME
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

    timeframe_raw = callback.data.split(
        ":",
        1,
    )[1]

    if not callback.message:
        return

    timeframe = (
        None
        if timeframe_raw == "ANY"
        else int(timeframe_raw)
    )

    user_id = callback.from_user.id

    selected_pair = USER_SELECTED_PAIRS.get(
        user_id,
        "ANY",
    )

    lock = get_user_lock(
        user_id
    )

    async with lock:

        if timeframe is None:

            user = await get_user(
                user_id
            )

            timeframe = (
                int(user.timeframe)
                if user is not None
                else 5
            )

        if selected_pair == "ANY":

            pair_label = "ЛЮБАЯ"

        else:

            pair_label = pair_name(
                selected_pair
            )

        await safe_edit(
            callback.message,
            (
                "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                f"💱 Пара: {pair_label}\n"
                f"⏱ Экспирация: {timeframe} мин\n\n"
                "🔌 Подключение к рынку... ⏳\n"
                "📥 Получение свечей... ⏳\n"
                "📊 Проверка закрытых свечей... ⏳\n"
                "🧮 Расчёт индикаторов... ⏳\n"
                "🎯 Поиск сильного сигнала... ⏳"
            )
        )

        try:

            # ------------------------------------------------
            # CONNECT
            # ------------------------------------------------

            if not await ensure_market_ready():

                await safe_edit(
                    callback.message,
                    (
                        "❌ <b>РЫНОК НЕДОСТУПЕН</b>\n\n"
                        "Не удалось подключиться "
                        "к источнику рыночных данных."
                    ),
                    main_keyboard(),
                )

                return

            # ------------------------------------------------
            # PAIRS
            # ------------------------------------------------

            if selected_pair == "ANY":

                pairs = [
                    symbol
                    for _, symbol
                    in get_config_pairs()
                ]

            else:

                pairs = [
                    selected_pair
                ]

            total = len(pairs)
            checked = 0

            found_signal = None
            found_candles = None

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            for pair in pairs:

                try:

                    await safe_edit(
                        callback.message,
                        (
                            "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                            f"💱 Пара: {pair_name(pair)}\n"
                            f"⏱ Экспирация: {timeframe} мин\n\n"
                            "🔌 Подключение к рынку... ✅\n"
                            f"📥 Получение свечей... ⏳\n\n"
                            f"📊 Проверено: {checked}/{total}"
                        )
                    )

                    candles = await market.candles(
                        pair,
                        limit=int(
                            getattr(
                                config,
                                "MARKET_CANDLE_LIMIT",
                                1600,
                            )
                        ),
                    )

                    if not candles:

                        checked += 1
                        continue

                    await safe_edit(
                        callback.message,
                        (
                            "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                            f"💱 Пара: {pair_name(pair)}\n"
                            f"⏱ Экспирация: {timeframe} мин\n\n"
                            "🔌 Подключение к рынку... ✅\n"
                            "📥 Получение свечей... ✅\n"
                            "📊 Проверка закрытых свечей... ⏳\n"
                            f"\n📊 Проверено: {checked}/{total}"
                        )
                    )

                    signal = engine.analyze(
                        pair,
                        timeframe,
                        candles,
                    )

                    checked += 1

                    if signal is None:

                        await safe_edit(
                            callback.message,
                            (
                                "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                                f"💱 Последняя пара: "
                                f"{pair_name(pair)}\n"
                                f"⏱ Экспирация: {timeframe} мин\n\n"
                                "📊 Проверка закрытых свечей... ✅\n"
                                "🧮 Расчёт индикаторов... ✅\n"
                                "🎯 Сильного сигнала на этой паре нет.\n\n"
                                f"📊 Проверено: {checked}/{total}"
                            )
                        )

                        continue

                    # Выбираем самый качественный сигнал.
                    if (
                        found_signal is None
                        or signal.quality
                        > found_signal.quality
                    ):

                        found_signal = signal
                        found_candles = candles

                except Exception as exc:

                    checked += 1

                    logger.exception(
                        "[SIGNAL] %s error: %s",
                        pair,
                        exc,
                    )

                    continue

            # ------------------------------------------------
            # NO SIGNAL
            # ------------------------------------------------

            if found_signal is None:

                await safe_edit(
                    callback.message,
                    (
                        "⚪ <b>СИЛЬНОГО СИГНАЛА СЕЙЧАС НЕТ.</b>\n\n"
                        f"💱 {pair_label}\n"
                        f"⏱ Экспирация: {timeframe} мин\n\n"
                        f"📊 Проверено пар: {checked}/{total}\n\n"
                        "Слабый сигнал не выдаю — "
                        "это сделано специально, чтобы "
                        "не искажать статистику."
                    ),
                    main_keyboard(),
                )

                return

            # ------------------------------------------------
            # ENTRY PRICE
            # ------------------------------------------------

            entry_price = (
                getattr(
                    found_signal,
                    "entry_price",
                    None,
                )
            )

            if entry_price is None and found_candles:

                # Резервный способ.
                valid = []

                for candle in found_candles:

                    start = utc_time(
                        candle.time
                    )

                    if (
                        start
                        + timedelta(minutes=1)
                        <= found_signal.entry_time
                    ):

                        valid.append(
                            candle
                        )

                if valid:

                    last = max(
                        valid,
                        key=lambda x: utc_time(
                            x.time
                        ),
                    )

                    entry_price = float(
                        last.close
                    )

            if entry_price is None:

                await safe_edit(
                    callback.message,
                    (
                        "⚠️ <b>СИГНАЛ НЕ СОХРАНЁН</b>\n\n"
                        "Не удалось получить "
                        "надёжную цену входа."
                    ),
                    main_keyboard(),
                )

                return

            # ------------------------------------------------
            # SAVE
            # ------------------------------------------------

            signal_id = await save_signal(
                pair=found_signal.pair,
                timeframe=found_signal.timeframe,
                direction=found_signal.direction,
                probability=found_signal.probability,
                quality=found_signal.quality,
                entry_time=found_signal.entry_time,
                close_time=found_signal.close_time,
                reasons=found_signal.reasons,
                entry_price=entry_price,
            )

            logger.info(
                "[SIGNAL] saved id=%s pair=%s direction=%s "
                "entry=%s close=%s",
                signal_id,
                found_signal.pair,
                found_signal.direction,
                entry_price,
                found_signal.close_time,
            )

            # ------------------------------------------------
            # SEND
            # ------------------------------------------------

            text = await format_signal(
                found_signal
            )

            await safe_edit(
                callback.message,
                text,
                main_keyboard(),
            )

        except Exception as exc:

            logger.exception(
                "[SIGNAL] Ошибка анализа: %s",
                exc,
            )

            await safe_edit(
                callback.message,
                (
                    "❌ <b>ОШИБКА АНАЛИЗА</b>\n\n"
                    f"<code>{str(exc)[:500]}</code>"
                ),
                main_keyboard(),
            )


# ============================================================
# SETTINGS
# ============================================================

@dp.callback_query(
    F.data == "settings"
)
async def settings_button(
    callback: CallbackQuery,
):

    await safe_callback_answer(
        callback
    )

    user = await get_user(
        callback.from_user.id
    )

    if user is None:

        await callback.message.answer(
            "Пользователь не найден."
        )

        return

    await safe_edit(
        callback.message,
        (
            "⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"💱 Пара: {user.pair}\n"
            f"⏱ Таймфрейм: {user.timeframe} мин\n"
            f"🤖 Автосигналы: "
            f"{'ВКЛ' if user.auto_signals else 'ВЫКЛ'}"
        ),
        main_keyboard(),
    )


# ============================================================
# AUTO
# ============================================================

@dp.callback_query(
    F.data == "auto_toggle"
)
async def auto_toggle(
    callback: CallbackQuery,
):

    await safe_callback_answer(
        callback
    )

    user = await get_user(
        callback.from_user.id
    )

    if user is None:
        return

    new_value = not user.auto_signals

    await update_user(
        callback.from_user.id,
        auto_signals=new_value,
    )

    await safe_edit(
        callback.message,
        (
            "🤖 <b>Автосигналы</b>\n\n"
            f"Статус: "
            f"{'🟢 ВКЛЮЧЕНЫ' if new_value else '🔴 ВЫКЛЮЧЕНЫ'}"
        ),
        main_keyboard(),
    )


# ============================================================
# BACK
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

    USER_SELECTED_PAIRS[
        callback.from_user.id
    ] = "ANY"

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
# WINRATE
# ============================================================

async def get_winrate_text():

    stats = await get_signal_stats()

    decided = stats["decided"]
    wins = stats["wins"]
    losses = stats["losses"]
    winrate = stats["winrate"]

    if decided < 30:

        return (
            "📊 <b>РЕАЛЬНЫЙ WINRATE</b>\n\n"
            f"✅ WIN: {wins}\n"
            f"❌ LOSS: {losses}\n"
            f"📦 Закрыто: {decided}\n\n"
            "⏳ Недостаточно статистики для "
            "надёжной оценки.\n"
            "Ничего не подставляется искусственно."
        )

    return (
        "📊 <b>РЕАЛЬНЫЙ WINRATE</b>\n\n"
        f"✅ WIN: {wins}\n"
        f"❌ LOSS: {losses}\n"
        f"📦 Закрыто: {decided}\n\n"
        f"🎯 <b>WINRATE: {winrate:.1f}%</b>\n\n"
        "Расчёт только по фактически "
        "определённым WIN/LOSS."
    )


# ============================================================
# SETTLEMENT
# ============================================================

async def settle_pending_signals():

    """
    Определяет WIN/LOSS только по фактической цене
    закрытой M1-свечи.

    probability вообще не используется.
    """

    while True:

        try:

            if await ensure_market_ready():

                pending = await get_pending_signals()

                now = datetime.now(
                    timezone.utc
                )

                for signal in pending:

                    close_time = utc_time(
                        signal.close_time
                    )

                    if close_time > now:
                        continue

                    if signal.entry_price is None:
                        continue

                    try:

                        # Берём достаточно большой диапазон,
                        # чтобы найти именно свечу окончания
                        # экспирации.
                        candles = await market.candles(
                            signal.pair,
                            limit=max(
                                100,
                                int(
                                    getattr(
                                        config,
                                        "MARKET_CANDLE_LIMIT",
                                        1600,
                                    )
                                ),
                            ),
                        )

                        if not candles:
                            continue

                        close_price = candle_close_at_or_before(
                            candles,
                            close_time,
                        )

                        if close_price is None:

                            logger.warning(
                                "[RESULT] Нет закрытой M1-свечи "
                                "для signal=%s close=%s",
                                signal.id,
                                close_time,
                            )

                            continue

                        result = await settle_signal_by_price(
                            signal.id,
                            close_price,
                        )

                        if result is None:

                            # Ничья или сигнал ещё нельзя закрыть.
                            continue

                        logger.info(
                            "[RESULT] signal=%s pair=%s "
                            "direction=%s entry=%s "
                            "close=%s result=%s",
                            signal.id,
                            signal.pair,
                            signal.direction,
                            signal.entry_price,
                            close_price,
                            result,
                        )

                    except Exception as exc:

                        logger.exception(
                            "[RESULT] Ошибка signal=%s: %s",
                            signal.id,
                            exc,
                        )

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.exception(
                "[RESULT] Worker error: %s",
                exc,
            )

        await asyncio.sleep(
            10
        )


# ============================================================
# STARTUP
# ============================================================

async def on_startup():

    await init_db()

    logger.info(
        "🚀 POCKET_SIGNAL_BOT запущен"
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def on_shutdown():

    try:

        await market.close()

    except Exception:

        pass

    await close_database()

    try:

        await bot.session.close()

    except Exception:

        pass


# ============================================================
# BOT
# ============================================================

async def bot_runner():

    await on_startup()

    result_worker = asyncio.create_task(
        settle_pending_signals()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        result_worker.cancel()

        try:

            await result_worker

        except asyncio.CancelledError:

            pass

        await on_shutdown()


# ============================================================
# WEB
# ============================================================

async def web_runner():

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
# MAIN
# ============================================================

async def main():

    await asyncio.gather(
        bot_runner(),
        web_runner(),
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )
