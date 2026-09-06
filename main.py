from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

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
    get_pending_signals,
    get_user,
    init_db,
    save_signal,
    settle_signal_by_price,
    update_user,
)

from market import PocketMarket
from signals import SignalEngine


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

MSK = ZoneInfo(
    "Europe/Moscow"
)

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()

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


app = FastAPI(
    title="Pocket Option Signal Bot"
)


@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected":
            market_is_connected(),
        "provider":
            market.provider,
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "service": "POCKET_SIGNAL_BOT",
        "market_connected":
            market_is_connected(),
        "provider":
            market.provider,
        "time":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


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
        and not market.client.closed
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
                "[MARKET] "
                "Подключение к источнику..."
            )

            connected = await asyncio.wait_for(
                market.connect(),
                timeout=25,
            )

            if not connected:
                MARKET_READY = False
                return False

            MARKET_READY = True

            logger.info(
                "[MARKET] "
                "✅ Подключено: %s",
                market.provider,
            )

            return True

        except asyncio.TimeoutError:

            MARKET_READY = False

            logger.error(
                "[MARKET] "
                "❌ Общий таймаут подключения"
            )

            return False

        except Exception as exc:

            MARKET_READY = False

            logger.exception(
                "[MARKET] "
                "Connection error: %s",
                exc,
            )

            return False


def get_config_pairs():

    pairs = getattr(
        config,
        "pairs",
        None,
    )

    if not pairs:
        raise RuntimeError(
            "В config.py отсутствует pairs"
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
            "В config.py отсутствует timeframes"
        )

    result = []

    for value in values:

        try:
            value = int(value)
        except (
            TypeError,
            ValueError,
        ):
            continue

        if value > 0:
            result.append(value)

    return result


def pair_name(symbol: str) -> str:

    for name, internal in (
        get_config_pairs()
    ):

        if internal == symbol:
            return name

    return symbol


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
    }:
        return "🟢 ВВЕРХ"

    if value in {
        "DOWN",
        "PUT",
        "SELL",
    }:
        return "🔴 ВНИЗ"

    return value


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


def candle_close_at_or_before(
    candles,
    target_time: datetime,
):

    target_time = utc_time(
        target_time
    )

    valid = []

    for candle in candles:

        start = utc_time(
            candle.time
        )

        close = (
            start
            + timedelta(
                minutes=1
            )
        )

        if close <= target_time:
            valid.append(candle)

    if not valid:
        return None

    candle = max(
        valid,
        key=lambda x:
            utc_time(x.time),
    )

    return float(
        candle.close
    )


async def pair_history_text(
    pair: str,
):

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
                f"📊 <b>История:</b> "
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
        "📊 <b>История:</b> "
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
        f"• {x}"
        for x in reasons[:8]
    )

    history = await pair_history_text(
        signal.pair
    )

    entry_time = getattr(
        signal,
        "entry_time",
        None,
    )

    close_time = getattr(
        signal,
        "close_time",
        None,
    )

    entry_text = (
        f"{msk_time(entry_time)} МСК"
        if isinstance(
            entry_time,
            datetime,
        )
        else "—"
    )

    close_text = (
        f"{msk_time(close_time)} МСК"
        if isinstance(
            close_time,
            datetime,
        )
        else "—"
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
        f"{entry_text}\n"

        f"⏰ <b>Закрытие:</b> "
        f"{close_text}\n\n"

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
        "⚠️ Техническая оценка "
        "не является гарантией результата."
    )

    return text


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
                        else
                        "🔴 Автосигналы"
                    ),
                    callback_data="auto_toggle",
                ),
            ],
        ]
    )


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

    for name, symbol in (
        get_config_pairs()
    ):

        row.append(
            InlineKeyboardButton(
                text=name,
                callback_data=(
                    f"sigp:{symbol}"
                ),
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


def signal_time_keyboard():

    rows = []
    row = []

    for timeframe in (
        get_config_timeframes()
    ):

        row.append(
            InlineKeyboardButton(
                text=f"{timeframe} мин",
                callback_data=(
                    f"sigt:{timeframe}"
                ),
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


def get_user_lock(
    user_id: int,
):

    if user_id not in USER_ANALYSIS_LOCKS:
        USER_ANALYSIS_LOCKS[
            user_id
        ] = asyncio.Lock()

    return USER_ANALYSIS_LOCKS[
        user_id
    ]


async def scan_pair(
    pair: str,
    timeframe: int | None = None,
):

    limit = int(
        getattr(
            config,
            "market_candle_limit",
            1600,
        )
    )

    candles = await market.get_candles(
        pair,
        1,
        limit,
    )

    timeframes = (
        [timeframe]
        if timeframe is not None
        else get_config_timeframes()
    )

    best = None

    for tf in timeframes:

        try:

            signal = engine.analyze(
                pair,
                int(tf),
                candles,
            )

        except Exception as exc:

            logger.exception(
                "[SIGNAL] %s tf=%s: %s",
                pair,
                tf,
                exc,
            )

            continue

        if signal is None:
            continue

        if best is None:

            best = signal

        elif (
            signal.quality,
            signal.probability,
        ) > (
            best.quality,
            best.probability,
        ):

            best = signal

    return best


async def scan_market(
    selected_pair: str,
    timeframe: int | None,
):

    if not await ensure_market_ready():
        return None, (
            "❌ <b>Рынок недоступен.</b>\n\n"
            "Источник рынка не ответил."
        )

    pairs = get_config_pairs()

    if selected_pair == "ANY":

        symbols = [
            symbol
            for _, symbol in pairs
        ]

    else:

        symbols = [
            selected_pair
        ]

    best = None

    for pair in symbols:

        try:

            signal = await scan_pair(
                pair,
                timeframe,
            )

            if signal is None:
                continue

            if best is None:

                best = signal

            elif (
                signal.quality,
                signal.probability,
            ) > (
                best.quality,
                best.probability,
            ):

                best = signal

        except Exception as exc:

            logger.warning(
                "[SCAN] %s: %s",
                pair,
                exc,
            )

    if best is None:

        return None, (
            "⚪ <b>Сильного сигнала сейчас нет.</b>\n\n"
            "Требование: "
            f"от {config.min_probability:.0f}%.\n\n"
            "Я не буду выдавать слабый "
            "сигнал только ради того, "
            "чтобы что-то показать."
        )

    return best, None


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
            "Выберите действие:"
        ),
        reply_markup=main_keyboard(),
    )


@dp.callback_query(
    F.data == "signal"
)
async def signal_button(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        (
            "💱 <b>ВЫБОР ПАРЫ</b>\n\n"
            "Выберите OTC-пару:"
        ),
        reply_markup=signal_pair_keyboard(),
    )


@dp.callback_query(
    F.data.startswith("sigp:")
)
async def pair_selected(
    callback: CallbackQuery,
):

    await callback.answer()

    value = callback.data.split(
        ":",
        1,
    )[1]

    USER_SELECTED_PAIRS[
        callback.from_user.id
    ] = value

    await callback.message.edit_text(
        (
            "⏱ <b>ВЫБОР ВРЕМЕНИ</b>\n\n"
            "Выберите экспирацию:"
        ),
        reply_markup=signal_time_keyboard(),
    )


@dp.callback_query(
    F.data.startswith("sigt:")
)
async def timeframe_selected(
    callback: CallbackQuery,
):

    await callback.answer(
        "Проверяю рынок..."
    )

    user_id = callback.from_user.id

    lock = get_user_lock(
        user_id
    )

    if lock.locked():

        await callback.message.edit_text(
            (
                "⏳ <b>Анализ уже выполняется.</b>\n\n"
                "Подождите завершения текущей проверки."
            ),
            reply_markup=main_keyboard(),
        )

        return

    async with lock:

        selected_pair = (
            USER_SELECTED_PAIRS.get(
                user_id,
                "ANY",
            )
        )

        raw_timeframe = callback.data.split(
            ":",
            1,
        )[1]

        if raw_timeframe == "ANY":
            timeframe = None
        else:
            try:
                timeframe = int(
                    raw_timeframe
                )
            except ValueError:
                timeframe = None

        await callback.message.edit_text(
            (
                "🔌 <b>ПРОВЕРКА РЫНКА</b>\n\n"
                "Подключение к источнику рынка..."
            )
        )

        signal, error = await scan_market(
            selected_pair,
            timeframe,
        )

        if signal is None:

            await callback.message.edit_text(
                error or (
                    "⚪ Сильного сигнала нет."
                ),
                reply_markup=main_keyboard(),
            )

            return

        try:

            await save_signal(
                signal
            )

        except Exception as exc:

            logger.exception(
                "[DATABASE] "
                "Не удалось сохранить сигнал: %s",
                exc,
            )

        text = await format_signal(
            signal
        )

        await callback.message.edit_text(
            text,
            reply_markup=main_keyboard(),
        )


@dp.callback_query(
    F.data == "auto_toggle"
)
async def auto_toggle(
    callback: CallbackQuery,
):

    global AUTO_SIGNALS

    AUTO_SIGNALS = not AUTO_SIGNALS

    await callback.answer(
        (
            "Автосигналы включены"
            if AUTO_SIGNALS
            else
            "Автосигналы выключены"
        )
    )

    await callback.message.edit_text(
        (
            "📈 <b>POCKET SIGNAL BOT</b>\n\n"
            "Настройки обновлены."
        ),
        reply_markup=main_keyboard(),
    )


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

    auto = (
        user.auto_signals
        if user
        else True
    )

    await callback.message.edit_text(
        (
            "⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"Автосигналы: "
            f"{'🟢 ВКЛ' if auto else '🔴 ВЫКЛ'}\n\n"
            f"Минимальная уверенность: "
            f"{config.min_probability:.0f}%\n"
            f"Минимальный Quality: "
            f"{config.min_signal_score:.0f}"
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Автосигналы",
                        callback_data="user_auto",
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


@dp.callback_query(
    F.data == "user_auto"
)
async def user_auto(
    callback: CallbackQuery,
):

    user = await get_user(
        callback.from_user.id
    )

    current = (
        user.auto_signals
        if user
        else True
    )

    await update_user(
        callback.from_user.id,
        auto_signals=not current,
    )

    await callback.answer(
        (
            "Автосигналы включены"
            if not current
            else
            "Автосигналы выключены"
        )
    )

    await settings_handler(
        callback
    )


@dp.callback_query(
    F.data == "back_main"
)
async def back_main(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        (
            "📈 <b>POCKET SIGNAL BOT</b>\n\n"
            "Выберите действие:"
        ),
        reply_markup=main_keyboard(),
    )


async def settlement_loop():

    logger.info(
        "[SETTLEMENT] "
        "Цикл расчёта результатов запущен"
    )

    while True:

        try:

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

                    candles = await market.get_candles(
                        signal.pair,
                        1,
                        30,
                    )

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

                except Exception as exc:

                    logger.warning(
                        "[SETTLEMENT] "
                        "signal=%s: %s",
                        signal.id,
                        exc,
                    )

        except Exception as exc:

            logger.exception(
                "[SETTLEMENT] %s",
                exc,
            )

        await asyncio.sleep(15)


async def auto_signal_loop():

    logger.info(
        "[AUTO] Автоматические сигналы запущены"
    )

    while True:

        try:

            if AUTO_SIGNALS:

                users = await get_access_users()

                pairs = get_config_pairs()

                best = None

                for _, pair in pairs:

                    try:

                        signal = await scan_pair(
                            pair,
                            None,
                        )

                        if signal is None:
                            continue

                        if best is None:

                            best = signal

                        elif (
                            signal.quality,
                            signal.probability,
                        ) > (
                            best.quality,
                            best.probability,
                        ):

                            best = signal

                    except Exception as exc:

                        logger.warning(
                            "[AUTO] %s: %s",
                            pair,
                            exc,
                        )

                if best is not None:

                    try:
                        await save_signal(
                            best
                        )
                    except Exception as exc:
                        logger.warning(
                            "[AUTO] "
                            "save_signal: %s",
                            exc,
                        )

                    text = await format_signal(
                        best
                    )

                    for user in users:

                        if not user.auto_signals:
                            continue

                        try:

                            await bot.send_message(
                                user.telegram_id,
                                text,
                            )

                        except Exception as exc:

                            logger.warning(
                                "[AUTO] Telegram "
                                "user=%s: %s",
                                user.telegram_id,
                                exc,
                            )

        except Exception as exc:

            logger.exception(
                "[AUTO] %s",
                exc,
            )

        # Следующая проверка.
        await asyncio.sleep(
            max(
                20,
                int(
                    config.scan_interval
                ),
            )
        )


async def startup():

    await init_db()

    logger.info(
        "[BOT] 🚀 Telegram bot запущен"
    )


async def shutdown():

    try:
        await market.close()
    except Exception:
        pass

    await close_database()

    try:
        await bot.session.close()
    except Exception:
        pass


async def bot_runner():

    await startup()

    settlement_task = asyncio.create_task(
        settlement_loop()
    )

    auto_task = asyncio.create_task(
        auto_signal_loop()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        settlement_task.cancel()
        auto_task.cancel()

        await shutdown()


def run():

    asyncio.run(
        bot_runner()
    )


if __name__ == "__main__":

    import threading

    def run_api():
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=(
                __import__(
                    "os"
                ).getenv(
                    "PORT",
                    "10000",
                )
            ),
        )

    api_thread = threading.Thread(
        target=run_api,
        daemon=True,
    )

    api_thread.start()

    run()
