from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
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
    get_pending_requests,
    get_recent_signals,
    get_signal_stats,
    get_user,
    get_user_stats,
    init_db,
    mark_expired_signals,
    save_join_request,
    save_signal,
    set_join_request_status,
    update_user,
)
from market import market
from signals import engine


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("pocket_signal_bot")


# ============================================================
# FASTAPI / RENDER HEALTH SERVER
# ============================================================

app = FastAPI(
    title="Pocket Signal Bot",
    version="1.0.0",
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
        "telegram": "running",
    }


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=config.bot_token)
dp = Dispatcher()


# ============================================================
# GLOBAL STATE
# ============================================================

AUTO_SIGNALS = True

LAST_SIGNAL_KEYS: set[str] = set()


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard(user: User):

    access_text = (
        "🟢 Доступ открыт"
        if user.access
        else "🔐 Получить доступ"
    )

    auto_text = (
        "🟢 Автосигналы: ON"
        if user.auto_signals
        else "🔴 Автосигналы: OFF"
    )

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
                    text=auto_text,
                    callback_data="toggle_auto",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=access_text,
                    callback_data="access",
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
                    text="🔐 Заявки",
                    callback_data="owner_requests",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="▶️ Автосигналы",
                    callback_data="owner_auto",
                ),
                InlineKeyboardButton(
                    text="🧹 Последние",
                    callback_data="owner_recent",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Пары",
                    callback_data="owner_pairs",
                ),
            ],
        ]
    )


# ============================================================
# SIGNAL MESSAGE
# ============================================================

async def send_signal_to_user(
    telegram_id: int,
    result,
):

    direction_icon = (
        "🟢"
        if result.direction == "UP"
        else "🔴"
    )

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "🚨 <b>СИЛЬНЫЙ СИГНАЛ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"💱 Пара: <b>{result.pair}</b>\n"
        f"📌 Направление: "
        f"<b>{direction_icon} {result.direction}</b>\n"
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
        text += f"• {reason}\n"

    text += (
        "\n⚠️ Вероятность является расчётной оценкой, "
        "а не гарантией результата."
    )

    try:

        await bot.send_message(
            telegram_id,
            text,
        )

        return True

    except Exception as exc:

        logger.warning(
            "Cannot send signal to %s: %s",
            telegram_id,
            exc,
        )

        return False


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

async def get_signal_for(
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


# ============================================================
# AUTOMATIC SCANNER
# ============================================================

async def scan_once():

    global LAST_SIGNAL_KEYS

    if not AUTO_SIGNALS:
        return

    users = await get_access_users()

    if not users:
        return

    requested: set[tuple[str, int]] = set()

    for user in users:

        pair_list = (
            config.pairs
            if user.pair == "ANY"
            else [user.pair]
        )

        for pair in pair_list:

            requested.add(
                (
                    pair,
                    user.timeframe,
                )
            )

    for pair, timeframe in requested:

        try:

            result = await get_signal_for(
                pair,
                timeframe,
            )

        except Exception:

            logger.exception(
                "Signal analysis error: %s %s",
                pair,
                timeframe,
            )

            continue

        if not result:
            continue

        minute_key = (
            f"{pair}:"
            f"{timeframe}:"
            f"{result.direction}:"
            f"{result.entry_time.strftime('%Y%m%d%H%M')}"
        )

        if minute_key in LAST_SIGNAL_KEYS:
            continue

        LAST_SIGNAL_KEYS.add(
            minute_key
        )

        if len(LAST_SIGNAL_KEYS) > 1000:

            LAST_SIGNAL_KEYS = set(
                list(LAST_SIGNAL_KEYS)[-500:]
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
                user.pair != "ANY"
                and user.pair != pair
            ):
                continue

            if user.timeframe != timeframe:
                continue

            await send_signal_to_user(
                user.telegram_id,
                result,
            )


async def scheduler_loop():

    while True:

        try:

            await scan_once()

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "Scanner error"
            )

        await asyncio.sleep(
            max(
                10,
                config.scan_interval,
            )
        )


# ============================================================
# RESULT LOOP
# ============================================================

async def result_loop():

    while True:

        try:

            await mark_expired_signals()

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "Result loop error"
            )

        await asyncio.sleep(30)


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(
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

    text = (
        "🤖 <b>POCKET SIGNAL BOT</b>\n\n"
        "Профессиональный анализ рынка и "
        "технические сигналы.\n\n"
        f"💱 Пара: <b>{user.pair}</b>\n"
        f"⏱ Время: <b>{user.timeframe} мин.</b>\n"
        f"📡 Автосигналы: "
        f"<b>{'ON' if user.auto_signals else 'OFF'}</b>\n\n"
    )

    if (
        not user.access
        and config.join_required
    ):

        text += (
            "🔐 Для получения сигналов сначала "
            "подай заявку на вступление."
        )

    await message.answer(
        text,
        reply_markup=main_keyboard(user),
    )


# ============================================================
# OWNER
# ============================================================

@dp.message(Command("owner"))
async def owner_handler(
    message: Message,
):

    if message.from_user.id != config.owner_id:
        return

    await message.answer(
        "👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА</b>",
        reply_markup=owner_keyboard(),
    )


# ============================================================
# MANUAL SIGNAL
# ============================================================

@dp.callback_query(F.data == "signal")
async def manual_signal(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if not user or user.blocked:

        await call.answer(
            "Доступ заблокирован.",
            show_alert=True,
        )

        return

    if (
        config.join_required
        and not user.access
    ):

        await call.answer(
            "Сначала получи доступ.",
            show_alert=True,
        )

        return

    await call.answer(
        "🔎 Анализирую рынок..."
    )

    pair = (
        config.pairs[0]
        if user.pair == "ANY"
        else user.pair
    )

    result = await get_signal_for(
        pair,
        user.timeframe,
    )

    if not result:

        await call.message.answer(
            "⚪ <b>Сильного сигнала сейчас нет.</b>\n\n"
            f"💱 {pair}\n"
            f"⏱ {user.timeframe} мин.\n\n"
            "Я не буду выдавать слабый сигнал "
            "только ради того, чтобы что-то показать."
        )

        return

    await send_signal_to_user(
        call.from_user.id,
        result,
    )


# ============================================================
# AUTO TOGGLE
# ============================================================

@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if not user:
        return

    new_value = not user.auto_signals

    await update_user(
        user.telegram_id,
        auto_signals=new_value,
    )

    user.auto_signals = new_value

    with suppress(Exception):

        await call.message.edit_reply_markup(
            reply_markup=main_keyboard(user)
        )

    await call.answer(
        "Автосигналы "
        + (
            "включены"
            if new_value
            else "выключены"
        )
    )


# ============================================================
# PAIRS
# ============================================================

@dp.callback_query(F.data == "pairs")
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
                callback_data=f"pair_{pair}",
            )
        )

        if len(row) == 2:

            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    await call.message.answer(
        "💱 <b>Выбери валютную пару:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await call.answer()


@dp.callback_query(F.data.startswith("pair_"))
async def choose_pair(
    call: CallbackQuery,
):

    pair = call.data.replace(
        "pair_",
        "",
        1,
    )

    await update_user(
        call.from_user.id,
        pair=pair,
    )

    await call.answer(
        f"Пара: {pair}"
    )


# ============================================================
# TIMEFRAMES
# ============================================================

@dp.callback_query(F.data == "timeframes")
async def timeframes_menu(
    call: CallbackQuery,
):

    buttons = []

    row = []

    for value in config.timeframes:

        row.append(
            InlineKeyboardButton(
                text=f"{value} мин.",
                callback_data=f"time_{value}",
            )
        )

        if len(row) == 3:

            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(
                text="♾ Любое время",
                callback_data="time_ANY",
            )
        ]
    )

    await call.message.answer(
        "⏱ <b>Выбери время экспирации:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await call.answer()


@dp.callback_query(F.data.startswith("time_"))
async def choose_time(
    call: CallbackQuery,
):

    value = call.data.replace(
        "time_",
        "",
        1,
    )

    if value == "ANY":

        value = 5

    await update_user(
        call.from_user.id,
        timeframe=int(value),
    )

    await call.answer(
        f"Время: {value} мин."
    )


# ============================================================
# SETTINGS
# ============================================================

@dp.callback_query(F.data == "settings")
async def settings(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if not user:
        return

    await call.message.answer(
        "⚙️ <b>Текущие настройки</b>\n\n"
        f"💱 Пара: <b>{user.pair}</b>\n"
        f"⏱ Время: <b>{user.timeframe} мин.</b>\n"
        f"📡 Автосигналы: "
        f"<b>{'ON' if user.auto_signals else 'OFF'}</b>\n"
        f"🔐 Доступ: "
        f"<b>{'ОТКРЫТ' if user.access else 'ЗАКРЫТ'}</b>",
    )

    await call.answer()


# ============================================================
# ACCESS
# ============================================================

@dp.callback_query(F.data == "access")
async def access(
    call: CallbackQuery,
):

    user = await get_user(
        call.from_user.id
    )

    if not user:
        return

    if user.access:

        await call.answer(
            "Доступ уже открыт.",
            show_alert=True,
        )

        return

    if not config.join_chat_id:

        await call.message.answer(
            "ℹ️ Обязательная подписка сейчас "
            "не настроена.\n\n"
            "Владелец может открыть доступ "
            "через настройки."
        )

        await call.answer()

        return

    try:

        invite = await bot.create_chat_invite_link(
            chat_id=config.join_chat_id,
            creates_join_request=True,
            name="Pocket Signal Access",
        )

        await call.message.answer(
            "🔐 <b>Получение доступа</b>\n\n"
            "1️⃣ Нажми кнопку ниже.\n"
            "2️⃣ Отправь заявку на вступление.\n"
            "3️⃣ После одобрения получишь доступ "
            "к сигналам.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔗 Подать заявку",
                            url=invite.invite_link,
                        )
                    ]
                ]
            ),
        )

    except Exception as exc:

        logger.exception(
            "Cannot create invite link: %s",
            exc,
        )

        await call.message.answer(
            "❌ Не удалось создать ссылку.\n\n"
            "Проверь, что бот является "
            "администратором канала/группы "
            "и имеет право приглашать пользователей."
        )

    await call.answer()


# ============================================================
# JOIN REQUEST
# ============================================================

@dp.chat_join_request()
async def join_request_handler(
    request: ChatJoinRequest,
):

    if (
        config.join_chat_id
        and request.chat.id != config.join_chat_id
    ):
        return

    user = request.from_user

    saved = await save_join_request(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )

    try:

        await bot.send_message(
            config.owner_id,
            "🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
            f"👤 Имя: <b>{user.first_name}</b>\n"
            f"🔹 Username: "
            f"<b>@{user.username or 'нет'}</b>\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"📅 Время: "
            f"<b>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</b>\n\n"
            f"📝 Заявка ID: <code>{saved.id}</code>",
        )

    except Exception:

        logger.exception(
            "Cannot notify owner about join request"
        )


# ============================================================
# OWNER USERS
# ============================================================

@dp.callback_query(F.data == "owner_users")
async def owner_users(
    call: CallbackQuery,
):

    if call.from_user.id != config.owner_id:
        return

    stats = await get_user_stats()

    await call.message.answer(
        "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
        f"👤 Всего: <b>{stats['total']}</b>\n"
        f"🟢 С доступом: <b>{stats['active']}</b>\n"
        f"🚫 Заблокировано: <b>{stats['blocked']}</b>"
    )

    await call.answer()


# ============================================================
# OWNER STATS
# ============================================================

@dp.callback_query(F.data == "owner_stats")
async def owner_stats(
    call: CallbackQuery,
):

    if call.from_user.id != config.owner_id:
        return

    users = await get_user_stats()
    signals = await get_signal_stats()

    await call.message.answer(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        "👥 Пользователи\n"
        f"• Всего: <b>{users['total']}</b>\n"
        f"• Активных: <b>{users['active']}</b>\n"
        f"• Заблокировано: <b>{users['blocked']}</b>\n\n"
        "📡 Сигналы\n"
        f"• Всего: <b>{signals['total']}</b>\n"
        f"• WIN: <b>{signals['wins']}</b>\n"
        f"• LOSS: <b>{signals['losses']}</b>\n"
        f"• ACTIVE: <b>{signals['active']}</b>\n"
        f"• WINRATE: <b>{signals['winrate']:.2f}%</b>"
    )

    await call.answer()


# ============================================================
# OWNER SIGNALS
# ============================================================

@dp.callback_query(F.data == "owner_signals")
async def owner_signals(
    call: CallbackQuery,
):

    if call.from_user.id != config.owner_id:
        return

    stats = await get_signal_stats()

    await call.message.answer(
        "📡 <b>СИГНАЛЫ</b>\n\n"
        f"Всего: <b>{stats['total']}</b>\n"
        f"WIN: <b>{stats['wins']}</b>\n"
        f"LOSS: <b>{stats['losses']}</b>\n"
        f"ACTIVE: <b>{stats['active']}</b>\n"
        f"WINRATE: <b>{stats['winrate']:.2f}%</b>"
    )

    await call.answer()


# ============================================================
# OWNER REQUESTS
# ============================================================

@dp.callback_query(F.data == "owner_requests")
async def owner_requests(
    call: CallbackQuery,
):

    if call.from_user.id != config.owner_id:
        return

    requests = await get_pending_requests()

    if not requests:

        await call.message.answer(
            "🔐 <b>Заявок нет.</b>"
        )

        await call.answer()

        return

    text = "🔐 <b>ОЖИДАЮЩИЕ ЗАЯВКИ</b>\n\n"

    for request in requests[:20]:

        text += (
            f"🆔 <b>{request.id}</b>\n"
            f"👤 {request.first_name or 'Без имени'}\n"
            f"🔹 @{request.username or 'нет'}\n"
            f"Telegram ID: <code>{request.telegram_id}</code>\n"
            f"📅 {request.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        )

    await call.message.answer(text)

    await call.answer()


# ============================================================
# OWNER AUTO
# ============================================================

@dp.callback_query(F.data == "owner_auto")
async def owner_auto(
    call: CallbackQuery,
):

    global AUTO_SIGNALS

    if call.from_user.id != config.owner_id:
        return

    AUTO_SIGNALS = not AUTO_SIGNALS

    await call.message.answer(
        "📡 Автосигналы: "
        f"<b>{'ON' if AUTO_SIGNALS else 'OFF'}</b>"
    )

    await call.answer()


# ============================================================
# OWNER RECENT
# ============================================================

@dp.callback_query(F.data == "owner_recent")
async def owner_recent(
    call: CallbackQuery,
):

    if call.from_user.id != config.owner_id:
        return

    signals = await get_recent_signals(10)

    if not signals:

        await call.message.answer(
            "🧹 Сигналов пока нет."
        )

        await call.answer()

        return

    text = "🧹 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>\n\n"

    for signal in signals:

        result = signal.result or "ACTIVE"

        text += (
            f"#{signal.id} "
            f"<b>{signal.pair}</b> "
            f"{signal.direction}\n"
            f"⏱ {signal.timeframe} мин. | "
            f"🎯 {signal.probability:.1f}% | "
            f"⭐ {signal.quality:.1f}\n"
            f"📊 {result}\n\n"
        )

    await call.message.answer(text)

    await call.answer()


# ============================================================
# OWNER PAIRS
# ============================================================

@dp.callback_query(F.data == "owner_pairs")
async def owner_pairs(
    call: CallbackQuery,
):

    if call.from_user.id != config.owner_id:
        return

    rows = await get_pair_stats()

    if not rows:

        await call.message.answer(
            "📊 Статистика по парам пока пустая."
        )

        await call.answer()

        return

    text = "📊 <b>СТАТИСТИКА ПО ПАРАМ</b>\n\n"

    for pair, total, wins, losses in rows:

        wins = wins or 0
        losses = losses or 0

        completed = wins + losses

        winrate = (
            wins / completed * 100
            if completed
            else 0
        )

        text += (
            f"💱 <b>{pair}</b>\n"
            f"Всего: {total}\n"
            f"WIN: {wins}\n"
            f"LOSS: {losses}\n"
            f"WINRATE: {winrate:.2f}%\n\n"
        )

    await call.message.answer(text)

    await call.answer()


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown():

    logger.info(
        "Shutting down Pocket Signal Bot..."
    )

    with suppress(Exception):
        await bot.session.close()


# ============================================================
# FASTAPI SERVER
# ============================================================

async def run_http_server():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )

    server = uvicorn.Server(
        server_config
    )

    logger.info(
        "HTTP health server starting on 0.0.0.0:%s",
        port,
    )

    await server.serve()


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "POCKET SIGNAL BOT starting..."
    )

    await init_db()

    logger.info(
        "Database initialized"
    )

    http_task = asyncio.create_task(
        run_http_server()
    )

    scanner_task = asyncio.create_task(
        scheduler_loop()
    )

    result_task = asyncio.create_task(
        result_loop()
    )

    try:

        logger.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot
        )

    finally:

        for task in (
            http_task,
            scanner_task,
            result_task,
        ):

            task.cancel()

        for task in (
            http_task,
            scanner_task,
            result_task,
        ):

            with suppress(
                asyncio.CancelledError
            ):
                await task

        await shutdown()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped"
        )
