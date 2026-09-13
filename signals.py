from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from config import config
from market import Candle


# ============================================================
# ЧЕСТНЫЙ SIGNAL ENGINE
# ============================================================

MIN_WINRATE = 80.0

# Минимальное количество исторических аналогов.
MIN_HISTORY = 30

# Минимальное количество WIN.
MIN_WINS = 24

# Насколько текущий technical score должен быть похож
# на исторический score.
SCORE_TOLERANCE = 8.0

# Минимальное преимущество направления.
MIN_DIRECTION_EDGE = 12.0

# Минимальное техническое качество.
MIN_TECHNICAL_SCORE = 75.0

# Максимальное число исторических точек.
MAX_HISTORY_POINTS = 350


# ============================================================
# RESULT
# ============================================================

@dataclass(slots=True)
class SignalResult:
    pair: str
    timeframe: int
    direction: str

    # Это НЕ сумма индикаторов.
    # Это консервативная историческая оценка.
    probability: float

    # Отдельно техническое качество.
    quality: float

    entry_time: datetime
    close_time: datetime

    entry_price: float | None

    reasons: list[str]


# ============================================================
# TIME
# ============================================================

def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _next_minute(
    moment: datetime | None = None,
) -> datetime:

    now = _utc(
        moment or datetime.now(timezone.utc)
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

    now = _utc(
        moment or datetime.now(timezone.utc)
    )

    closed = []

    for candle in candles:

        start = _utc(candle.time)

        if start + timedelta(minutes=1) <= now:
            closed.append(candle)

    if not closed:
        return None

    return max(
        closed,
        key=lambda x: _utc(x.time),
    )


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period: int):

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

    alpha = 2.0 / (period + 1.0)

    result[period - 1] = np.mean(
        values[:period]
    )

    for i in range(period, len(values)):

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

            result[i] = (
                100.0
                if avg_gain[i] > 0
                else 50.0
            )

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
            np.abs(highs - previous),
            np.abs(lows - previous),
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
# AGGREGATION
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
        key=lambda x: _utc(x.time),
    )

    if timeframe <= 1:
        result = list(candles)

    else:

        seconds = timeframe * 60

        buckets: dict[int, list[Candle]] = {}

        for candle in candles:

            timestamp = int(
                _utc(candle.time).timestamp()
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
                key=lambda x: _utc(x.time),
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

    now = datetime.now(timezone.utc)

    if result:

        last = result[-1]

        if (
            _utc(last.time)
            + timedelta(minutes=timeframe)
            > now
        ):
            result.pop()

    return result


# ============================================================
# TECHNICAL SETUP
# ============================================================

def _technical_setup(
    data: list[Candle],
) -> tuple[str | None, float, list[str]]:

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

    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    ema50 = ema(close, 50)

    if not (
        np.isfinite(ema9[-1])
        and np.isfinite(ema21[-1])
        and np.isfinite(ema50[-1])
    ):
        return None, 0.0, []

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi14 = rsi(close, 14)
    current_rsi = float(rsi14[-1])

    if not np.isfinite(current_rsi):
        return None, 0.0, []

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr14 = atr(data, 14)

    if not np.isfinite(atr14[-1]):
        return None, 0.0, []

    current_atr = float(atr14[-1])
    price = float(close[-1])

    if current_atr <= 0:
        return None, 0.0, []

    atr_percent = (
        current_atr
        / price
        * 100.0
    )

    if atr_percent < 0.01:
        return None, 0.0, []

    if atr_percent > 5.0:
        return None, 0.0, []

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    ema12 = ema(close, 12)
    ema26 = ema(close, 26)

    macd = ema12 - ema26

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

    macd_value = float(macd[-1])

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
            * (price - lowest)
            / (highest - lowest)
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

    bullish = (
        candle.close > candle.open
    )

    bearish = (
        candle.close < candle.open
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
    # SCORE
    # --------------------------------------------------------

    up = 0.0
    down = 0.0

    reasons_up: list[str] = []
    reasons_down: list[str] = []

    # EMA
    if ema9[-1] > ema21[-1] > ema50[-1]:

        up += 18
        reasons_up.append(
            "EMA9 > EMA21 > EMA50"
        )

    elif ema9[-1] < ema21[-1] < ema50[-1]:

        down += 18
        reasons_down.append(
            "EMA9 < EMA21 < EMA50"
        )

    elif ema9[-1] > ema21[-1]:

        up += 8
        reasons_up.append(
            "EMA9 выше EMA21"
        )

    elif ema9[-1] < ema21[-1]:

        down += 8
        reasons_down.append(
            "EMA9 ниже EMA21"
        )

    # EMA slope
    if ema21_slope > 0:

        up += 8
        reasons_up.append(
            "EMA21 растёт"
        )

    elif ema21_slope < 0:

        down += 8
        reasons_down.append(
            "EMA21 снижается"
        )

    if ema50_slope > 0:
        up += 5

    elif ema50_slope < 0:
        down += 5

    # RSI
    if 53 <= current_rsi <= 67:

        up += 12
        reasons_up.append(
            f"RSI {current_rsi:.1f}"
        )

    elif 33 <= current_rsi <= 47:

        down += 12
        reasons_down.append(
            f"RSI {current_rsi:.1f}"
        )

    elif current_rsi < 30:

        up += 5
        reasons_up.append(
            "RSI сильно перепродан"
        )

    elif current_rsi > 70:

        down += 5
        reasons_down.append(
            "RSI сильно перекуплен"
        )

    # MACD
    if (
        macd_value > macd_signal
        and macd_hist > 0
    ):

        up += 12
        reasons_up.append(
            "MACD подтверждает рост"
        )

    elif (
        macd_value < macd_signal
        and macd_hist < 0
    ):

        down += 12
        reasons_down.append(
            "MACD подтверждает падение"
        )

    # Momentum
    if momentum > 0:

        up += 8
        reasons_up.append(
            "Положительный momentum"
        )

    elif momentum < 0:

        down += 8
        reasons_down.append(
            "Отрицательный momentum"
        )

    if short_momentum > 0:
        up += 4

    elif short_momentum < 0:
        down += 4

    # Stochastic
    if 55 <= stochastic <= 85:
        up += 6

    elif 15 <= stochastic <= 45:
        down += 6

    # Bollinger
    if (
        price > bb_middle
        and price < bb_upper
    ):
        up += 5

    elif (
        price < bb_middle
        and price > bb_lower
    ):
        down += 5

    if price >= bb_upper:
        up -= 5

    if price <= bb_lower:
        down -= 5

    # Candle
    if (
        bullish
        and body_ratio >= 0.55
        and close_position >= 0.65
    ):

        up += 7
        reasons_up.append(
            "Сильная бычья свеча"
        )

    elif (
        bearish
        and body_ratio >= 0.55
        and close_position <= 0.35
    ):

        down += 7
        reasons_down.append(
            "Сильная медвежья свеча"
        )

    # Volume
    if volume_ratio >= 1.20:

        if up > down:

            up += 4
            reasons_up.append(
                "Объём выше среднего"
            )

        elif down > up:

            down += 4
            reasons_down.append(
                "Объём выше среднего"
            )

    # Resistance/support
    if (
        resistance > support
        and resistance_distance
        < current_atr * 0.35
    ):
        up -= 6

    if (
        resistance > support
        and support_distance
        < current_atr * 0.35
    ):
        down -= 6

    up = max(
        0.0,
        min(100.0, up),
    )

    down = max(
        0.0,
        min(100.0, down),
    )

    if up >= down:

        if (
            up - down
            < MIN_DIRECTION_EDGE
        ):
            return None, 0.0, []

        direction = "UP"
        score = up
        reasons = reasons_up

    else:

        if (
            down - up
            < MIN_DIRECTION_EDGE
        ):
            return None, 0.0, []

        direction = "DOWN"
        score = down
        reasons = reasons_down

    score = float(
        min(100.0, score)
    )

    if score < MIN_TECHNICAL_SCORE:
        return None, score, []

    # Жёсткая проверка momentum.
    if direction == "UP":

        if (
            momentum <= 0
            and short_momentum <= 0
        ):
            return None, score, []

    else:

        if (
            momentum >= 0
            and short_momentum >= 0
        ):
            return None, score, []

    return (
        direction,
        score,
        reasons,
    )


# ============================================================
# WILSON LOWER BOUND
# ============================================================

def _wilson_lower_bound(
    wins: int,
    total: int,
) -> float:

    if total <= 0:
        return 0.0

    p = wins / total

    z = 1.96

    denominator = (
        1.0
        + z * z / total
    )

    centre = (
        p
        + z * z / (2.0 * total)
    )

    spread = z * np.sqrt(
        (
            p * (1.0 - p)
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

    lower = (
        centre - spread
    ) / denominator

    return float(
        max(
            0.0,
            min(1.0, lower),
        )
    )


# ============================================================
# HISTORICAL TEST
# ============================================================

def _historical_probability(
    data: list[Candle],
    timeframe: int,
    current_score: float,
    current_direction: str,
) -> tuple[float, int, int]:

    timeframe = int(timeframe)

    if len(data) < 100:
        return 0.0, 0, 0

    wins = 0
    losses = 0

    start = 60

    end = (
        len(data)
        - timeframe
        - 1
    )

    if end <= start:
        return 0.0, 0, 0

    if end - start > MAX_HISTORY_POINTS:

        start = (
            end
            - MAX_HISTORY_POINTS
        )

    for i in range(
        start,
        end,
    ):

        history = data[:i + 1]

        (
            direction,
            score,
            _,
        ) = _technical_setup(
            history
        )

        if direction is None:
            continue

        if direction != current_direction:
            continue

        if (
            abs(
                score
                - current_score
            )
            > SCORE_TOLERANCE
        ):
            continue

        entry = float(
            data[i].close
        )

        future_index = (
            i + timeframe
        )

        if future_index >= len(data):
            continue

        close = float(
            data[future_index].close
        )

        # DRAW исключаем из denominator.
        if close == entry:
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

    lower = _wilson_lower_bound(
        wins,
        total,
    )

    probability = (
        lower * 100.0
    )

    return (
        round(
            max(
                0.0,
                min(
                    100.0,
                    probability,
                ),
            ),
            1,
        ),
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
            timeframe = int(timeframe)
        except (
            TypeError,
            ValueError,
        ):
            return None

        configured_timeframes = getattr(
            config,
            "timeframes",
            [],
        )

        try:

            allowed = [
                int(x)
                for x in configured_timeframes
            ]

        except Exception:

            allowed = []

        if timeframe not in allowed:
            return None

        if not candles:
            return None

        # ----------------------------------------------------
        # M1 ENTRY
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
        # TIMEFRAME DATA
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

        if technical_score < MIN_TECHNICAL_SCORE:
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

        # Недостаточно истории = НЕТ СИГНАЛА.
        if sample_size < MIN_HISTORY:
            return None

        if wins < MIN_WINS:
            return None

        # Жёсткий порог 80%.
        if historical_probability < MIN_WINRATE:
            return None

        # Дополнительная защита.
        if technical_score < 75.0:
            return None

        final_reasons = list(
            reasons
        )

        final_reasons.append(
            f"История: {wins}/{sample_size} WIN"
        )

        final_reasons.append(
            (
                "Консервативный "
                f"исторический winrate: "
                f"{historical_probability:.1f}%"
            )
        )

        final_reasons.append(
            f"Минимальный фильтр: {MIN_WINRATE:.0f}%"
        )

        return SignalResult(
            pair=str(pair),
            timeframe=timeframe,
            direction=direction,
            probability=round(
                historical_probability,
                1,
            ),
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
# PUBLIC HELPER
# ============================================================

def calculate_signal(
    pair: str,
    timeframe: int,
    candles: list[Candle],
) -> SignalResult | None:

    return SignalEngine().analyze(
        pair=pair,
        timeframe=timeframe,
        candles=candles,
    )