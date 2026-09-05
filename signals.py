from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from config import config
from market import Candle


# ============================================================
# RESULT
# ============================================================

@dataclass(slots=True)
class SignalResult:
    pair: str
    timeframe: int
    direction: str

    # Это техническая confidence-модель,
    # а НЕ гарантированный исторический winrate.
    probability: float

    quality: float

    entry_time: datetime
    close_time: datetime

    reasons: list[str]


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period: int):
    values = np.asarray(values, dtype=float)

    result = np.full(
        len(values),
        np.nan,
        dtype=float,
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


def rsi(values, period: int = 14):
    values = np.asarray(values, dtype=float)

    if len(values) < period + 1:
        return np.full(
            len(values),
            np.nan,
        )

    delta = np.diff(
        values,
        prepend=values[0],
    )

    gain = np.maximum(delta, 0.0)
    loss = np.maximum(-delta, 0.0)

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

    for i in range(period + 1, len(values)):
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

    result = np.full(
        len(values),
        np.nan,
    )

    for i in range(period, len(values)):
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
# TIMEFRAME AGGREGATION
# ============================================================

def aggregate_candles(
    candles: list[Candle],
    timeframe: int,
) -> list[Candle]:

    timeframe = int(timeframe)

    if timeframe <= 1:
        return list(candles)

    if not candles:
        return []

    buckets: dict[int, list[Candle]] = {}

    seconds = timeframe * 60

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

    result: list[Candle] = []

    for bucket in sorted(buckets):

        group = buckets[bucket]

        if not group:
            continue

        group = sorted(
            group,
            key=lambda x: x.time,
        )

        result.append(
            Candle(
                time=datetime.fromtimestamp(
                    bucket,
                    tz=timezone.utc,
                ),
                open=group[0].open,
                high=max(
                    x.high for x in group
                ),
                low=min(
                    x.low for x in group
                ),
                close=group[-1].close,
                volume=sum(
                    x.volume
                    for x in group
                ),
            )
        )

    return result


# ============================================================
# ENGINE
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

        # ----------------------------------------------------
        # AGGREGATE 1M → SELECTED TIMEFRAME
        # ----------------------------------------------------

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

        if not np.all(
            np.isfinite(close)
        ):
            return None

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        rsi14 = rsi(
            close,
            14,
        )

        # ----------------------------------------------------
        # ATR
        # ----------------------------------------------------

        atr14 = atr(
            candles_tf,
            14,
        )

        if not np.isfinite(
            atr14[-1]
        ):
            return None

        current_atr = float(
            atr14[-1]
        )

        if current_atr <= 0:
            return None

        # ----------------------------------------------------
        # MACD
        # ----------------------------------------------------

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

        valid_macd = macd[
            np.isfinite(macd)
        ]

        if len(valid_macd) < 20:
            return None

        macd_signal_array = ema(
            valid_macd,
            9,
        )

        macd_value = float(
            macd[-1]
        )

        macd_signal = float(
            macd_signal_array[-1]
        )

        # ----------------------------------------------------
        # BOLLINGER
        # ----------------------------------------------------

        if len(close) < 20:
            return None

        bb = close[-20:]

        bb_middle = float(
            np.mean(bb)
        )

        bb_std = float(
            np.std(bb)
        )

        bb_upper = (
            bb_middle
            + 2.0 * bb_std
        )

        bb_lower = (
            bb_middle
            - 2.0 * bb_std
        )

        # ----------------------------------------------------
        # STOCHASTIC
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        volume_confirmation = False

        if len(volume) >= 20:

            average_volume = float(
                np.mean(
                    volume[-20:]
                )
            )

            if (
                average_volume > 0
                and volume[-1]
                >= average_volume * 1.10
            ):
                volume_confirmation = True

        # ----------------------------------------------------
        # SUPPORT / RESISTANCE
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # SCORES
        # ----------------------------------------------------

        up_score = 0.0
        down_score = 0.0

        up_reasons: list[str] = []
        down_reasons: list[str] = []

        # ----------------------------------------------------
        # EMA TREND — 20
        # ----------------------------------------------------

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

                up_score += 20

                up_reasons.append(
                    "EMA 9/21/50 подтверждают восходящий тренд"
                )

            elif (
                ema9[-1]
                < ema21[-1]
                < ema50[-1]
            ):

                down_score += 20

                down_reasons.append(
                    "EMA 9/21/50 подтверждают нисходящий тренд"
                )

        # ----------------------------------------------------
        # RSI — 15
        # ----------------------------------------------------

        current_rsi = float(
            rsi14[-1]
        )

        if np.isfinite(current_rsi):

            if current_rsi <= 30:

                up_score += 15

                up_reasons.append(
                    f"RSI {current_rsi:.1f} — сильная перепроданность"
                )

            elif current_rsi >= 70:

                down_score += 15

                down_reasons.append(
                    f"RSI {current_rsi:.1f} — сильная перекупленность"
                )

            elif 45 <= current_rsi <= 55:

                # Нейтральный RSI не подтверждает направление.
                pass

            elif current_rsi > 55:

                up_score += 5

                up_reasons.append(
                    f"RSI {current_rsi:.1f} поддерживает UP"
                )

            elif current_rsi < 45:

                down_score += 5

                down_reasons.append(
                    f"RSI {current_rsi:.1f} поддерживает DOWN"
                )

        # ----------------------------------------------------
        # MACD — 15
        # ----------------------------------------------------

        if macd_value > macd_signal:

            up_score += 15

            up_reasons.append(
                "MACD подтверждает UP"
            )

        elif macd_value < macd_signal:

            down_score += 15

            down_reasons.append(
                "MACD подтверждает DOWN"
            )

        # ----------------------------------------------------
        # BOLLINGER — 10
        # ----------------------------------------------------

        if price <= bb_lower:

            up_score += 10

            up_reasons.append(
                "Цена возле нижней Bollinger Band"
            )

        elif price >= bb_upper:

            down_score += 10

            down_reasons.append(
                "Цена возле верхней Bollinger Band"
            )

        # ----------------------------------------------------
        # STOCHASTIC — 10
        # ----------------------------------------------------

        if stochastic <= 20:

            up_score += 10

            up_reasons.append(
                f"Stochastic {stochastic:.1f} — перепроданность"
            )

        elif stochastic >= 80:

            down_score += 10

            down_reasons.append(
                f"Stochastic {stochastic:.1f} — перекупленность"
            )

        # ----------------------------------------------------
        # MOMENTUM — 10
        # ----------------------------------------------------

        if momentum > 0:

            up_score += 10

            up_reasons.append(
                "Положительный momentum"
            )

        elif momentum < 0:

            down_score += 10

            down_reasons.append(
                "Отрицательный momentum"
            )

        # ----------------------------------------------------
        # SUPPORT / RESISTANCE — 10
        # ----------------------------------------------------

        if position <= 0.18:

            up_score += 10

            up_reasons.append(
                "Цена находится возле поддержки"
            )

        elif position >= 0.82:

            down_score += 10

            down_reasons.append(
                "Цена находится возле сопротивления"
            )

        # ----------------------------------------------------
        # VOLUME — BONUS 5
        # ----------------------------------------------------

        if volume_confirmation:

            if up_score > down_score:

                up_score += 5

                up_reasons.append(
                    "Объём выше среднего подтверждает движение"
                )

            elif down_score > up_score:

                down_score += 5

                down_reasons.append(
                    "Объём выше среднего подтверждает движение"
                )

        # ----------------------------------------------------
        # VOLATILITY PROTECTION
        # ----------------------------------------------------

        recent_range = (
            float(
                np.max(high[-20:])
            )
            - float(
                np.min(low[-20:])
            )
        )

        if recent_range <= 0:
            return None

        # Если рынок практически стоит,
        # сигнал не выдаём.
        if current_atr / price < 0.00001:
            return None

        # ----------------------------------------------------
        # FINAL SCORE
        # ----------------------------------------------------

        up_score = min(
            100.0,
            up_score,
        )

        down_score = min(
            100.0,
            down_score,
        )

        quality = max(
            up_score,
            down_score,
        )

        # ----------------------------------------------------
        # MIN QUALITY
        # ----------------------------------------------------

        if quality < config.min_signal_score:
            return None

        direction = (
            "UP"
            if up_score > down_score
            else "DOWN"
        )

        # Если оценки равны — нет преимущества.
        if up_score == down_score:
            return None

        winning_score = (
            up_score
            if direction == "UP"
            else down_score
        )

        losing_score = (
            down_score
            if direction == "UP"
            else up_score
        )

        edge = (
            winning_score
            - losing_score
        )

        # ----------------------------------------------------
        # CONFIDENCE
        # ----------------------------------------------------

        # Это не заявленный исторический winrate.
        # Это техническая оценка силы текущего сетапа.
        probability = (
            50.0
            + quality * 0.35
            + min(edge, 30.0) * 0.25
        )

        probability = max(
            50.0,
            min(
                97.0,
                probability,
            ),
        )

        if probability < config.min_probability:
            return None

        # ----------------------------------------------------
        # TIME
        # ----------------------------------------------------

        now = datetime.now(
            timezone.utc
        ).replace(
            second=0,
            microsecond=0,
        )

        close_time = (
            now
            + timedelta(
                minutes=timeframe
            )
        )

        reasons = (
            up_reasons
            if direction == "UP"
            else down_reasons
        )

        return SignalResult(
            pair=pair,
            timeframe=timeframe,
            direction=direction,
            probability=round(
                probability,
                1,
            ),
            quality=round(
                quality,
                1,
            ),
            entry_time=now,
            close_time=close_time,
            reasons=reasons[:8],
        )


# ============================================================
# GLOBAL ENGINE
# ============================================================

engine = SignalEngine()
