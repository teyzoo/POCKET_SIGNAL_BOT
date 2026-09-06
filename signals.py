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

    # НЕ winrate.
    # Только техническая оценка текущей ситуации.
    probability: float

    # Техническое качество сигнала.
    quality: float

    # Реальный момент формирования сигнала.
    entry_time: datetime

    # Плановое время окончания экспирации.
    close_time: datetime

    reasons: list[str]


# ============================================================
# EMA
# ============================================================

def ema(values, period: int):
    values = np.asarray(values, dtype=float)

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
            + (1.0 - alpha) * result[i - 1]
        )

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period: int = 14):
    values = np.asarray(values, dtype=float)

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

    gain = np.maximum(
        delta,
        0.0,
    )

    loss = np.maximum(
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
        gain[1:period + 1]
    )

    avg_loss[period] = np.mean(
        loss[1:period + 1]
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
            + gain[i]
        ) / period

        avg_loss[i] = (
            (
                avg_loss[i - 1]
                * (period - 1)
            )
            + loss[i]
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
                100.0
                - 100.0 / (1.0 + rs)
            )

    return result


# ============================================================
# ATR
# ============================================================

def atr(
    candles: list[Candle],
    period: int = 14,
):
    result = np.full(
        len(candles),
        np.nan,
    )

    if len(candles) < period + 1:
        return result

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

    previous_close = np.roll(
        closes,
        1,
    )

    true_range = np.maximum(
        highs - lows,
        np.maximum(
            np.abs(
                highs - previous_close
            ),
            np.abs(
                lows - previous_close
            ),
        ),
    )

    true_range[0] = (
        highs[0] - lows[0]
    )

    return ema(
        true_range,
        period,
    )


# ============================================================
# AGGREGATE
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
        key=lambda x: x.time,
    )

    if timeframe <= 1:
        result = list(candles)

    else:
        seconds = timeframe * 60

        buckets: dict[
            int,
            list[Candle],
        ] = {}

        for candle in candles:

            timestamp = int(
                candle.time.timestamp()
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
                key=lambda x: x.time,
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

    # Только полностью закрытые свечи.
    now = datetime.now(
        timezone.utc
    )

    candle_seconds = (
        timeframe * 60
    )

    if result:

        last = result[-1]

        candle_close = (
            last.time
            + timedelta(
                seconds=candle_seconds
            )
        )

        if candle_close > now:
            result = result[:-1]

    return result


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

        timeframe = int(timeframe)

        if timeframe not in config.timeframes:
            return None

        candles_tf = aggregate_candles(
            candles,
            timeframe,
        )

        if len(candles_tf) < self.MIN_CANDLES:
            return None

        close = np.asarray(
            [c.close for c in candles_tf],
            dtype=float,
        )

        high = np.asarray(
            [c.high for c in candles_tf],
            dtype=float,
        )

        low = np.asarray(
            [c.low for c in candles_tf],
            dtype=float,
        )

        volume = np.asarray(
            [c.volume for c in candles_tf],
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

        # ====================================================
        # INDICATORS
        # ====================================================

        ema9 = ema(close, 9)
        ema21 = ema(close, 21)
        ema50 = ema(close, 50)

        rsi14 = rsi(close, 14)

        atr14 = atr(
            candles_tf,
            14,
        )

        if not np.isfinite(atr14[-1]):
            return None

        current_atr = float(
            atr14[-1]
        )

        if current_atr <= 0:
            return None

        # ====================================================
        # MACD
        # ====================================================

        ema12 = ema(close, 12)
        ema26 = ema(close, 26)

        macd = (
            ema12 - ema26
        )

        valid_macd = macd[
            np.isfinite(macd)
        ]

        if len(valid_macd) < 20:
            return None

        macd_signal_array = ema(
            valid_macd,
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

        # ====================================================
        # BOLLINGER
        # ====================================================

        if len(close) < 20:
            return None

        bb_window = close[-20:]

        bb_middle = float(
            np.mean(bb_window)
        )

        bb_std = float(
            np.std(bb_window)
        )

        bb_upper = (
            bb_middle
            + 2.0 * bb_std
        )

        bb_lower = (
            bb_middle
            - 2.0 * bb_std
        )

        # ====================================================
        # STOCHASTIC
        # ====================================================

        highest = float(
            np.max(high[-14:])
        )

        lowest = float(
            np.min(low[-14:])
        )

        price = float(
            close[-1]
        )

        if highest == lowest:
            stochastic = 50.0
        else:
            stochastic = (
                100.0
                * (price - lowest)
                / (highest - lowest)
            )

        # ====================================================
        # MOMENTUM
        # ====================================================

        lookback = min(
            12,
            len(close) - 1,
        )

        momentum = (
            price
            - float(
                close[-1 - lookback]
            )
        )

        short_lookback = min(
            3,
            len(close) - 1,
        )

        short_momentum = (
            price
            - float(
                close[-1 - short_lookback]
            )
        )

        # ====================================================
        # VOLUME
        # ====================================================

        volume_ratio = 1.0

        if len(volume) >= 20:

            avg_volume = float(
                np.mean(volume[-20:])
            )

            if avg_volume > 0:

                volume_ratio = (
                    float(volume[-1])
                    / avg_volume
                )

        # ====================================================
        # SUPPORT / RESISTANCE
        # ====================================================

        support = float(
            np.min(low[-30:])
        )

        resistance = float(
            np.max(high[-30:])
        )

        range_size = max(
            resistance - support,
            1e-12,
        )

        position = (
            price - support
        ) / range_size

        # ====================================================
        # CANDLE
        # ====================================================

        last_candle = candles_tf[-1]

        candle_range = max(
            last_candle.high
            - last_candle.low,
            1e-12,
        )

        candle_body = abs(
            last_candle.close
            - last_candle.open
        )

        body_ratio = (
            candle_body
            / candle_range
        )

        bullish_candle = (
            last_candle.close
            > last_candle.open
        )

        bearish_candle = (
            last_candle.close
            < last_candle.open
        )

        # ====================================================
        # SCORES
        # ====================================================

        up = 0.0
        down = 0.0

        up_reasons: list[str] = []
        down_reasons: list[str] = []

        # ====================================================
        # EMA
        # ====================================================

        if (
            np.isfinite(ema9[-1])
            and np.isfinite(ema21[-1])
            and np.isfinite(ema50[-1])
        ):

            if (
                ema9[-1]
                > ema21[-1]
                > ema50[-1]
            ):

                up += 20

                up_reasons.append(
                    "EMA 9/21/50 подтверждают восходящий тренд"
                )

            elif (
                ema9[-1]
                < ema21[-1]
                < ema50[-1]
            ):

                down += 20

                down_reasons.append(
                    "EMA 9/21/50 подтверждают нисходящий тренд"
                )

            elif ema9[-1] > ema21[-1]:

                up += 7

            elif ema9[-1] < ema21[-1]:

                down += 7

        # ====================================================
        # EMA SLOPE
        # ====================================================

        if (
            len(ema21) >= 4
            and np.isfinite(ema21[-1])
            and np.isfinite(ema21[-4])
        ):

            slope = (
                ema21[-1]
                - ema21[-4]
            )

            if slope > 0:

                up += 8

                up_reasons.append(
                    "EMA 21 имеет положительный наклон"
                )

            elif slope < 0:

                down += 8

                down_reasons.append(
                    "EMA 21 имеет отрицательный наклон"
                )

        # ====================================================
        # RSI
        # ====================================================

        current_rsi = float(
            rsi14[-1]
        )

        if np.isfinite(
            current_rsi
        ):

            if 52 <= current_rsi <= 68:

                up += 15

                up_reasons.append(
                    f"RSI {current_rsi:.1f} поддерживает UP"
                )

            elif 32 <= current_rsi <= 48:

                down += 15

                down_reasons.append(
                    f"RSI {current_rsi:.1f} поддерживает DOWN"
                )

            elif current_rsi < 30:

                up += 10

                up_reasons.append(
                    f"RSI {current_rsi:.1f} — перепроданность"
                )

            elif current_rsi > 70:

                down += 10

                down_reasons.append(
                    f"RSI {current_rsi:.1f} — перекупленность"
                )

        # ====================================================
        # MACD
        # ====================================================

        if macd_value > macd_signal:

            up += 15

            up_reasons.append(
                "MACD подтверждает UP"
            )

        elif macd_value < macd_signal:

            down += 15

            down_reasons.append(
                "MACD подтверждает DOWN"
            )

        # ====================================================
        # MACD HISTOGRAM
        # ====================================================

        histogram = (
            macd_value
            - macd_signal
        )

        previous_histogram = (
            float(macd[-2])
            - float(
                macd_signal_array[-2]
            )
        )

        if (
            histogram > 0
            and histogram > previous_histogram
        ):

            up += 5

            up_reasons.append(
                "MACD histogram усиливается вверх"
            )

        elif (
            histogram < 0
            and histogram < previous_histogram
        ):

            down += 5

            down_reasons.append(
                "MACD histogram усиливается вниз"
            )

        # ====================================================
        # BOLLINGER
        # ====================================================

        if price <= bb_lower:

            up += 10

            up_reasons.append(
                "Цена возле нижней Bollinger Band"
            )

        elif price >= bb_upper:

            down += 10

            down_reasons.append(
                "Цена возле верхней Bollinger Band"
            )

        elif price > bb_middle:

            up += 4

        elif price < bb_middle:

            down += 4

        # ====================================================
        # STOCHASTIC
        # ====================================================

        if stochastic <= 20:

            up += 10

            up_reasons.append(
                f"Stochastic {stochastic:.1f} — перепроданность"
            )

        elif stochastic >= 80:

            down += 10

            down_reasons.append(
                f"Stochastic {stochastic:.1f} — перекупленность"
            )

        elif stochastic > 55:

            up += 4

        elif stochastic < 45:

            down += 4

        # ====================================================
        # MOMENTUM
        # ====================================================

        if momentum > 0:

            up += 10

            up_reasons.append(
                "Положительный momentum"
            )

        elif momentum < 0:

            down += 10

            down_reasons.append(
                "Отрицательный momentum"
            )

        # ====================================================
        # SHORT MOMENTUM
        # ====================================================

        if short_momentum > 0:

            up += 5

        elif short_momentum < 0:

            down += 5

        # ====================================================
        # SUPPORT / RESISTANCE
        # ====================================================

        if position <= 0.18:

            up += 10

            up_reasons.append(
                "Цена возле поддержки"
            )

        elif position >= 0.82:

            down += 10

            down_reasons.append(
                "Цена возле сопротивления"
            )

        # ====================================================
        # CANDLE CONFIRMATION
        # ====================================================

        if body_ratio >= 0.55:

            if bullish_candle:

                up += 5

                up_reasons.append(
                    "Сильная бычья свеча"
                )

            elif bearish_candle:

                down += 5

                down_reasons.append(
                    "Сильная медвежья свеча"
                )

        # ====================================================
        # VOLUME
        # ====================================================

        if volume_ratio >= 1.10:

            if up > down:

                up += 5

                up_reasons.append(
                    "Объём выше среднего"
                )

            elif down > up:

                down += 5

                down_reasons.append(
                    "Объём выше среднего"
                )

        # ====================================================
        # VOLATILITY
        # ====================================================

        atr_percent = (
            current_atr
            / price
            * 100.0
        )

        if atr_percent < 0.01:
            return None

        if atr_percent > 5.0:
            return None

        # ====================================================
        # DIRECTION
        # ====================================================

        if up > down:

            direction = "UP"
            score = up
            opposite = down
            reasons = up_reasons

        elif down > up:

            direction = "DOWN"
            score = down
            opposite = up
            reasons = down_reasons

        else:

            return None

        # ====================================================
        # GAP
        # ====================================================

        gap = (
            score
            - opposite
        )

        if gap < 15:
            return None

        # ====================================================
        # QUALITY
        # ====================================================

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
            quality -= 5.0

        if volume_ratio >= 1.20:
            quality += 3.0

        quality = min(
            100.0,
            max(
                0.0,
                quality,
            ),
        )

        # ====================================================
        # TECHNICAL CONFIDENCE
        # ====================================================
        #
        # ВАЖНО:
        # НИКОГДА не называем это WINRATE.
        #
        # WINRATE будет считаться database.py
        # только по реальным WIN/LOSS.
        #
        # Эта цифра показывает только силу текущего
        # технического набора условий.

        confirmation_count = len(
            reasons
        )

        confirmation_bonus = min(
            5.0,
            confirmation_count * 0.5,
        )

        probability = (
            50.0
            + quality * 0.38
            + min(
                gap,
                30.0,
            ) * 0.18
            + confirmation_bonus
        )

        probability = min(
            92.0,
            max(
                50.0,
                probability,
            ),
        )

        # ====================================================
        # CONFIG FILTER
        # ====================================================

        min_quality = float(
            getattr(
                config,
                "MIN_SIGNAL_SCORE",
                75.0,
            )
        )

        min_probability = float(
            getattr(
                config,
                "MIN_PROBABILITY",
                75.0,
            )
        )

        if quality < min_quality:
            return None

        if probability < min_probability:
            return None

        # ====================================================
        # ENTRY / CLOSE
        # ====================================================
        #
        # Сигнал считается входом ПО ЗАЯВКЕ:
        # момент создания сигнала = момент входа.
        #
        # Никакого искусственного ожидания следующей
        # границы таймфрейма.

        entry_time = datetime.now(
            timezone.utc
        )

        close_time = (
            entry_time
            + timedelta(
                minutes=timeframe
            )
        )

        # ====================================================
        # REASONS
        # ====================================================

        reasons = list(
            dict.fromkeys(
                reasons
            )
        )

        if gap >= 30:

            reasons.append(
                f"Преимущество направления: {gap:.1f} балла"
            )

        if atr_percent >= 0.03:

            reasons.append(
                "ATR подтверждает достаточную "
                f"волатильность ({atr_percent:.2f}%)"
            )

        if volume_ratio >= 1.10:

            reasons.append(
                f"Объём: {volume_ratio:.2f}x от среднего"
            )

        return SignalResult(
            pair=pair,
            timeframe=timeframe,
            direction=direction,

            probability=round(
                probability,
                2,
            ),

            quality=round(
                quality,
                2,
            ),

            entry_time=entry_time,
            close_time=close_time,

            reasons=reasons[:8],
        )
