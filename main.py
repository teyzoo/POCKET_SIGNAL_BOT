from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

import uvicorn

from fastapi import FastAPI

from aiogram import (
    Bot,
    Dispatcher,
    F,
)

from aiogram.filters import (
    Command,
    CommandStart,
)

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config

from database import (
    User,
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

from market import market

from signals import engine


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
    "pocket_signal_bot"
)


app = FastAPI(
    title="Pocket Signal Bot",
)


@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "POCKET SIGNAL BOT",
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "service": "POCKET SIGNAL BOT",
    }


bot = Bot(
    token=config.bot_token
)

dp = Dispatcher()

AUTO_SIGNALS = True

LAST_KEYS: set[str] = set()


def main_keyboard(
    user: User,
):

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
                    text="💱 Пара",
                    callback_data="pairs",
                ),
                InlineKeyboardButton(
                    text="⏱ Время",
                    callback_data="timeframes",
                ),
            ],

            [
                InlineKeyboardButton(
                    text=(
                        "🟢 Автосигналы: ON"
                        if user.auto_signals
                        else
                        "🔴 Автосигналы: OFF"
                    ),
                    callback_data="toggle_auto",
                ),
            ],
        ]
    )


def owner_keyboard():

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
                    text="📡 Сигналы",
                    callback_data="owner_signals",
                ),
                InlineKeyboardButton(
                    text="📊 Пары",
                    callback_data="owner_pairs",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="▶️ Автосканер",
                    callback_data="owner_auto",
                ),
            ],
        ]
    )


async def get_signal(
    pair: str,
    timeframe: int,
):

    candles = await market.candles(
        pair,
        minutes=timeframe,
        limit=200,
    )

    if not candles:

        return None

    return engine.analyze(
        pair,
        timeframe,
        candles,
    )


async def send_signal(
    telegram_id: int,
    result,
):

    icon = (
        "🟢"
        if result.direction == "UP"
        else "🔴"
    )

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "🚨 <b>СИЛЬНЫЙ OTC СИГНАЛ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"💱 Пара: "
        f"<b>{result.pair}</b>\n"

        f"📌 Направление: "
        f"<b>{icon} {result.direction}</b>\n"

        f"🎯 Вероятность: "
        f"<b>{result.probability:.1f}%</b>\n"

        f"⭐ Quality Score: "
        f"<b>{result.quality:.1f}</b>\n"

        f"⏱ Экспирация: "
        f"<b>{result.timeframe} мин.</b>\n\n"

        f"🕐 Вход: "
        f"<b>{result.entry_time.strftime('%H:%M:%S')}</b>\n"

        f"🕐 Закрытие: "
        f"<b>{result.close_time.strftime('%H:%M:%S')}</b>\n\n"

        "📊 <b>Подтверждения:</b>\n"
    )

    for reason in result.reasons:

        text += (
            f"• {reason}\n"
        )

    text += (
        "\n⚠️ Расчётная вероятность "
        "не является гарантией результата."
    )

    await bot.send_message(
        telegram_id,
        text,
    )


@dp.message(CommandStart())
async def start(
    message: Message,
):

    user = await ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    if user.blocked:

        await message.answer(
            "🚫 Ваш доступ заблокирован."
        )

        return

    await message.answer(
        "🤖 <b>POCKET SIGNAL BOT</b>\n\n"

        "📡 Анализ OTC Pocket Option\n"
        "📊 Технические сигналы\n"
        "🎯 Фильтр сильных ситуаций\n\n"

        f"💱 Пара: "
        f"<b>{user.pair}</b>\n"

        f"⏱ Экспирация: "
        f"<b>{user.timeframe} мин.</b>\n"

        f"📡 Авто: "
        f"<b>{'ON' if user.auto_signals else 'OFF'}</b>",

        reply_markup=main_keyboard(
            user
        ),
    )


@dp.message(Command("owner"))
async def owner(
    message: Message,
):

    if (
        message.from_user.id
        != config.owner_id
    ):

        return

    await message.answer(
        "👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА</b>",
        reply_markup=owner_keyboard(),
    )


@dp.callback_query(
    F.data == "signal"
)
async def manual_signal(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if (
        not user
        or user.blocked
    ):

        await call.answer(
            "Доступ заблокирован.",
            show_alert=True,
        )

        return

    await call.answer(
        "🔎 Анализирую OTC..."
    )

    if user.pair == "ANY":

        best = None

        for pair in config.pairs:

            try:

                result = await get_signal(
                    pair,
                    user.timeframe,
                )

                if result:

                    if (
                        best is None
                        or result.quality
                        > best.quality
                    ):

                        best = result

            except Exception:

                logger.exception(
                    "Ошибка анализа %s",
                    pair,
                )

        result = best

    else:

        result = await get_signal(
            user.pair,
            user.timeframe,
        )

    if not result:

        await call.message.answer(
            "⚪ <b>Сильного OTC-сигнала сейчас нет.</b>\n\n"
            "Я не буду выдавать слабый сигнал "
            "только ради того, чтобы что-то показать."
        )

        return

    await save_signal(
        pair=result.pair,
        timeframe=result.timeframe,
        direction=result.direction,
        probability=result.probability,
        quality=result.quality,
        entry_time=result.entry_time,
        close_time=result.close_time,
        reasons=result.reasons,
    )

    await send_signal(
        call.from_user.id,
        result,
    )


@dp.callback_query(
    F.data == "toggle_auto"
)
async def toggle_auto(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if not user:

        return

    value = not user.auto_signals

    await update_user(
        user.telegram_id,
        auto_signals=value,
    )

    user.auto_signals = value

    await call.message.edit_reply_markup(
        reply_markup=main_keyboard(
            user
        )
    )

    await call.answer(
        "Автосигналы "
        + (
            "включены"
            if value
            else "выключены"
        )
    )


@dp.callback_query(
    F.data == "pairs"
)
async def pairs_menu(
    call: CallbackQuery,
):

    buttons = [

        [
            InlineKeyboardButton(
                text="🌐 Любая пара",
                callback_data="pair_ANY",
            )
        ]
    ]

    row = []

    for pair in config.pairs:

        row.append(
            InlineKeyboardButton(
                text=pair,
                callback_data=(
                    "pair_"
                    + pair
                ),
            )
        )

        if len(row) == 2:

            buttons.append(row)

            row = []

    if row:

        buttons.append(row)

    await call.message.answer(
        "💱 <b>Выбери OTC-пару:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await call.answer()


@dp.callback_query(
    F.data.startswith("pair_")
)
async def choose_pair(
    call: CallbackQuery,
):

    pair = call.data[
        len("pair_"):
    ]

    await update_user(
        call.from_user.id,
        pair=pair,
    )

    await call.answer(
        f"Выбрано: {pair}"
    )


@dp.callback_query(
    F.data == "timeframes"
)
async def timeframes_menu(
    call: CallbackQuery,
):

    buttons = []

    row = []

    for timeframe in config.timeframes:

        row.append(
            InlineKeyboardButton(
                text=(
                    f"{timeframe} мин."
                ),
                callback_data=(
                    f"time_{timeframe}"
                ),
            )
        )

        if len(row) == 3:

            buttons.append(row)

            row = []

    if row:

        buttons.append(row)

    await call.message.answer(
        "⏱ <b>Выбери экспирацию:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await call.answer()


@dp.callback_query(
    F.data.startswith("time_")
)
async def choose_time(
    call: CallbackQuery,
):

    timeframe = int(
        call.data[
            len("time_"):
        ]
    )

    await update_user(
        call.from_user.id,
        timeframe=timeframe,
    )

    await call.answer(
        f"Экспирация: {timeframe} мин."
    )


@dp.callback_query(
    F.data == "settings"
)
async def settings(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if not user:

        return

    await call.message.answer(
        "⚙️ <b>ТЕКУЩИЕ НАСТРОЙКИ</b>\n\n"

        f"💱 Пара: "
        f"<b>{user.pair}</b>\n"

        f"⏱ Экспирация: "
        f"<b>{user.timeframe} мин.</b>\n"

        f"📡 Автосигналы: "
        f"<b>{'ON' if user.auto_signals else 'OFF'}</b>\n"

        f"🎯 Минимальный Quality: "
        f"<b>{config.min_signal_score}%</b>\n"

        f"🎯 Минимальная вероятность: "
        f"<b>{config.min_probability}%</b>"
    )

    await call.answer()


@dp.callback_query(
    F.data == "owner_stats"
)
async def owner_stats(
    call: CallbackQuery,
):

    if (
        call.from_user.id
        != config.owner_id
    ):

        return

    users = await get_user_stats()

    signals = await get_signal_stats()

    await call.message.answer(
        "📊 <b>СТАТИСТИКА</b>\n\n"

        f"👥 Пользователей: "
        f"<b>{users['total']}</b>\n"

        f"🟢 Активных: "
        f"<b>{users['active']}</b>\n\n"

        f"📡 Сигналов: "
        f"<b>{signals['total']}</b>\n"

        f"🟢 WIN: "
        f"<b>{signals['wins']}</b>\n"

        f"🔴 LOSS: "
        f"<b>{signals['losses']}</b>\n"

        f"📈 WINRATE: "
        f"<b>{signals['winrate']:.1f}%</b>"
    )

    await call.answer()


@dp.callback_query(
    F.data == "owner_users"
)
async def owner_users(
    call: CallbackQuery,
):

    if (
        call.from_user.id
        != config.owner_id
    ):

        return

    stats = await get_user_stats()

    await call.message.answer(
        f"👥 Всего пользователей: "
        f"<b>{stats['total']}</b>\n"
        f"🟢 Активных: "
        f"<b>{stats['active']}</b>"
    )

    await call.answer()


@dp.callback_query(
    F.data == "owner_signals"
)
async def owner_signals(
    call: CallbackQuery,
):

    if (
        call.from_user.id
        != config.owner_id
    ):

        return

    rows = await get_recent_signals(
        10
    )

    text = (
        "📡 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>\n\n"
    )

    for row in rows:

        text += (
            f"{row.pair} | "
            f"{row.direction} | "
            f"{row.timeframe}м | "
            f"{row.probability:.1f}% | "
            f"{row.status}\n"
        )

    await call.message.answer(
        text
    )

    await call.answer()


@dp.callback_query(
    F.data == "owner_pairs"
)
async def owner_pairs(
    call: CallbackQuery,
):

    if (
        call.from_user.id
        != config.owner_id
    ):

        return

    rows = await get_pair_stats()

    text = (
        "📊 <b>СТАТИСТИКА ПО ПАРАМ</b>\n\n"
    )

    for pair, count in rows:

        text += (
            f"• {pair}: {count}\n"
        )

    await call.message.answer(
        text
        if rows
        else "Пока нет данных."
    )

    await call.answer()


@dp.callback_query(
    F.data == "owner_auto"
)
async def owner_auto(
    call: CallbackQuery,
):

    global AUTO_SIGNALS

    if (
        call.from_user.id
        != config.owner_id
    ):

        return

    AUTO_SIGNALS = not AUTO_SIGNALS

    await call.answer(
        "Автосканер: "
        + (
            "ON"
            if AUTO_SIGNALS
            else "OFF"
        ),
        show_alert=True,
    )


async def scanner():

    global LAST_KEYS

    while True:

        try:

            if AUTO_SIGNALS:

                users = (
                    await get_access_users()
                )

                requested = set()

                for user in users:

                    pairs = (
                        config.pairs
                        if user.pair == "ANY"
                        else [user.pair]
                    )

                    for pair in pairs:

                        requested.add(
                            (
                                pair,
                                user.timeframe,
                            )
                        )

                for pair, timeframe in requested:

                    try:

                        result = (
                            await get_signal(
                                pair,
                                timeframe,
                            )
                        )

                    except Exception:

                        logger.exception(
                            "Ошибка анализа %s %s",
                            pair,
                            timeframe,
                        )

                        continue

                    if not result:

                        continue

                    key = (
                        f"{pair}|"
                        f"{timeframe}|"
                        f"{result.direction}|"
                        f"{result.entry_time:%Y%m%d%H%M}"
                    )

                    if key in LAST_KEYS:

                        continue

                    LAST_KEYS.add(key)

                    if len(
                        LAST_KEYS
                    ) > 1000:

                        LAST_KEYS = set(
                            list(
                                LAST_KEYS
                            )[-500:]
                        )

                    await save_signal(
                        pair=result.pair,
                        timeframe=result.timeframe,
                        direction=result.direction,
                        probability=result.probability,
                        quality=result.quality,
                        entry_time=result.entry_time,
                        close_time=result.close_time,
                        reasons=result.reasons,
                    )

                    for user in users:

                        if not user.auto_signals:

                            continue

                        if (
                            user.timeframe
                            != timeframe
                        ):

                            continue

                        if (
                            user.pair != "ANY"
                            and user.pair
                            != pair
                        ):

                            continue

                        try:

                            await send_signal(
                                user.telegram_id,
                                result,
                            )

                        except Exception:

                            logger.exception(
                                "Не удалось отправить сигнал"
                            )

        except Exception:

            logger.exception(
                "Ошибка сканера"
            )

        await asyncio.sleep(
            max(
                10,
                config.scan_interval,
            )
        )


async def result_loop():

    while True:

        try:

            await mark_expired_signals()

        except Exception:

            logger.exception(
                "Ошибка result loop"
            )

        await asyncio.sleep(30)


async def main():

    await init_db()

    logger.info(
        "POCKET SIGNAL BOT started"
    )

    scanner_task = asyncio.create_task(
        scanner()
    )

    result_task = asyncio.create_task(
        result_loop()
    )

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

    server_task = asyncio.create_task(
        server.serve()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        scanner_task.cancel()
        result_task.cancel()
        server_task.cancel()

        with suppress(
            asyncio.CancelledError
        ):

            await scanner_task

        with suppress(
            asyncio.CancelledError
        ):

            await result_task

        with suppress(
            asyncio.CancelledError
        ):

            await server_task

        with suppress(Exception):

            await market.close()

        with suppress(Exception):

            await bot.session.close()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
