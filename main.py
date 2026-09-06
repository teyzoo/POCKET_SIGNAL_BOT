from __future__ import annotations

import asyncio
import logging
import os

from datetime import datetime, timezone
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
    get_recent_signals,
    get_signal_stats,
    get_user,
    get_user_stats,
    init_db,
    save_signal,
    set_signal_result,
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
                "[MARKET] Подключение..."
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
# TIME FORMAT
# ============================================================

def msk_time(
    value: datetime,
) -> str:

    if value.tzinfo is None:

        value = value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        MSK
    ).strftime(
        "%H:%M"
    )


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(
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
        f"{msk_time(signal.close_time)} МСК"
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
# SIGNAL PAIR KEYBOARD
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

    user = await ensure_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

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

        await callback.message.edit_text(
            (
                "📈 <b>ВЫБЕРИТЕ ПАРУ</b>\n\n"
                "Выберите конкретную OTC-пару "
                "или автоматический поиск."
            ),
            reply_markup=signal_pair_keyboard(),
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

    if callback.message:

        await callback.message.edit_text(
            (
                "⏱ <b>ВЫБЕРИТЕ ЭКСПИРАЦИЮ</b>\n\n"
                f"💱 Пара: "
                f"{'Любая' if pair == 'ANY' else pair_name(pair)}\n\n"
                "Вход выполняется ПО ЗАЯВКЕ."
            ),
            reply_markup=signal_time_keyboard(),
        )

        # Сохраняем выбранную пару в callback message
        callback.message._signal_pair = pair


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

    lock = get_user_lock(
        user_id
    )

    async with lock:

        await callback.message.edit_text(
            (
                "🔎 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                "⏳ Получаю актуальные свечи...\n\n"
                "📊 Анализирую технические условия."
            )
        )

        try:

            if not await ensure_market_ready():

                await callback.message.edit_text(
                    (
                        "❌ <b>РЫНОК НЕДОСТУПЕН</b>\n\n"
                        "Не удалось подключиться "
                        "к рыночному источнику."
                    ),
                    reply_markup=main_keyboard(),
                )

                return

            user = await get_user(
                user_id
            )

            selected_pair = getattr(
                callback.message,
                "_signal_pair",
                "ANY",
            )

            if user is not None:

                selected_pair = (
                    user.pair
                    if user.pair != "ANY"
                    else selected_pair
                )

            # ------------------------------------------------
            # TIMEFRAME
            # ------------------------------------------------

            if timeframe is None:

                if user is not None:

                    timeframe = int(
                        user.timeframe
                    )

                else:

                    timeframe = 5

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

            found_signal = None

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            for pair in pairs:

                try:

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

                        continue

                    signal = engine.analyze(
                        pair,
                        timeframe,
                        candles,
                    )

                    if signal is None:

                        continue

                    if (
                        found_signal is None
                        or signal.quality
                        > found_signal.quality
                    ):

                        found_signal = signal

                except Exception as exc:

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

                await callback.message.edit_text(
                    (
                        "⚪ <b>СИЛЬНОГО СИГНАЛА СЕЙЧАС НЕТ.</b>\n\n"
                        f"💱 {selected_pair}\n"
                        f"⏱ Экспирация: {timeframe} мин\n\n"
                        "Я не буду выдавать слабый сигнал "
                        "только ради того, чтобы что-то показать."
                    ),
                    reply_markup=main_keyboard(),
                )

                return

            # ------------------------------------------------
            # SAVE
            # ------------------------------------------------

            # Цена входа берётся из последней закрытой свечи.
            # Это фактическая цена, используемая для будущего
            # определения WIN/LOSS.

            candles = await market.candles(
                found_signal.pair,
                limit=5,
            )

            entry_price = None

            if candles:

                entry_price = float(
                    candles[-1].close
                )

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

            LAST_KEYS.add(
                f"signal:{signal_id}"
            )

            # ------------------------------------------------
            # SEND
            # ------------------------------------------------

            text = format_signal(
                found_signal
            )

            await callback.message.edit_text(
                text,
                reply_markup=main_keyboard(),
            )

        except Exception as exc:

            logger.exception(
                "[SIGNAL] Ошибка анализа: %s",
                exc,
            )

            await callback.message.edit_text(
                (
                    "❌ <b>ОШИБКА АНАЛИЗА</b>\n\n"
                    f"<code>{str(exc)[:500]}</code>"
                ),
                reply_markup=main_keyboard(),
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

    await callback.message.edit_text(
        (
            "⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"💱 Пара: {user.pair}\n"
            f"⏱ Таймфрейм: {user.timeframe} мин\n"
            f"🤖 Автосигналы: "
            f"{'ВКЛ' if user.auto_signals else 'ВЫКЛ'}"
        ),
        reply_markup=main_keyboard(),
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

    await safe_callback_answer(
        callback
    )

    user = await get_user(
        callback.from_user.id
    )

    if user is None:
        return

    await update_user(
        callback.from_user.id,
        auto_signals=not user.auto_signals,
    )

    await callback.message.edit_text(
        (
            "🤖 <b>Автосигналы</b>\n\n"
            f"Статус: "
            f"{'🟢 ВКЛЮЧЕНЫ' if not user.auto_signals else '🔴 ВЫКЛЮЧЕНЫ'}"
        ),
        reply_markup=main_keyboard(),
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

    if callback.message:

        await callback.message.edit_text(
            (
                "📈 <b>POCKET SIGNAL BOT</b>\n\n"
                "Выберите действие:"
            ),
            reply_markup=main_keyboard(),
        )


# ============================================================
# HONEST WINRATE
# ============================================================

async def get_winrate_text():

    stats = await get_signal_stats()

    decided = stats[
        "decided"
    ]

    wins = stats[
        "wins"
    ]

    losses = stats[
        "losses"
    ]

    winrate = stats[
        "winrate"
    ]

    if decided < 30:

        return (
            "📊 <b>РЕАЛЬНЫЙ WINRATE</b>\n\n"
            f"✅ WIN: {wins}\n"
            f"❌ LOSS: {losses}\n"
            f"📦 Закрыто: {decided}\n\n"
            "⏳ Недостаточно статистики.\n"
            "Winrate будет показан после "
            "накопления 30 закрытых сигналов."
        )

    return (
        "📊 <b>РЕАЛЬНЫЙ WINRATE</b>\n\n"
        f"✅ WIN: {wins}\n"
        f"❌ LOSS: {losses}\n"
        f"📦 Закрыто: {decided}\n\n"
        f"🎯 <b>WINRATE: {winrate:.1f}%</b>\n\n"
        "Расчёт выполнен только по "
        "фактически закрытым сигналам."
    )


# ============================================================
# SETTLE PENDING SIGNALS
# ============================================================

async def settle_pending_signals():

    """
    Периодическая проверка завершённых сигналов.

    Здесь специально НЕ используется probability.

    Результат определяется только по:
        entry_price
        close_price
        direction
    """

    while True:

        try:

            if await ensure_market_ready():

                pending = (
                    await get_pending_signals()
                )

                now = datetime.now(
                    timezone.utc
                )

                for signal in pending:

                    if signal.close_time > now:
                        continue

                    if signal.entry_price is None:
                        continue

                    try:

                        candles = (
                            await market.candles(
                                signal.pair,
                                limit=3,
                            )
                        )

                        if not candles:
                            continue

                        close_price = float(
                            candles[-1].close
                        )

                        entry_price = float(
                            signal.entry_price
                        )

                        if (
                            close_price
                            == entry_price
                        ):
                            continue

                        if signal.direction.upper() == "UP":

                            result = (
                                "WIN"
                                if close_price
                                > entry_price
                                else "LOSS"
                            )

                        elif signal.direction.upper() == "DOWN":

                            result = (
                                "WIN"
                                if close_price
                                < entry_price
                                else "LOSS"
                            )

                        else:

                            continue

                        await set_signal_result(
                            signal.id,
                            result,
                            close_price=close_price,
                        )

                        logger.info(
                            "[RESULT] signal=%s pair=%s "
                            "direction=%s entry=%s close=%s result=%s",
                            signal.id,
                            signal.pair,
                            signal.direction,
                            entry_price,
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

    await bot.session.close()


# ============================================================
# RUN BOT
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
# WEB SERVER
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
