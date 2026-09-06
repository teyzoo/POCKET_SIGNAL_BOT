from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from config import config
from market import Candle


@dataclass(slots=True)
class SignalResult:
    pair: str
    timeframe: int
    direction: str

    # Техническая уверенность.
    # НЕ WINRATE.
    probability: float

    quality: float

    entry_time: datetime
    close_time: datetime

    entry_price: float | None

    reasons: list[str]


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
        key=lambda x: _utc_datetime(
            x.time
        ),
    )


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
            + (1 - alpha)
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
        0,
    )

    losses = np.maximum(
        -delta,
        0,
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
            result[i] = 100.0

        else:

            rs = (
                avg_gain[i]
                / avg_loss[i]
            )

            result[i] = (
                100
                - 100 / (1 + rs)
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
            abs(highs - previous),
            abs(lows - previous),
        ),
    )

    tr[0] = highs[0] - lows[0]

    return ema(
        tr,
        period,
    )


def aggregate_candles(
    candles: list[Candle],
    timeframe: int,
) -> list[Candle]:

    timeframe = int(timeframe)

    if not candles:
        return []

    candles = sorted(
        candles,
        key=lambda x: _utc_datetime(
            x.time
        ),
    )

    if timeframe <= 1:
        result = list(candles)

    else:

        seconds = timeframe * 60

        buckets = {}

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

        for bucket in sorted(
            buckets
        ):

            group = sorted(
                buckets[bucket],
                key=lambda x:
                    _utc_datetime(
                        x.time
                    ),
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


class SignalEngine:

    MIN_CANDLES = 60

    def analyze(
        self,
        pair: str,
        timeframe: int,
        candles: list[Candle],
    ) -> SignalResult | None:

        timeframe = int(timeframe)

        if timeframe not in config.timeframes:
            return None

        if not candles:
            return None

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

        # Вход всегда на следующей
        # полной минуте реального UTC.
        entry_time = _next_minute(
            now
        )

        close_time = (
            entry_time
            + timedelta(
                minutes=timeframe
            )
        )

        data = aggregate_candles(
            candles,
            timeframe,
        )

        if len(data) < self.MIN_CANDLES:
            return None

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
            return None

        if np.any(close <= 0):
            return None

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

        rsi14 = rsi(
            close,
            14,
        )

        atr14 = atr(
            data,
            14,
        )

        if not np.isfinite(
            atr14[-1]
        ):
            return None

        current_atr = float(
            atr14[-1]
        )

        price = float(
            close[-1]
        )

        if current_atr <= 0:
            return None

        atr_percent = (
            current_atr
            / price
            * 100
        )

        # Слишком мёртвый или аномально
        # волатильный рынок пропускаем.
        if (
            atr_percent < 0.01
            or atr_percent > 5.0
        ):
            return None

        # -----------------------------
        # MACD
        # -----------------------------

        ema12 = ema(
            close,
            12,
        )

        ema26 = ema(
            close,
            26,
        )

        macd = (
            ema12 - ema26
        )

        valid = macd[
            np.isfinite(macd)
        ]

        if len(valid) < 20:
            return None

        macd_signal_array = ema(
            valid,
            9,
        )

        if not np.isfinite(
            macd_signal_array[-1]
        ):
            return None

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

        # -----------------------------
        # Bollinger
        # -----------------------------

        if len(close) < 20:
            return None

        window = close[-20:]

        bb_middle = float(
            np.mean(window)
        )

        bb_std = float(
            np.std(window)
        )

        bb_upper = (
            bb_middle
            + 2 * bb_std
        )

        bb_lower = (
            bb_middle
            - 2 * bb_std
        )

        # -----------------------------
        # Stochastic
        # -----------------------------

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
                100
                * (price - lowest)
                / (highest - lowest)
            )

        # -----------------------------
        # Momentum
        # -----------------------------

        lb = min(
            12,
            len(close) - 1,
        )

        momentum = (
            price
            - close[-1 - lb]
        )

        short_lb = min(
            3,
            len(close) - 1,
        )

        short_momentum = (
            price
            - close[-1 - short_lb]
        )

        # -----------------------------
        # EMA slope
        # -----------------------------

        ema21_slope = (
            ema21[-1]
            - ema21[-4]
            if len(ema21) >= 4
            else 0
        )

        # -----------------------------
        # Support / resistance
        # -----------------------------

        support = float(
            np.min(low[-20:])
        )

        resistance = float(
            np.max(high[-20:])
        )

        # -----------------------------
        # Candle
        # -----------------------------

        candle = data[-1]

        candle_range = (
            candle.high
            - candle.low
        )

        if candle_range > 0:
            body_ratio = abs(
                candle.close
                - candle.open
            ) / candle_range
        else:
            body_ratio = 0.0

        # -----------------------------
        # Volume
        # -----------------------------

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

        up_score = 0.0
        down_score = 0.0

        reasons_up = []
        reasons_down = []

        # EMA trend
        if (
            ema9[-1] > ema21[-1]
            > ema50[-1]
        ):

            up_score += 20
            reasons_up.append(
                "EMA9 > EMA21 > EMA50"
            )

        elif (
            ema9[-1] < ema21[-1]
            < ema50[-1]
        ):

            down_score += 20
            reasons_down.append(
                "EMA9 < EMA21 < EMA50"
            )

        elif ema9[-1] > ema21[-1]:

            up_score += 7
            reasons_up.append(
                "EMA9 выше EMA21"
            )

        elif ema9[-1] < ema21[-1]:

            down_score += 7
            reasons_down.append(
                "EMA9 ниже EMA21"
            )

        # EMA slope
        if ema21_slope > 0:
            up_score += 8
            reasons_up.append(
                "EMA21 растёт"
            )

        elif ema21_slope < 0:
            down_score += 8
            reasons_down.append(
                "EMA21 снижается"
            )

        # RSI
        current_rsi = float(
            rsi14[-1]
        )

        if 52 <= current_rsi <= 68:

            up_score += 15
            reasons_up.append(
                f"RSI {current_rsi:.1f}"
            )

        elif 32 <= current_rsi <= 48:

            down_score += 15
            reasons_down.append(
                f"RSI {current_rsi:.1f}"
            )

        elif current_rsi < 30:

            up_score += 10
            reasons_up.append(
                "RSI перепродан"
            )

        elif current_rsi > 70:

            down_score += 10
            reasons_down.append(
                "RSI перекуплен"
            )

        # MACD
        if macd_value > macd_signal:

            up_score += 15
            reasons_up.append(
                "MACD bullish"
            )

        elif macd_value < macd_signal:

            down_score += 15
            reasons_down.append(
                "MACD bearish"
            )

        if macd_hist > 0:
            up_score += 5

        elif macd_hist < 0:
            down_score += 5

        # Bollinger
        if price <= bb_lower:

            up_score += 10
            reasons_up.append(
                "Цена у нижней Bollinger"
            )

        elif price >= bb_upper:

            down_score += 10
            reasons_down.append(
                "Цена у верхней Bollinger"
            )

        elif price > bb_middle:

            up_score += 4

        elif price < bb_middle:

            down_score += 4

        # Stochastic
        if stochastic < 20:

            up_score += 10
            reasons_up.append(
                "Stochastic перепродан"
            )

        elif stochastic > 80:

            down_score += 10
            reasons_down.append(
                "Stochastic перекуплен"
            )

        elif stochastic > 50:

            up_score += 4

        else:

            down_score += 4

        # Momentum
        if momentum > 0:

            up_score += 10
            reasons_up.append(
                "Положительный momentum"
            )

        elif momentum < 0:

            down_score += 10
            reasons_down.append(
                "Отрицательный momentum"
            )

        # Short momentum
        if short_momentum > 0:

            up_score += 5

        elif short_momentum < 0:

            down_score += 5

        # Support / resistance
        distance_support = (
            abs(price - support)
            / price
            * 100
        )

        distance_resistance = (
            abs(resistance - price)
            / price
            * 100
        )

        if distance_support < 0.15:

            up_score += 10
            reasons_up.append(
                "Цена возле поддержки"
            )

        if distance_resistance < 0.15:

            down_score += 10
            reasons_down.append(
                "Цена возле сопротивления"
            )

        # Candle confirmation
        if (
            candle.close
            > candle.open
            and body_ratio >= 0.25
        ):

            up_score += 5
            reasons_up.append(
                "Бычья свеча"
            )

        elif (
            candle.close
            < candle.open
            and body_ratio >= 0.25
        ):

            down_score += 5
            reasons_down.append(
                "Медвежья свеча"
            )

        # Volume
        if volume_ratio >= 1.20:

            if up_score >= down_score:
                up_score += 5
                reasons_up.append(
                    "Повышенный объём"
                )
            else:
                down_score += 5
                reasons_down.append(
                    "Повышенный объём"
                )

        # -----------------------------
        # Direction
        # -----------------------------

        if up_score >= down_score:

            direction = "UP"
            score = up_score
            opposite = down_score
            reasons = reasons_up

        else:

            direction = "DOWN"
            score = down_score
            opposite = up_score
            reasons = reasons_down

        gap = score - opposite

        # Слабое преимущество запрещаем.
        if gap < 15:
            return None

        # -----------------------------
        # Quality
        # -----------------------------

        quality = min(
            100.0,
            max(
                0.0,
                score * 1.25,
            ),
        )

        quality += min(
            8.0,
            gap * 0.20,
        )

        if body_ratio < 0.25:
            quality -= 5

        if volume_ratio >= 1.20:
            quality += 3

        quality = min(
            100.0,
            max(
                0.0,
                quality,
            ),
        )

        # -----------------------------
        # Technical confidence
        # -----------------------------

        confirmation_count = len(
            reasons
        )

        probability = (
            50
            + quality * 0.38
            + min(gap, 30) * 0.18
            + min(
                5,
                confirmation_count * 0.5,
            )
        )

        probability = min(
            92.0,
            max(
                50.0,
                probability,
            ),
        )

        # -----------------------------
        # Strict filters
        # -----------------------------

        if quality < float(
            config.min_signal_score
        ):
            return None

        if probability < float(
            config.min_probability
        ):
            return None

        if not reasons:
            return None

        return SignalResult(
            pair=pair,
            timeframe=timeframe,
            direction=direction,
            probability=float(
                probability
            ),
            quality=float(
                quality
            ),
            entry_time=entry_time,
            close_time=close_time,
            entry_price=entry_price,
            reasons=reasons[:8],
        )
