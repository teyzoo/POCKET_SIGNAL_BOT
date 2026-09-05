from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
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
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("pocket_signal_bot")


# ============================================================
# BOT / DISPATCHER
# ============================================================

if not config.bot_token:
    raise RuntimeError("BOT_TOKEN is not configured")

bot = Bot(
    token=config.bot_token,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()

market = PocketMarket()
engine = SignalEngine()


# ============================================================
# GLOBAL STATE
# ============================================================

AUTO_SIGNALS = True

# Защита от повторной отправки одинаковых автосигналов.
LAST_KEYS: set[str] = set()

# Защита от одновременного анализа одного пользователя.
USER_ANALYSIS_LOCKS: dict[int, asyncio.Lock] = {}


# ============================================================
# FASTAPI
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "POCKET_SIGNAL_BOT",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "POCKET_SIGNAL_BOT",
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

    if value in {"UP", "CALL", "BUY", "ВВЕРХ", "ВЫШЕ"}:
        return "🟢 ВВЕРХ"

    if value in {"DOWN", "PUT", "SELL", "ВНИЗ", "НИЖЕ"}:
        return "🔴 ВНИЗ"

    return value


def timeframe_text(minutes: int) -> str:
    return f"{minutes} мин"


def main_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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


def owner_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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


def signal_pair_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text="🌐 ЛЮБАЯ ПАРА",
                callback_data="sigp:ANY",
            )
        ]
    ]

    current_row = []

    for display_name, symbol in config.pairs:
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

    return InlineKeyboardMarkup(inline_keyboard=rows)


def signal_time_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text="1 мин",
                callback_data="sigt:1",
            ),
            InlineKeyboardButton(
                text="2 мин",
                callback_data="sigt:2",
            ),
            InlineKeyboardButton(
                text="3 мин",
                callback_data="sigt:3",
            ),
        ],
        [
            InlineKeyboardButton(
                text="5 мин",
                callback_data="sigt:5",
            ),
            InlineKeyboardButton(
                text="10 мин",
                callback_data="sigt:10",
            ),
            InlineKeyboardButton(
                text="15 мин",
                callback_data="sigt:15",
            ),
        ],
        [
            InlineKeyboardButton(
                text="20 мин",
                callback_data="sigt:20",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🌐 ЛЮБОЕ ВРЕМЯ",
                callback_data="sigt:ANY",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад к паре",
                callback_data="signal",
            ),
        ],
    ]

    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_settings_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="back_main",
                )
            ]
        ]
    )


def format_signal(signal) -> str:
    reasons = getattr(signal, "reasons", None) or []

    reasons_text = ""

    if reasons:
        reasons_text = "\n".join(
            f"• {reason}"
            for reason in reasons[:8]
        )

    result = (
        "📈 <b>СИЛЬНЫЙ OTC-СИГНАЛ</b>\n\n"
        f"💱 <b>Пара:</b> {pair_name(signal.pair)}\n"
        f"📌 <b>Направление:</b> {direction_text(signal.direction)}\n"
        f"⏱ <b>Экспирация:</b> {signal.timeframe} мин\n"
        f"🎯 <b>Вероятность:</b> {signal.probability:.1f}%\n"
        f"⭐ <b>Quality Score:</b> {signal.quality:.1f}/100\n\n"
        f"🕐 <b>Вход:</b> "
        f"{signal.entry_time.strftime('%H:%M:%S')} UTC\n"
        f"⏰ <b>Закрытие:</b> "
        f"{signal.close_time.strftime('%H:%M:%S')} UTC"
    )

    if reasons_text:
        result += (
            "\n\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{reasons_text}"
        )

    result += (
        "\n\n"
        "⚠️ Сигнал является результатом технического анализа "
        "и не гарантирует прибыль."
    )

    return result


async def send_signal(
    user_id: int,
    signal,
):
    text = format_signal(signal)

    await bot.send_message(
        user_id,
        text,
    )


async def get_signal(
    pair: str,
    timeframe: int,
):
    """
    Получение реальных свечей и анализ.

    ВАЖНО:
    market.candles() должен выбрасывать исключение,
    если Pocket Option не дал актуальные данные.

    Здесь мы специально НЕ превращаем ошибку рынка
    в обычный None.
    """

    candles = await market.candles(
        pair,
        minutes=1,
        limit=200,
    )

    if not candles:
        raise RuntimeError(
            f"Market returned no candles for {pair}"
        )

    return engine.analyze(
        pair,
        timeframe,
        candles,
    )


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
):
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )
    except Exception:
        try:
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("Unable to send/edit message")


async def show_signal_analysis_progress(
    callback: CallbackQuery,
    pair: str,
    timeframe_text_value: str,
    candles_count: int | None = None,
    checked: int | None = None,
    total: int | None = None,
):
    text = (
        "🔎 <b>АНАЛИЗ OTC</b>\n\n"
        f"💱 <b>Пара:</b> {pair}\n"
        f"⏱ <b>Время:</b> {timeframe_text_value}\n\n"
        "📡 Подключение к рыночным данным...\n"
    )

    if candles_count is not None:
        text += (
            f"📊 <b>Свечей получено:</b> "
            f"{candles_count}\n"
        )
    else:
        text += "📊 Получение свечей...\n"

    text += "🧮 Рассчитываю индикаторы...\n"

    if checked is not None and total is not None:
        text += (
            f"📡 <b>Проверено:</b> "
            f"{checked}/{total}\n"
        )

    await safe_edit(
        callback,
        text,
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    user = await ensure_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    await message.answer(
        "🚀 <b>Pocket Option Signal Bot</b>\n\n"
        "Бот анализирует OTC-рынок и ищет сильные "
        "технические сигналы.\n\n"
        "📈 Нажми <b>Сигнал</b>, чтобы выбрать пару "
        "и время экспирации.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# OWNER
# ============================================================

def is_owner(user_id: int) -> bool:
    return (
        config.owner_id is not None
        and int(user_id) == int(config.owner_id)
    )


@dp.message(Command("owner"))
async def owner_handler(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer(
            "⛔ Доступ запрещён."
        )
        return

    await message.answer(
        "👑 <b>Панель владельца</b>",
        reply_markup=owner_keyboard(),
    )


@dp.callback_query(F.data == "owner_users")
async def owner_users(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    try:
        users = await get_access_users()

        await safe_edit(
            callback,
            (
                "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
                f"Всего пользователей с доступом: "
                f"<b>{len(users)}</b>"
            ),
            owner_keyboard(),
        )

    except Exception as exc:
        logger.exception("owner_users failed")

        await safe_edit(
            callback,
            (
                "❌ <b>Ошибка получения пользователей</b>\n\n"
                f"<code>{str(exc)[:500]}</code>"
            ),
            owner_keyboard(),
        )


@dp.callback_query(F.data == "owner_stats")
async def owner_stats(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    try:
        stats = await get_signal_stats()

        await safe_edit(
            callback,
            (
                "📊 <b>ОБЩАЯ СТАТИСТИКА</b>\n\n"
                f"📈 Всего сигналов: "
                f"<b>{stats.get('total', 0)}</b>\n"
                f"🟢 WIN: "
                f"<b>{stats.get('wins', 0)}</b>\n"
                f"🔴 LOSS: "
                f"<b>{stats.get('losses', 0)}</b>\n"
                f"⚪ Нерешённых: "
                f"<b>{stats.get('pending', 0)}</b>\n"
                f"🎯 WINRATE: "
                f"<b>{stats.get('winrate', 0):.2f}%</b>"
            ),
            owner_keyboard(),
        )

    except Exception as exc:
        logger.exception("owner_stats failed")

        await safe_edit(
            callback,
            (
                "❌ <b>Ошибка статистики</b>\n\n"
                f"<code>{str(exc)[:500]}</code>"
            ),
            owner_keyboard(),
        )


@dp.callback_query(F.data == "owner_signals")
async def owner_signals(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    try:
        signals = await get_recent_signals(limit=10)

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
                pair = getattr(
                    signal,
                    "pair",
                    "?",
                )

                direction = getattr(
                    signal,
                    "direction",
                    "?",
                )

                timeframe = getattr(
                    signal,
                    "timeframe",
                    "?",
                )

                probability = getattr(
                    signal,
                    "probability",
                    0,
                )

                result = getattr(
                    signal,
                    "result",
                    None,
                )

                result_text = result or "PENDING"

                lines.append(
                    f"• {pair_name(pair)} | "
                    f"{direction_text(direction)} | "
                    f"{timeframe}m | "
                    f"{probability:.1f}% | "
                    f"{result_text}"
                )

            text = "\n".join(lines)

        await safe_edit(
            callback,
            text,
            owner_keyboard(),
        )

    except Exception as exc:
        logger.exception("owner_signals failed")

        await safe_edit(
            callback,
            (
                "❌ <b>Ошибка</b>\n\n"
                f"<code>{str(exc)[:500]}</code>"
            ),
            owner_keyboard(),
        )


@dp.callback_query(F.data == "owner_pairs")
async def owner_pairs(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    try:
        stats = await get_pair_stats()

        lines = [
            "💱 <b>СТАТИСТИКА ПО ПАРАМ</b>\n"
        ]

        if not stats:
            lines.append(
                "Пока нет завершённых сигналов."
            )
        else:
            for row in stats:
                if isinstance(row, dict):
                    pair = row.get("pair", "?")
                    total = row.get("total", 0)
                    wins = row.get("wins", 0)
                    losses = row.get("losses", 0)
                    winrate = row.get("winrate", 0)
                else:
                    pair = getattr(row, "pair", "?")
                    total = getattr(row, "total", 0)
                    wins = getattr(row, "wins", 0)
                    losses = getattr(row, "losses", 0)
                    winrate = getattr(row, "winrate", 0)

                lines.append(
                    f"\n<b>{pair_name(pair)}</b>\n"
                    f"Всего: {total}\n"
                    f"WIN: {wins}\n"
                    f"LOSS: {losses}\n"
                    f"WINRATE: {winrate:.2f}%"
                )

        await safe_edit(
            callback,
            "\n".join(lines),
            owner_keyboard(),
        )

    except Exception as exc:
        logger.exception("owner_pairs failed")

        await safe_edit(
            callback,
            (
                "❌ <b>Ошибка статистики пар</b>\n\n"
                f"<code>{str(exc)[:500]}</code>"
            ),
            owner_keyboard(),
        )


@dp.callback_query(F.data == "owner_auto")
async def owner_auto(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    status = (
        "🟢 ВКЛЮЧЕН"
        if AUTO_SIGNALS
        else "🔴 ВЫКЛЮЧЕН"
    )

    await safe_edit(
        callback,
        (
            "🤖 <b>АВТОСКАНЕР</b>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Интервал: <b>{config.scan_interval}</b> сек.\n"
            f"Минимальный Quality Score: "
            f"<b>{config.min_signal_score:.1f}</b>\n"
            f"Минимальная вероятность: "
            f"<b>{config.min_probability:.1f}%</b>"
        ),
        owner_keyboard(),
    )


# ============================================================
# MAIN MENU
# ============================================================

@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await safe_edit(
        callback,
        (
            "🚀 <b>Pocket Option Signal Bot</b>\n\n"
            "Выбери действие:"
        ),
        main_keyboard(),
    )


# ============================================================
# SIGNAL FLOW — PAIR SELECTION
# ============================================================

@dp.callback_query(F.data == "signal")
async def signal_menu(callback: CallbackQuery):
    await callback.answer()

    await safe_edit(
        callback,
        (
            "📈 <b>НАСТРОЙКА СИГНАЛА</b>\n\n"
            "💱 <b>Выбери OTC-пару:</b>\n\n"
            "🌐 <b>ЛЮБАЯ ПАРА</b>\n"
            "Бот реально проверит доступные OTC-пары "
            "и выберет лучший результат."
        ),
        signal_pair_keyboard(),
    )


@dp.callback_query(F.data.startswith("sigp:"))
async def signal_pair_selected(callback: CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    selected_pair = callback.data.split(":", 1)[1]

    if selected_pair == "ANY":
        display_pair = "🌐 Любая пара"
        database_pair = "ANY"
    else:
        display_pair = pair_name(selected_pair)
        database_pair = selected_pair

    try:
        await update_user(
            user_id,
            pair=database_pair,
        )
    except Exception:
        logger.exception(
            "Unable to save selected pair"
        )

    await safe_edit(
        callback,
        (
            "⏱ <b>ВЫБОР ЭКСПИРАЦИИ</b>\n\n"
            f"💱 <b>Пара:</b> {display_pair}\n\n"
            "Выбери время закрытия сигнала:"
        ),
        signal_time_keyboard(),
    )


# ============================================================
# SIGNAL FLOW — TIME SELECTION + REAL ANALYSIS
# ============================================================

@dp.callback_query(F.data.startswith("sigt:"))
async def signal_time_selected(callback: CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    selected_time = callback.data.split(":", 1)[1]

    user = await get_user(user_id)

    if not user:
        await ensure_user(
            telegram_id=user_id,
            username=callback.from_user.username,
        )
        user = await get_user(user_id)

    selected_pair = getattr(
        user,
        "pair",
        "ANY",
    )

    if not selected_pair:
        selected_pair = "ANY"

    # --------------------------------------------------------
    # TIMEFRAMES
    # --------------------------------------------------------

    if selected_time == "ANY":
        selected_times = list(config.timeframes)
        selected_time_text = "🌐 Любое время"
    else:
        try:
            timeframe = int(selected_time)
        except ValueError:
            await safe_edit(
                callback,
                "❌ Некорректное время.",
                main_keyboard(),
            )
            return

        if timeframe not in config.timeframes:
            await safe_edit(
                callback,
                "❌ Такое время экспирации недоступно.",
                main_keyboard(),
            )
            return

        selected_times = [timeframe]
        selected_time_text = timeframe_text(timeframe)

    # --------------------------------------------------------
    # PAIRS
    # --------------------------------------------------------

    if selected_pair == "ANY":
        selected_pairs = [
            symbol
            for _, symbol in config.pairs
        ]

        selected_pair_text = "🌐 Любая пара"
    else:
        valid_symbols = {
            symbol
            for _, symbol in config.pairs
        }

        if selected_pair not in valid_symbols:
            await safe_edit(
                callback,
                "❌ Некорректная OTC-пара.",
                main_keyboard(),
            )
            return

        selected_pairs = [selected_pair]
        selected_pair_text = pair_name(selected_pair)

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
            main_keyboard(),
        )
        return

    async with lock:

        total_combinations = (
            len(selected_pairs) * len(selected_times)
        )

        checked = 0
        successful_market_checks = 0
        market_errors = 0

        best_signal = None

        await show_signal_analysis_progress(
            callback,
            selected_pair_text,
            selected_time_text,
            checked=0,
            total=total_combinations,
        )

        # ----------------------------------------------------
        # REAL MARKET ANALYSIS
        # ----------------------------------------------------

        for pair in selected_pairs:

            for timeframe in selected_times:

                checked += 1

                current_pair_text = pair_name(pair)

                progress_text = (
                    "🔎 <b>АНАЛИЗ OTC</b>\n\n"
                    f"💱 <b>Пара:</b> "
                    f"{current_pair_text}\n"
                    f"⏱ <b>Время:</b> "
                    f"{timeframe} мин\n\n"
                    "📡 Получаю реальные рыночные данные...\n"
                    "📊 Проверяю свечи...\n"
                    "🧮 Рассчитываю индикаторы...\n"
                    f"📡 <b>Проверено:</b> "
                    f"{checked}/{total_combinations}"
                )

                await safe_edit(
                    callback,
                    progress_text,
                )

                try:
                    # ------------------------------------------------
                    # IMPORTANT:
                    # REAL market request.
                    # No fake sleep.
                    # ------------------------------------------------

                    candles = await market.candles(
                        pair,
                        minutes=1,
                        limit=200,
                    )

                    if not candles:
                        raise RuntimeError(
                            "Pocket Option returned no candles"
                        )

                    successful_market_checks += 1

                    await safe_edit(
                        callback,
                        (
                            "🔎 <b>АНАЛИЗ OTC</b>\n\n"
                            f"💱 <b>Пара:</b> "
                            f"{current_pair_text}\n"
                            f"⏱ <b>Время:</b> "
                            f"{timeframe} мин\n\n"
                            f"📊 <b>Свечей получено:</b> "
                            f"{len(candles)}\n"
                            "🧮 Рассчитываю индикаторы...\n"
                            f"📡 <b>Проверено:</b> "
                            f"{checked}/{total_combinations}"
                        ),
                    )

                    signal = engine.analyze(
                        pair,
                        timeframe,
                        candles,
                    )

                    if signal is not None:

                        if best_signal is None:
                            best_signal = signal
                        else:
                            # Выбираем самый сильный результат.
                            current_score = float(
                                getattr(
                                    signal,
                                    "quality",
                                    0,
                                )
                            )

                            best_score = float(
                                getattr(
                                    best_signal,
                                    "quality",
                                    0,
                                )
                            )

                            if current_score > best_score:
                                best_signal = signal

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    market_errors += 1

                    logger.warning(
                        "Market check failed "
                        "pair=%s timeframe=%s: %s",
                        pair,
                        timeframe,
                        exc,
                    )

                    # Ошибка не считается отсутствием сигнала.
                    # Переходим к следующей комбинации.
                    continue

        # --------------------------------------------------------
        # ANALYSIS RESULT
        # --------------------------------------------------------

        # Ни одного успешного получения рынка.
        if successful_market_checks == 0:

            await safe_edit(
                callback,
                (
                    "⚠️ <b>РЫНОЧНЫЕ ДАННЫЕ НЕ ПОЛУЧЕНЫ</b>\n\n"
                    "Pocket Option не вернул ни одного набора "
                    "актуальных OTC-свечей.\n\n"
                    f"📡 Проверено комбинаций: "
                    f"<b>{checked}</b>\n"
                    f"❌ Ошибок получения данных: "
                    f"<b>{market_errors}</b>\n\n"
                    "❌ <b>Сигнал НЕ сформирован.</b>\n\n"
                    "Это не означает, что сильного сигнала нет. "
                    "Сначала необходимо получить актуальные "
                    "рыночные данные."
                ),
                main_keyboard(),
            )

            return

        # --------------------------------------------------------
        # NO STRONG SIGNAL
        # --------------------------------------------------------

        if best_signal is None:

            await safe_edit(
                callback,
                (
                    "⚪ <b>СИЛЬНОГО OTC-СИГНАЛА НЕТ</b>\n\n"
                    f"📊 Успешно проверено рыночных "
                    f"комбинаций: "
                    f"<b>{successful_market_checks}</b>\n"
                    f"📡 Всего проверено: "
                    f"<b>{checked}</b>\n"
                    f"❌ Ошибок получения данных: "
                    f"<b>{market_errors}</b>\n\n"
                    "Все доступные данные были реально "
                    "проверены.\n\n"
                    "Условия сильного сигнала не выполнены.\n"
                    "Я не буду выдавать слабый сигнал "
                    "только ради результата."
                ),
                main_keyboard(),
            )

            return

        # --------------------------------------------------------
        # SAVE SIGNAL
        # --------------------------------------------------------

        try:
            await save_signal(
                user_id=user_id,
                signal=best_signal,
            )
        except Exception:
            logger.exception(
                "Unable to save manual signal"
            )

        # --------------------------------------------------------
        # SEND RESULT
        # --------------------------------------------------------

        await safe_edit(
            callback,
            format_signal(best_signal),
            main_keyboard(),
        )


# ============================================================
# SETTINGS
# ============================================================

@dp.callback_query(F.data == "settings")
async def settings_handler(callback: CallbackQuery):
    await callback.answer()

    user = await get_user(
        callback.from_user.id
    )

    if not user:
        await ensure_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
        user = await get_user(
            callback.from_user.id
        )

    current_pair = getattr(
        user,
        "pair",
        "ANY",
    )

    current_timeframe = getattr(
        user,
        "timeframe",
        1,
    )

    if current_pair == "ANY":
        display_pair = "🌐 Любая пара"
    else:
        display_pair = pair_name(current_pair)

    auto_text = (
        "🟢 Включены"
        if AUTO_SIGNALS
        else "🔴 Выключены"
    )

    await safe_edit(
        callback,
        (
            "⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"💱 Пара: <b>{display_pair}</b>\n"
            f"⏱ Время: <b>{current_timeframe} мин</b>\n"
            f"🤖 Автосигналы: <b>{auto_text}</b>\n"
            f"⭐ Минимальный Quality: "
            f"<b>{config.min_signal_score:.1f}</b>\n"
            f"🎯 Минимальная вероятность: "
            f"<b>{config.min_probability:.1f}%</b>"
        ),
        back_settings_keyboard(),
    )


# ============================================================
# AUTO SIGNAL TOGGLE
# ============================================================

@dp.callback_query(F.data == "auto_toggle")
async def auto_toggle(callback: CallbackQuery):
    global AUTO_SIGNALS

    AUTO_SIGNALS = not AUTO_SIGNALS

    await callback.answer(
        (
            "Автосигналы включены"
            if AUTO_SIGNALS
            else "Автосигналы выключены"
        )
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
# OLD PAIR/TIME BUTTONS
# ============================================================
#
# Отдельных кнопок в главном меню больше нет.
#
# Но оставляем callback-совместимость, если старое сообщение
# пользователя ещё содержит старую кнопку.
# ============================================================

@dp.callback_query(F.data == "pairs")
async def old_pairs_handler(callback: CallbackQuery):
    await callback.answer()

    await safe_edit(
        callback,
        (
            "📈 <b>НАСТРОЙКА СИГНАЛА</b>\n\n"
            "💱 <b>Выбери OTC-пару:</b>"
        ),
        signal_pair_keyboard(),
    )


@dp.callback_query(F.data.startswith("pair_"))
async def old_pair_handler(callback: CallbackQuery):
    await callback.answer()

    symbol = callback.data.replace(
        "pair_",
        "",
        1,
    )

    if symbol not in {
        pair_symbol(name)
        for name, _ in config.pairs
    }:
        await safe_edit(
            callback,
            "❌ Неизвестная пара.",
            main_keyboard(),
        )
        return

    await update_user(
        callback.from_user.id,
        pair=symbol,
    )

    await safe_edit(
        callback,
        (
            "⏱ <b>ВЫБОР ЭКСПИРАЦИИ</b>\n\n"
            f"💱 <b>Пара:</b> {pair_name(symbol)}"
        ),
        signal_time_keyboard(),
    )


@dp.callback_query(F.data == "timeframes")
async def old_timeframes_handler(callback: CallbackQuery):
    await callback.answer()

    await safe_edit(
        callback,
        (
            "⏱ <b>ВЫБОР ЭКСПИРАЦИИ</b>\n\n"
            "Выбери время:"
        ),
        signal_time_keyboard(),
    )


# ============================================================
# USER STATS
# ============================================================

@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    await callback.answer()

    try:
        stats = await get_user_stats(
            callback.from_user.id
        )

        await safe_edit(
            callback,
            (
                "📊 <b>МОЯ СТАТИСТИКА</b>\n\n"
                f"📈 Сигналов: "
                f"<b>{stats.get('total', 0)}</b>\n"
                f"🟢 WIN: "
                f"<b>{stats.get('wins', 0)}</b>\n"
                f"🔴 LOSS: "
                f"<b>{stats.get('losses', 0)}</b>\n"
                f"⚪ Pending: "
                f"<b>{stats.get('pending', 0)}</b>\n"
                f"🎯 WINRATE: "
                f"<b>{stats.get('winrate', 0):.2f}%</b>"
            ),
            back_settings_keyboard(),
        )

    except Exception as exc:
        logger.exception("my_stats failed")

        await safe_edit(
            callback,
            (
                "❌ <b>Ошибка статистики</b>\n\n"
                f"<code>{str(exc)[:500]}</code>"
            ),
            back_settings_keyboard(),
        )


# ============================================================
# AUTO SCANNER
# ============================================================

async def scan_one_pair(
    pair: str,
    timeframe: int,
):
    """
    Реальная проверка одной пары.

    Ошибка рынка возвращается как ошибка,
    а не как отсутствие сигнала.
    """

    candles = await market.candles(
        pair,
        minutes=1,
        limit=200,
    )

    if not candles:
        raise RuntimeError(
            f"No candles for {pair}"
        )

    return engine.analyze(
        pair,
        timeframe,
        candles,
    )


async def auto_scanner_loop():
    global LAST_KEYS

    logger.info("Auto scanner started")

    while True:
        try:
            if not AUTO_SIGNALS:
                await asyncio.sleep(
                    max(5, config.scan_interval)
                )
                continue

            users = await get_access_users()

            if not users:
                await asyncio.sleep(
                    max(5, config.scan_interval)
                )
                continue

            for user in users:

                try:
                    user_id = getattr(
                        user,
                        "telegram_id",
                        None,
                    )

                    if not user_id:
                        continue

                    selected_pair = getattr(
                        user,
                        "pair",
                        "ANY",
                    )

                    timeframe = getattr(
                        user,
                        "timeframe",
                        1,
                    )

                    if not timeframe:
                        timeframe = 1

                    if selected_pair == "ANY":
                        pairs = [
                            symbol
                            for _, symbol in config.pairs
                        ]
                    else:
                        pairs = [selected_pair]

                    best_signal = None

                    for pair in pairs:

                        try:
                            signal = await scan_one_pair(
                                pair,
                                int(timeframe),
                            )

                            if signal is None:
                                continue

                            if best_signal is None:
                                best_signal = signal
                            else:
                                signal_quality = float(
                                    getattr(
                                        signal,
                                        "quality",
                                        0,
                                    )
                                )

                                best_quality = float(
                                    getattr(
                                        best_signal,
                                        "quality",
                                        0,
                                    )
                                )

                                if (
                                    signal_quality
                                    > best_quality
                                ):
                                    best_signal = signal

                        except asyncio.CancelledError:
                            raise

                        except Exception as exc:
                            logger.warning(
                                "Auto scan failed "
                                "user=%s pair=%s: %s",
                                user_id,
                                pair,
                                exc,
                            )
                            continue

                    if best_signal is None:
                        continue

                    # --------------------------------------------
                    # DUPLICATE PROTECTION
                    # --------------------------------------------

                    key = (
                        f"{user_id}:"
                        f"{best_signal.pair}:"
                        f"{best_signal.timeframe}:"
                        f"{best_signal.direction}:"
                        f"{best_signal.close_time.isoformat()}"
                    )

                    if key in LAST_KEYS:
                        continue

                    LAST_KEYS.add(key)

                    # Ограничиваем память.
                    if len(LAST_KEYS) > 5000:
                        LAST_KEYS = set(
                            list(LAST_KEYS)[-2500:]
                        )

                    # --------------------------------------------
                    # SAVE
                    # --------------------------------------------

                    try:
                        await save_signal(
                            user_id=user_id,
                            signal=best_signal,
                        )
                    except Exception:
                        logger.exception(
                            "Unable to save auto signal"
                        )

                    # --------------------------------------------
                    # SEND
                    # --------------------------------------------

                    try:
                        await send_signal(
                            user_id,
                            best_signal,
                        )

                    except Exception:
                        logger.exception(
                            "Unable to send auto signal "
                            "to user=%s",
                            user_id,
                        )

                except asyncio.CancelledError:
                    raise

                except Exception:
                    logger.exception(
                        "Auto scanner user iteration failed"
                    )

            await asyncio.sleep(
                max(5, config.scan_interval)
            )

        except asyncio.CancelledError:
            logger.info(
                "Auto scanner cancelled"
            )
            raise

        except Exception:
            logger.exception(
                "Auto scanner loop error"
            )

            await asyncio.sleep(
                max(10, config.scan_interval)
            )


# ============================================================
# RESULT CHECKER
# ============================================================

async def result_checker_loop():
    logger.info("Result checker started")

    while True:
        try:
            await mark_expired_signals()

        except asyncio.CancelledError:
            logger.info(
                "Result checker cancelled"
            )
            raise

        except Exception:
            logger.exception(
                "Result checker failed"
            )

        await asyncio.sleep(30)


# ============================================================
# STARTUP
# ============================================================

async def start_bot():
    logger.info(
        "Starting Pocket Option Signal Bot"
    )

    await init_db()

    logger.info(
        "Database initialized"
    )

    # --------------------------------------------------------
    # MARKET CONNECTION
    # --------------------------------------------------------

    try:
        await market.connect()

        logger.info(
            "Pocket Option market connected"
        )

    except Exception as exc:
        logger.exception(
            "Pocket Option connection failed: %s",
            exc,
        )

        # Не падаем сразу.
        #
        # market.py сможет повторить подключение
        # при следующем запросе.
        logger.warning(
            "Bot will continue and market connection "
            "will be retried when needed."
        )

    # --------------------------------------------------------
    # BACKGROUND TASKS
    # --------------------------------------------------------

    scanner_task = asyncio.create_task(
        auto_scanner_loop()
    )

    result_task = asyncio.create_task(
        result_checker_loop()
    )

    # --------------------------------------------------------
    # FASTAPI
    # --------------------------------------------------------

    port = int(
        __import__("os").environ.get(
            "PORT",
            "10000",
        )
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

    api_task = asyncio.create_task(
        server.serve()
    )

    # --------------------------------------------------------
    # TELEGRAM POLLING
    # --------------------------------------------------------

    logger.info(
        "Telegram polling started"
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        logger.info(
            "Stopping bot..."
        )

        scanner_task.cancel()
        result_task.cancel()
        api_task.cancel()

        for task in (
            scanner_task,
            result_task,
            api_task,
        ):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "Background task shutdown error"
                )

        try:
            await market.close()
        except Exception:
            logger.exception(
                "Market close failed"
            )

        try:
            await bot.session.close()
        except Exception:
            logger.exception(
                "Bot session close failed"
            )

        await close_database()

        logger.info(
            "Pocket Option Signal Bot stopped"
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

    except Exception:
        logger.exception(
            "Fatal application error"
        )
