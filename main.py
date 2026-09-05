from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import (
    Bot,
    Dispatcher,
    F,
)
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
    Session,
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
    save_join_request,
    save_signal,
    set_join_request_status,
    update_user,
)
from market import market
from signals import engine


logging.basicConfig(
    level=logging.INFO,
)

bot = Bot(token=config.bot_token)
dp = Dispatcher()

AUTO_SIGNALS = True
LAST_SIGNAL_KEYS: set[str] = set()


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


async def send_signal_to_user(
    telegram_id: int,
    result,
):
    direction_icon = (
        "🟢" if result.direction == "UP"
        else "🔴"
    )

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "🚨 СИЛЬНЫЙ СИГНАЛ\n"
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
        "📊 Подтверждения:\n"
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
        logging.warning(
            "Cannot send signal to %s: %s",
            telegram_id,
            exc,
        )
        return False


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


async def scan_once():
    global LAST_SIGNAL_KEYS

    if not AUTO_SIGNALS:
        return

    users = await get_access_users()

    requested: set[tuple[str, int]] = set()

    for user in users:
        pair_list = (
            config.pairs
            if user.pair == "ANY"
            else [user.pair]
        )

        for pair in pair_list:
            requested.add(
                (pair, user.timeframe)
            )

    for pair, timeframe in requested:
        result = await get_signal_for(
            pair,
            timeframe,
        )

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

        LAST_SIGNAL_KEYS.add(minute_key)

        if len(LAST_SIGNAL_KEYS) > 1000:
            LAST_SIGNAL_KEYS = set(
                list(LAST_SIGNAL_KEYS)[-500:]
            )

        signal = await save_signal(
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

            if user.pair != "ANY" and user.pair != pair:
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

        except Exception:
            logging.exception(
                "Scanner error"
            )

        await asyncio.sleep(
            max(10, config.scan_interval)
        )


async def result_loop():
    while True:
        try:
            # В этой версии закрытые сигналы помечаются
            # как EXPIRED. Реальное определение WIN/LOSS
            # можно расширять отдельным провайдером котировок.
            from database import mark_expired_signals

            await mark_expired_signals()

        except Exception:
            logging.exception(
                "Result loop error"
            )

        await asyncio.sleep(30)


@dp.message(CommandStart())
async def start_handler(message: Message):
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

    if not user.access and config.join_required:
        text += (
            "🔐 Для получения сигналов сначала "
            "подай заявку на вступление."
        )

    await message.answer(
        text,
        reply_markup=main_keyboard(user),
    )


@dp.message(Command("owner"))
async def owner_handler(message: Message):
    if message.from_user.id != config.owner_id:
        return

    await message.answer(
        "👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА</b>",
        reply_markup=owner_keyboard(),
    )


@dp.callback_query(F.data == "signal")
async def manual_signal(call: CallbackQuery):
    user = await get_user(call.from_user.id)

    if not user or user.blocked:
        await call.answer(
            "Доступ заблокирован.",
            show_alert=True,
        )
        return

    if config.join_required and not user.access:
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


@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto(call: CallbackQuery):
    user = await get_user(call.from_user.id)

    if not user:
        return

    new_value = not user.auto_signals

    await update_user(
        user.telegram_id,
        auto_signals=new_value,
    )

    user.auto_signals = new_value

    await call.message.edit_reply_markup(
        reply_markup=main_keyboard(user)
    )

    await call.answer(
        "Автосигналы "
        + ("включены" if new_value else "выключены")
    )


@dp.callback_query(F.data == "pairs")
async def pairs_menu(call: CallbackQuery):
    buttons = []

    buttons.append([
        InlineKeyboardButton(
            text="🌐 Любая пара",
            callback_data="pair_ANY",
        )
    ])

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
async def choose_pair(call: CallbackQuery):
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


@dp.callback_query(F.data == "timeframes")
async def timeframes_menu(call: CallbackQuery):
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

    buttons.append([
        InlineKeyboardButton(
            text="♾ Любое время",
            callback_data="time_ANY",
        )
    ])

    await call.message.answer(
        "⏱ <b>Выбери время экспирации:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await call.answer()


@dp.callback_query(F.data.startswith("time_"))
async def choose_time(call: CallbackQuery):
    value = call.data.replace(
        "time_",
        "",
        1,
    )

    if value == "ANY":
        # Для автоматического режима
        # выбираем наиболее универсальные 5 минут.
        value = 5

    await update_user(
        call.from_user.id,
        timeframe=int(value),
    )

    await call.answer(
        f"Время: {value} мин."
    )


@dp.callback_query(F.data == "settings")
async def settings(call: CallbackQuery):
    user = await get_user(call.from_user.id)

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


@dp.callback_query(F.data == "access")
async def access(call: CallbackQuery):
    user = await get_user(call.from_user.id)

    if user.access:
        await call.answer(
            "Доступ уже открыт.",
            show_alert=True,
        )
        return

    if not config.join_chat_id:
        await call.answer(
            "JOIN_CHAT_ID не настроен.",
            show_alert=True,
        )
        return

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=config.join_chat_id,
            name="Signal Bot",
            creates_join_request=True,
        )

        await call.message.answer(
            "🔐 <b>Получение доступа</b>\n\n"
            "1️⃣ Нажми кнопку ниже.\n"
            "2️⃣ Отправь заявку на вступление.\n"
            "3️⃣ После одобрения доступ к сигналам "
            "будет открыт автоматически.",
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
        logging.exception(
            "Invite creation error"
        )

        await call.answer(
            "Не удалось создать ссылку. "
            "Проверь права бота.",
            show_alert=True,
        )


@dp.chat_join_request()
async def join_request_handler(
    request: ChatJoinRequest,
):
    user = request.from_user

    await ensure_user(
        user.id,
        user.username,
        user.first_name,
    )

    saved = await save_join_request(
        user.id,
        user.username,
        user.first_name,
    )

    if config.join_chat_id:
        if str(request.chat.id) != str(
            config.join_chat_id
        ):
            return

    username = (
        f"@{user.username}"
        if user.username
        else "нет username"
    )

    await bot.send_message(
        config.owner_id,
        "🔐 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 {user.first_name}\n"
        f"🔗 {username}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        "Выбери действие:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Одобрить",
                        callback_data=f"approve_{saved.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"decline_{saved.id}",
                    ),
                ]
            ]
        ),
    )


@dp.callback_query(F.data.startswith("approve_"))
async def approve_request(call: CallbackQuery):
    if call.from_user.id != config.owner_id:
        return

    request_id = int(
        call.data.replace(
            "approve_",
            "",
            1,
        )
    )

    pending = await get_pending_requests()

    request = next(
        (
            x for x in pending
            if x.id == request_id
        ),
        None,
    )

    if not request:
        await call.answer(
            "Заявка уже обработана.",
            show_alert=True,
        )
        return

    if config.join_chat_id:
        try:
            await bot.approve_chat_join_request(
                chat_id=config.join_chat_id,
                user_id=request.telegram_id,
            )
        except Exception as exc:
            logging.exception(
                "Approve error: %s",
                exc,
            )

    await update_user(
        request.telegram_id,
        access=True,
    )

    await set_join_request_status(
        request.id,
        "approved",
    )

    try:
        await bot.send_message(
            request.telegram_id,
            "✅ <b>Доступ одобрен!</b>\n\n"
            "Теперь тебе доступны сигналы.",
        )
    except Exception:
        pass

    await call.message.edit_text(
        "✅ <b>Заявка одобрена.</b>\n\n"
        f"🆔 {request.telegram_id}\n"
        "🔓 Доступ к сигналам открыт."
    )

    await call.answer("Одобрено")


@dp.callback_query(F.data.startswith("decline_"))
async def decline_request(call: CallbackQuery):
    if call.from_user.id != config.owner_id:
        return

    request_id = int(
        call.data.replace(
            "decline_",
            "",
            1,
        )
    )

    pending = await get_pending_requests()

    request = next(
        (
            x for x in pending
            if x.id == request_id
        ),
        None,
    )

    if not request:
        await call.answer(
            "Заявка уже обработана.",
            show_alert=True,
        )
        return

    if config.join_chat_id:
        try:
            await bot.decline_chat_join_request(
                chat_id=config.join_chat_id,
                user_id=request.telegram_id,
            )
        except Exception:
            logging.exception(
                "Decline error"
            )

    await set_join_request_status(
        request.id,
        "declined",
    )

    await call.message.edit_text(
        "❌ <b>Заявка отклонена.</b>\n\n"
        f"🆔 {request.telegram_id}"
    )

    await call.answer("Отклонено")


@dp.callback_query(F.data.startswith("owner_"))
async def owner_callbacks(call: CallbackQuery):
    if call.from_user.id != config.owner_id:
        return

    action = call.data.replace(
        "owner_",
        "",
        1,
    )

    if action == "users":
        stats = await get_user_stats()

        await call.message.answer(
            "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
            f"Всего: <b>{stats['total']}</b>\n"
            f"Активных: <b>{stats['active']}</b>\n"
            f"Заблокировано: <b>{stats['blocked']}</b>"
        )

    elif action == "stats":
        stats = await get_signal_stats()

        await call.message.answer(
            "📊 <b>СТАТИСТИКА</b>\n\n"
            f"Всего сигналов: <b>{stats['total']}</b>\n"
            f"🟢 WIN: <b>{stats['wins']}</b>\n"
            f"🔴 LOSS: <b>{stats['losses']}</b>\n"
            f"🟡 ACTIVE: <b>{stats['active']}</b>\n"
            f"📈 WINRATE: <b>{stats['winrate']:.2f}%</b>"
        )

    elif action == "signals":
        stats = await get_signal_stats()

        await call.message.answer(
            "📡 <b>СИГНАЛЫ</b>\n\n"
            f"Всего: {stats['total']}\n"
            f"Активных: {stats['active']}\n"
            f"WIN: {stats['wins']}\n"
            f"LOSS: {stats['losses']}\n"
            f"WINRATE: {stats['winrate']:.2f}%"
        )

    elif action == "requests":
        requests = await get_pending_requests()

        if not requests:
            await call.message.answer(
                "🔐 Нет ожидающих заявок."
            )
            return

        for request in requests[:20]:
            username = (
                f"@{request.username}"
                if request.username
                else "нет username"
            )

            await call.message.answer(
                "🔐 <b>ЗАЯВКА</b>\n\n"
                f"👤 {request.first_name}\n"
                f"🔗 {username}\n"
                f"🆔 <code>{request.telegram_id}</code>\n"
                f"📅 {request.created_at:%d.%m.%Y %H:%M}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Одобрить",
                                callback_data=f"approve_{request.id}",
                            ),
                            InlineKeyboardButton(
                                text="❌ Отклонить",
                                callback_data=f"decline_{request.id}",
                            ),
                        ]
                    ]
                ),
            )

    elif action == "auto":
        global AUTO_SIGNALS

        AUTO_SIGNALS = not AUTO_SIGNALS

        await call.message.answer(
            "▶️ Автосигналы: "
            + (
                "<b>ON</b>"
                if AUTO_SIGNALS
                else "<b>OFF</b>"
            )
        )

    elif action == "recent":
        signals = await get_recent_signals(10)

        if not signals:
            await call.message.answer(
                "Сигналов пока нет."
            )
            return

        text = "🧹 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>\n\n"

        for signal in signals:
            text += (
                f"{'🟢' if signal.direction == 'UP' else '🔴'} "
                f"{signal.pair} "
                f"{signal.direction} "
                f"{signal.timeframe}m | "
                f"{signal.quality:.0f}% | "
                f"{signal.status}\n"
            )

        await call.message.answer(text)

    elif action == "pairs":
        stats = await get_pair_stats()

        if not stats:
            await call.message.answer(
                "Статистики по парам пока нет."
            )
            return

        text = "📊 <b>СТАТИСТИКА ПО ПАРАМ</b>\n\n"

        for pair, total, wins, losses in stats:
            wins = wins or 0
            losses = losses or 0

            completed = wins + losses

            rate = (
                wins / completed * 100
                if completed
                else 0
            )

            text += (
                f"💱 <b>{pair}</b>\n"
                f"Сигналов: {total}\n"
                f"WIN: {wins} | LOSS: {losses}\n"
                f"WINRATE: {rate:.1f}%\n\n"
            )

        await call.message.answer(text)

    await call.answer()


async def main():
    await init_db()
    await market.start()

    asyncio.create_task(
        scheduler_loop()
    )

    asyncio.create_task(
        result_loop()
    )

    logging.info(
        "POCKET SIGNAL BOT started"
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "chat_join_request",
            ],
        )
    finally:
        await market.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
