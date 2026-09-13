from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from config import config
from market import Candle


# ============================================================
# НАСТРОЙКИ ЧЕСТНОГО СИГНАЛА
# ============================================================

MIN_WINRATE = 80.0

# Минимум исторических аналогичных ситуаций.
MIN_HISTORY = 30

# Для дополнительной защиты от случайных 100%.
MIN_WINS = 24

# Максимальное расстояние между текущим score
# и историческим score.
SCORE_TOLERANCE = 7.5

# Не выдавать сигнал, если направления слишком близки.
MIN_DIRECTION_EDGE = 12.0

# Минимальное качество технической картины.
MIN_TECHNICAL_SCORE = 72.0

# Минимальная разница между быстрым и медленным
# моментумом.
MIN_MOMENTUM_EDGE = 0.0


# ============================================================
# RESULT
# ============================================================

@dataclass(slots=True)
class SignalResult:
    pair: str
    timeframe: int
    direction: str

    # ВАЖНО:
    # probability теперь означает
    # консервативную историческую вероятность,
    # а НЕ просто сумму индикаторов.
    probability: float

    # Техническое качество setup.
    quality: float

    entry_time: datetime
    close_time: datetime

    entry_price: float | None

    reasons: list[str]


# ============================================================
# TIME
# ============================================================

def _utc_datetime(
    value: datetime,
) -> datetime:

    if value.tzinfo is None:
        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


def _next_minute(
    moment: datetime | None = None,
) -> datetime:

    now = _utc_datetime(
        moment
        or datetime.now(timezone.utc)
    )

    return (
        now.replace(
            second=0,
            microsecond=0,
        )
        + timedelta(minutes=1)
    )


def _last_closed_m1(
    candles: list[Candle],
    moment: datetime | None = None,
) -> Candle | None:

    if not candles:
        return None

    now = _utc_datetime(
        moment
        or datetime.now(timezone.utc)
    )

    closed = [
        candle
        for candle in candles
        if (
            _utc_datetime(candle.time)
            + timedelta(minutes=1)
            <= now
        )
    ]

    if not closed:
        return None

    return max(
        closed,
        key=lambda x: _utc_datetime(x.time)
    )


# ============================================================
# INDICATORS
# ============================================================

def ema(
    values,
    period: int,
):

    values = np.asarray(
        values,
        dtype=float,
    )

    result = np.full(
        len(values),
        np.nan,
    )

    if len(values) < period:
        return result

    alpha = 2.0 / (
        period + 1
    )

    result[period - 1] = np.mean(
        values[:period]
    )

    for i in range(
        period,
        len(values),
    ):
        result[i] = (
            alpha * values[i]
            + (1.0 - alpha)
            * result[i - 1]
        )

    return result


def rsi(
    values,
    period: int = 14,
):

    values = np.asarray(
        values,
        dtype=float,
    )

    result = np.full(
        len(values),
        np.nan,
    )

    if len(values) < period + 1:
        return result

    delta = np.diff(
        values,
        prepend=values[0],
    )

    gains = np.maximum(
        delta,
        0.0,
    )

    losses = np.maximum(
        -delta,
        0.0,
    )

    avg_gain = np.full(
        len(values),
        np.nan,
    )

    avg_loss = np.full(
        len(values),
        np.nan,
    )

    avg_gain[period] = np.mean(
        gains[1:period + 1]
    )

    avg_loss[period] = np.mean(
        losses[1:period + 1]
    )

    for i in range(
        period + 1,
        len(values),
    ):

        avg_gain[i] = (
            (
                avg_gain[i - 1]
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss[i] = (
            (
                avg_loss[i - 1]
                * (period - 1)
            )
            + losses[i]
        ) / period

    for i in range(
        period,
        len(values),
    ):

        if avg_loss[i] == 0:

            if avg_gain[i] > 0:
                result[i] = 100.0
            else:
                result[i] = 50.0

        else:

            rs = (
                avg_gain[i]
                / avg_loss[i]
            )

            result[i] = (
                100.0
                - 100.0 / (1.0 + rs)
            )

    return result


def atr(
    candles: list[Candle],
    period: int = 14,
):

    if len(candles) < period + 1:
        return np.full(
            len(candles),
            np.nan,
        )

    highs = np.asarray(
        [c.high for c in candles],
        dtype=float,
    )

    lows = np.asarray(
        [c.low for c in candles],
        dtype=float,
    )

    closes = np.asarray(
        [c.close for c in candles],
        dtype=float,
    )

    previous = np.roll(
        closes,
        1,
    )

    tr = np.maximum(
        highs - lows,
        np.maximum(
            np.abs(
                highs - previous
            ),
            np.abs(
                lows - previous
            ),
        ),
    )

    tr[0] = (
        highs[0]
        - lows[0]
    )

    return ema(
        tr,
        period,
    )


# ============================================================
# CANDLE AGGREGATION
# ============================================================

def aggregate_candles(
    candles: list[Candle],
    timeframe: int,
) -> list[Candle]:

    timeframe = int(timeframe)

    if not candles:
        return []

    candles = sorted(
        candles,
        key=lambda x: _utc_datetime(x.time),
    )

    if timeframe <= 1:

        result = list(candles)

    else:

        seconds = timeframe * 60

        buckets: dict[int, list[Candle]] = {}

        for candle in candles:

            timestamp = int(
                _utc_datetime(
                    candle.time
                ).timestamp()
            )

            bucket = (
                timestamp // seconds
            ) * seconds

            buckets.setdefault(
                bucket,
                [],
            ).append(candle)

        result = []

        for bucket in sorted(buckets):

            group = sorted(
                buckets[bucket],
                key=lambda x:
                    _utc_datetime(x.time),
            )

            if not group:
                continue

            result.append(
                Candle(
                    time=datetime.fromtimestamp(
                        bucket,
                        tz=timezone.utc,
                    ),
                    open=group[0].open,
                    high=max(
                        x.high
                        for x in group
                    ),
                    low=min(
                        x.low
                        for x in group
                    ),
                    close=group[-1].close,
                    volume=sum(
                        x.volume
                        for x in group
                    ),
                )
            )

    now = datetime.now(
        timezone.utc
    )

    if result:

        last = result[-1]

        if (
            _utc_datetime(last.time)
            + timedelta(
                minutes=timeframe
            )
            > now
        ):
            result.pop()

    return result


# ============================================================
# TECHNICAL ANALYSIS
# ============================================================

def _technical_setup(
    data: list[Candle],
) -> tuple[
    str | None,
    float,
    list[str],
]:

    if len(data) < 60:
        return None, 0.0, []

    close = np.asarray(
        [c.close for c in data],
        dtype=float,
    )

    high = np.asarray(
        [c.high for c in data],
        dtype=float,
    )

    low = np.asarray(
        [c.low for c in data],
        dtype=float,
    )

    volume = np.asarray(
        [c.volume for c in data],
        dtype=float,
    )

    if not (
        np.all(np.isfinite(close))
        and np.all(np.isfinite(high))
        and np.all(np.isfinite(low))
    ):
        return None, 0.0, []

    if np.any(close <= 0):
        return None, 0.0, []

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    ema9 = ema(
        close,
        9,
    )

    ema21 = ema(
        close,
        21,
    )

    ema50 = ema(
        close,
        50,
    )

    if not (
        np.isfinite(ema9[-1])
        and np.isfinite(ema21[-1])
        and np.isfinite(ema50[-1])
    ):
        return None, 0.0, []

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi14 = rsi(
        close,
        14,
    )

    current_rsi = float(
        rsi14[-1]
    )

    if not np.isfinite(
        current_rsi
    ):
        return None, 0.0, []

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr14 = atr(
        data,
        14,
    )

    if not np.isfinite(
        atr14[-1]
    ):
        return None, 0.0, []

    current_atr = float(
        atr14[-1]
    )

    price = float(
        close[-1]
    )

    if current_atr <= 0:
        return None, 0.0, []

    atr_percent = (
        current_atr
        / price
        * 100.0
    )

    # Мёртвый рынок.
    if atr_percent < 0.01:
        return None, 0.0, []

    # Аномальная волатильность.
    if atr_percent > 5.0:
        return None, 0.0, []

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    ema12 = ema(
        close,
        12,
    )

    ema26 = ema(
        close,
        26,
    )

    macd = (
        ema12
        - ema26
    )

    valid_macd = macd[
        np.isfinite(macd)
    ]

    if len(valid_macd) < 20:
        return None, 0.0, []

    macd_signal_array = ema(
        valid_macd,
        9,
    )

    if not np.isfinite(
        macd_signal_array[-1]
    ):
        return None, 0.0, []

    macd_value = float(
        macd[-1]
    )

    macd_signal = float(
        macd_signal_array[-1]
    )

    macd_hist = (
        macd_value
        - macd_signal
    )

    # --------------------------------------------------------
    # BOLLINGER
    # --------------------------------------------------------

    window = close[-20:]

    bb_middle = float(
        np.mean(window)
    )

    bb_std = float(
        np.std(window)
    )

    bb_upper = (
        bb_middle
        + 2.0 * bb_std
    )

    bb_lower = (
        bb_middle
        - 2.0 * bb_std
    )

    # --------------------------------------------------------
    # STOCHASTIC
    # --------------------------------------------------------

    highest = float(
        np.max(high[-14:])
    )

    lowest = float(
        np.min(low[-14:])
    )

    if highest == lowest:

        stochastic = 50.0

    else:

        stochastic = (
            100.0
            * (
                price
                - lowest
            )
            / (
                highest
                - lowest
            )
        )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    long_lb = min(
        12,
        len(close) - 1,
    )

    short_lb = min(
        3,
        len(close) - 1,
    )

    momentum = (
        price
        - close[-1 - long_lb]
    )

    short_momentum = (
        price
        - close[-1 - short_lb]
    )

    # --------------------------------------------------------
    # EMA SLOPE
    # --------------------------------------------------------

    ema21_slope = (
        ema21[-1]
        - ema21[-4]
    )

    ema50_slope = (
        ema50[-1]
        - ema50[-5]
    )

    # --------------------------------------------------------
    # CANDLE
    # --------------------------------------------------------

    candle = data[-1]

    candle_range = (
        candle.high
        - candle.low
    )

    if candle_range > 0:

        body_ratio = (
            abs(
                candle.close
                - candle.open
            )
            / candle_range
        )

        close_position = (
            candle.close
            - candle.low
        ) / candle_range

    else:

        body_ratio = 0.0
        close_position = 0.5

    bullish_candle = (
        candle.close
        > candle.open
    )

    bearish_candle = (
        candle.close
        < candle.open
    )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    volume_ratio = 1.0

    if len(volume) >= 20:

        avg_volume = float(
            np.mean(
                volume[-20:-1]
            )
        )

        if avg_volume > 0:

            volume_ratio = (
                float(volume[-1])
                / avg_volume
            )

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE
    # --------------------------------------------------------

    support = float(
        np.min(low[-20:])
    )

    resistance = float(
        np.max(high[-20:])
    )

    support_distance = (
        price - support
    )

    resistance_distance = (
        resistance - price
    )

    # --------------------------------------------------------
    # SCORES
    # --------------------------------------------------------

    up = 0.0
    down = 0.0

    reasons_up: list[str] = []
    reasons_down: list[str] = []

    # ========================================================
    # EMA TREND
    # ========================================================

    if (
        ema9[-1]
        > ema21[-1]
        > ema50[-1]
    ):

        up += 18.0

        reasons_up.append(
            "EMA9 > EMA21 > EMA50"
        )

    elif (
        ema9[-1]
        < ema21[-1]
        < ema50[-1]
    ):

        down += 18.0

        reasons_down.append(
            "EMA9 < EMA21 < EMA50"
        )

    elif ema9[-1] > ema21[-1]:

        up += 8.0

        reasons_up.append(
            "EMA9 выше EMA21"
        )

    elif ema9[-1] < ema21[-1]:

        down += 8.0

        reasons_down.append(
            "EMA9 ниже EMA21"
        )

    # ========================================================
    # EMA SLOPE
    # ========================================================

    if ema21_slope > 0:

        up += 8.0

        reasons_up.append(
            "EMA21 растёт"
        )

    elif ema21_slope < 0:

        down += 8.0

        reasons_down.append(
            "EMA21 снижается"
        )

    if ema50_slope > 0:

        up += 5.0

    elif ema50_slope < 0:

        down += 5.0

    # ========================================================
    # RSI
    # ========================================================

    if 53.0 <= current_rsi <= 67.0:

        up += 12.0

        reasons_up.append(
            f"RSI {current_rsi:.1f}"
        )

    elif 33.0 <= current_rsi <= 47.0:

        down += 12.0

        reasons_down.append(
            f"RSI {current_rsi:.1f}"
        )

    elif 48.0 <= current_rsi < 53.0:

        # Нейтральная зона.
        pass

    elif 47.0 < current_rsi <= 52.0:

        pass

    elif current_rsi < 30.0:

        up += 5.0

        reasons_up.append(
            "RSI сильно перепродан"
        )

    elif current_rsi > 70.0:

        down += 5.0

        reasons_down.append(
            "RSI сильно перекуплен"
        )

    # ========================================================
    # MACD
    # ========================================================

    if (
        macd_value > macd_signal
        and macd_hist > 0
    ):

        up += 12.0

        reasons_up.append(
            "MACD подтверждает рост"
        )

    elif (
        macd_value < macd_signal
        and macd_hist < 0
    ):

        down += 12.0

        reasons_down.append(
            "MACD подтверждает падение"
        )

    # ========================================================
    # MOMENTUM
    # ========================================================

    if momentum > 0:

        up += 8.0

        reasons_up.append(
            "Положительный momentum"
        )

    elif momentum < 0:

        down += 8.0

        reasons_down.append(
            "Отрицательный momentum"
        )

    if short_momentum > 0:

        up += 4.0

    elif short_momentum < 0:

        down += 4.0

    # ========================================================
    # STOCHASTIC
    # ========================================================

    if (
        55.0 <= stochastic <= 85.0
    ):

        up += 6.0

    elif (
        15.0 <= stochastic <= 45.0
    ):

        down += 6.0

    # ========================================================
    # BOLLINGER
    # ========================================================

    if (
        price > bb_middle
        and price < bb_upper
    ):

        up += 5.0

    elif (
        price < bb_middle
        and price > bb_lower
    ):

        down += 5.0

    # Не покупать прямо на верхней границе.
    if price >= bb_upper:

        up -= 5.0

    # Не продавать прямо на нижней границе.
    if price <= bb_lower:

        down -= 5.0

    # ========================================================
    # CANDLE CONFIRMATION
    # ========================================================

    if (
        bullish_candle
        and body_ratio >= 0.55
        and close_position >= 0.65
    ):

        up += 7.0

        reasons_up.append(
            "Сильная бычья свеча"
        )

    elif (
        bearish_candle
        and body_ratio >= 0.55
        and close_position <= 0.35
    ):

        down += 7.0

        reasons_down.append(
            "Сильная медвежья свеча"
        )

    # ========================================================
    # VOLUME
    # ========================================================

    if volume_ratio >= 1.20:

        if up > down:

            up += 4.0

            reasons_up.append(
                "Объём выше среднего"
            )

        elif down > up:

            down += 4.0

            reasons_down.append(
                "Объём выше среднего"
            )

    # ========================================================
    # SUPPORT / RESISTANCE
    # ========================================================

    # Если UP почти упёрся в сопротивление —
    # уменьшаем вероятность продолжения.
    if (
        resistance > support
        and resistance_distance
        < current_atr * 0.35
    ):

        up -= 6.0

    # Если DOWN почти упёрся в поддержку —
    # уменьшаем вероятность продолжения.
    if (
        resistance > support
        and support_distance
        < current_atr * 0.35
    ):

        down -= 6.0

    # ========================================================
    # NORMALIZE
    # ========================================================

    up = max(
        0.0,
        min(100.0, up),
    )

    down = max(
        0.0,
        min(100.0, down),
    )

    total = up + down

    if total <= 0:
        return None, 0.0, []

    if (
        up >= down
        and (
            up - down
            >= MIN_DIRECTION_EDGE
        )
    ):

        direction = "UP"
        technical_score = up
        reasons = reasons_up

    elif (
        down > up
        and (
            down - up
            >= MIN_DIRECTION_EDGE
        )
    ):

        direction = "DOWN"
        technical_score = down
        reasons = reasons_down

    else:

        return None, 0.0, []

    # ========================================================
    # FINAL TECHNICAL SCORE
    # ========================================================

    # Не превращаем score в fake probability.
    technical_score = float(
        min(
            100.0,
            technical_score,
        )
    )

    if (
        technical_score
        < MIN_TECHNICAL_SCORE
    ):

        return None, technical_score, []

    # Сильный конфликт momentum.
    if direction == "UP":

        if (
            momentum <= MIN_MOMENTUM_EDGE
            and short_momentum <= 0
        ):

            return None, technical_score, []

    else:

        if (
            momentum >= -MIN_MOMENTUM_EDGE
            and short_momentum >= 0
        ):

            return None, technical_score, []

    return (
        direction,
        technical_score,
        reasons,
    )


# ============================================================
# HISTORICAL BACKTEST
# ============================================================

def _historical_probability(
    data: list[Candle],
    timeframe: int,
    current_score: float,
    current_direction: str,
) -> tuple[
    float,
    int,
    int,
]:

    """
    Строит вероятность только по прошлым ситуациям.

    Ключевой принцип:

        Для исторической точки i
        используются только свечи <= i.

        Результат проверяется через timeframe
        свечей ПОСЛЕ i.

    Таким образом будущие свечи не попадают
    в расчёт технических признаков исторической точки.
    """

    timeframe = int(timeframe)

    if len(data) < 90:
        return 0.0, 0, 0

    wins = 0
    losses = 0

    start = 60

    # Оставляем достаточно данных после точки,
    # чтобы определить результат.
    end = (
        len(data)
        - timeframe
        - 1
    )

    if end <= start:
        return 0.0, 0, 0

    # Чтобы вычисление не становилось огромным.
    # Берём последние исторические точки.
    max_history_points = 250

    if end - start > max_history_points:

        start = (
            end
            - max_history_points
        )

    for i in range(
        start,
        end,
    ):

        historical_data = data[
            :i + 1
        ]

        (
            direction,
            score,
            _,
        ) = _technical_setup(
            historical_data
        )

        if direction is None:
            continue

        # Ситуация должна быть достаточно
        # похожа на текущую.
        if direction != current_direction:
            continue

        if abs(
            score
            - current_score
        ) > SCORE_TOLERANCE:
            continue

        entry = float(
            data[i].close
        )

        future_index = (
            i
            + timeframe
        )

        if future_index >= len(data):
            continue

        close = float(
            data[future_index].close
        )

        if close == entry:
            # DRAW не считаем WIN.
            # Это консервативный подход.
            continue

        if direction == "UP":

            if close > entry:
                wins += 1
            else:
                losses += 1

        else:

            if close < entry:
                wins += 1
            else:
                losses += 1

    total = wins + losses

    if total < MIN_HISTORY:
        return 0.0, wins, total

    # --------------------------------------------------------
    # Wilson lower bound
    # --------------------------------------------------------
    #
    # Это специально консервативная оценка.
    #
    # Например:
    #
    # 1/1 не превращается в 100%.
    # 2/2 не превращается в 100%.
    #
    # Чем меньше выборка, тем сильнее штраф.
    # --------------------------------------------------------

    p = wins / total

    z = 1.96

    denominator = (
        1.0
        + (
            z * z
            / total
        )
    )

    centre = (
        p
        + (
            z * z
            / (
                2.0 * total
            )
        )
    )

    spread = (
        z
        * np.sqrt(
            (
                p
                * (1.0 - p)
                / total
            )
            + (
                z * z
                / (
                    4.0
                    * total
                    * total
                )
            )
        )
    )

    lower = (
        centre
        - spread
    ) / denominator

    probability = (
        lower
        * 100.0
    )

    probability = float(
        max(
            0.0,
            min(
                100.0,
                probability,
            ),
        )
    )

    return (
        probability,
        wins,
        total,
    )


# ============================================================
# SIGNAL ENGINE
# ============================================================

class SignalEngine:

    MIN_CANDLES = 60

    def analyze(
        self,
        pair: str,
        timeframe: int,
        candles: list[Candle],
    ) -> SignalResult | None:

        try:
            timeframe = int(
                timeframe
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

        # ----------------------------------------------------
        # TIMEFRAME
        # ----------------------------------------------------

        if timeframe not in config.timeframes:
            return None

        if not candles:
            return None

        # ----------------------------------------------------
        # LAST CLOSED M1
        # ----------------------------------------------------

        now = datetime.now(
            timezone.utc
        )

        last_m1 = _last_closed_m1(
            candles,
            now,
        )

        if last_m1 is None:
            return None

        entry_price = float(
            last_m1.close
        )

        # ----------------------------------------------------
        # ENTRY / CLOSE
        # ----------------------------------------------------

        entry_time = _next_minute(
            now
        )

        close_time = (
            entry_time
            + timedelta(
                minutes=timeframe
            )
        )

        # ----------------------------------------------------
        # AGGREGATION
        # ----------------------------------------------------

        data = aggregate_candles(
            candles,
            timeframe,
        )

        if len(data) < self.MIN_CANDLES:
            return None

        # ----------------------------------------------------
        # TECHNICAL SETUP
        # ----------------------------------------------------

        (
            direction,
            technical_score,
            reasons,
        ) = _technical_setup(
            data
        )

        if direction is None:
            return None

        # ----------------------------------------------------
        # HARD TECHNICAL FILTER
        # ----------------------------------------------------

        if (
            technical_score
            < MIN_TECHNICAL_SCORE
        ):
            return None

        # ----------------------------------------------------
        # HISTORICAL PROBABILITY
        # ----------------------------------------------------

        (
            historical_probability,
            wins,
            sample_size,
        ) = _historical_probability(
            data=data,
            timeframe=timeframe,
            current_score=technical_score,
            current_direction=direction,
        )

        # ----------------------------------------------------
        # НЕ ХВАТАЕТ СТАТИСТИКИ
        # ----------------------------------------------------

        if sample_size < MIN_HISTORY:

            return None

        # ----------------------------------------------------
        # СЛИШКОМ МАЛО WIN
        # ----------------------------------------------------

        if wins < MIN_WINS:

            return None

        # ----------------------------------------------------
        # ЖЁСТКИЙ ПОРOГ 80%
        # ----------------------------------------------------

        if (
            historical_probability
            < MIN_WINRATE
        ):

            return None

        # ----------------------------------------------------
        # ДОПОЛНИТЕЛЬНАЯ ЗАЩИТА
        # ----------------------------------------------------

        # Даже если расчёт показывает 80+,
        # качество самой технической ситуации
        # не должно быть слабым.

        if technical_score < 75.0:
            return None

        # ----------------------------------------------------
        # REASONS
        # ----------------------------------------------------

        final_reasons = list(
            reasons
        )

        final_reasons.append(
            (
                "История: "
                f"{wins}/{sample_size} "
                "WIN"
            )
        )

        final_reasons.append(
            (
                "Консервативный "
                f"winrate: "
                f"{historical_probability:.1f}%"
            )
        )

        final_reasons.append(
            (
                "Фильтр: "
                f">= {MIN_WINRATE:.0f}%"
            )
        )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        return SignalResult(
            pair=str(pair),
            timeframe=timeframe,
            direction=direction,

            # Это уже НЕ fake technical score.
            # Это исторически проверенная
            # консервативная оценка.
            probability=round(
                historical_probability,
                1,
            ),

            # Отдельно сохраняем техническое
            # качество setup.
            quality=round(
                technical_score,
                1,
            ),

            entry_time=entry_time,
            close_time=close_time,

            entry_price=entry_price,

            reasons=final_reasons,
        )


# ============================================================
# PUBLIC HELPERS
# ============================================================

def calculate_signal(
    pair: str,
    timeframe: int,
    candles: list[Candle],
) -> SignalResult | None:

    """
    Совместимый публичный helper.
    """

    engine = SignalEngine()

    return engine.analyze(
        pair=pair,
        timeframe=timeframe,
        candles=candles,
    )