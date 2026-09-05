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

    probability: float

    quality: float

    entry_time: datetime

    close_time: datetime

    reasons: list[str]


def ema(
    values,
    period: int,
):

    values = np.asarray(
        values,
        dtype=float,
    )

    if len(values) < period:

        return np.full(
            len(values),
            np.nan,
        )

    alpha = 2 / (
        period + 1
    )

    result = np.full(
        len(values),
        np.nan,
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

    delta = np.diff(
        values,
        prepend=values[0],
    )

    gain = np.maximum(
        delta,
        0,
    )

    loss = np.maximum(
        -delta,
        0,
    )

    avg_gain = ema(
        gain,
        period,
    )

    avg_loss = ema(
        loss,
        period,
    )

    rs = avg_gain / np.where(
        avg_loss == 0,
        1e-12,
        avg_loss,
    )

    return 100 - (
        100 / (1 + rs)
    )


def atr(
    candles: list[Candle],
    period: int = 14,
):

    highs = np.array(
        [x.high for x in candles],
        dtype=float,
    )

    lows = np.array(
        [x.low for x in candles],
        dtype=float,
    )

    closes = np.array(
        [x.close for x in candles],
        dtype=float,
    )

    previous = np.roll(
        closes,
        1,
    )

    true_range = np.maximum(
        highs - lows,
        np.maximum(
            abs(highs - previous),
            abs(lows - previous),
        ),
    )

    true_range[0] = (
        highs[0] - lows[0]
    )

    return ema(
        true_range,
        period,
    )


class SignalEngine:

    def analyze(
        self,
        pair: str,
        timeframe: int,
        candles: list[Candle],
    ) -> SignalResult | None:

        if len(candles) < 60:

            return None

        close = np.array(
            [x.close for x in candles],
            dtype=float,
        )

        high = np.array(
            [x.high for x in candles],
            dtype=float,
        )

        low = np.array(
            [x.low for x in candles],
            dtype=float,
        )

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
            candles,
            14,
        )

        deltas = (
            close
            - np.roll(
                close,
                12,
            )
        )

        momentum = deltas[-1]

        price = close[-1]

        # MACD
        ema12 = ema(
            close,
            12,
        )

        ema26 = ema(
            close,
            26,
        )

        macd = ema12 - ema26

        valid_macd = macd[
            ~np.isnan(macd)
        ]

        macd_signal_array = ema(
            valid_macd,
            9,
        )

        macd_value = macd[-1]

        macd_signal = (
            macd_signal_array[-1]
            if len(
                macd_signal_array
            )
            else 0
        )

        # Bollinger Bands
        bb_window = close[-20:]

        bb_middle = (
            bb_window.mean()
        )

        bb_std = (
            bb_window.std()
        )

        bb_upper = (
            bb_middle
            + 2 * bb_std
        )

        bb_lower = (
            bb_middle
            - 2 * bb_std
        )

        # Stochastic
        highest = high[-14:].max()

        lowest = low[-14:].min()

        if highest == lowest:

            stochastic = 50

        else:

            stochastic = (
                100
                * (
                    price - lowest
                )
                / (
                    highest - lowest
                )
            )

        up_score = 0.0
        down_score = 0.0

        up_reasons: list[str] = []
        down_reasons: list[str] = []

        # EMA trend
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

        # RSI
        if rsi14[-1] < 35:

            up_score += 14

            up_reasons.append(
                f"RSI {rsi14[-1]:.1f} — зона перепроданности"
            )

        elif rsi14[-1] > 65:

            down_score += 14

            down_reasons.append(
                f"RSI {rsi14[-1]:.1f} — зона перекупленности"
            )

        # MACD
        if macd_value > macd_signal:

            up_score += 14

            up_reasons.append(
                "MACD подтверждает движение UP"
            )

        elif macd_value < macd_signal:

            down_score += 14

            down_reasons.append(
                "MACD подтверждает движение DOWN"
            )

        # Bollinger
        if price <= bb_lower:

            up_score += 14

            up_reasons.append(
                "Цена возле нижней границы Bollinger"
            )

        elif price >= bb_upper:

            down_score += 14

            down_reasons.append(
                "Цена возле верхней границы Bollinger"
            )

        # Stochastic
        if stochastic < 25:

            up_score += 12

            up_reasons.append(
                f"Stochastic {stochastic:.1f} — перепроданность"
            )

        elif stochastic > 75:

            down_score += 12

            down_reasons.append(
                f"Stochastic {stochastic:.1f} — перекупленность"
            )

        # Momentum
        if momentum > 0:

            up_score += 8

            up_reasons.append(
                "Положительный ценовой импульс"
            )

        elif momentum < 0:

            down_score += 8

            down_reasons.append(
                "Отрицательный ценовой импульс"
            )

        # Support / resistance
        support = low[-30:].min()

        resistance = high[-30:].max()

        distance = max(
            resistance - support,
            1e-12,
        )

        position = (
            price - support
        ) / distance

        if position < 0.18:

            up_score += 10

            up_reasons.append(
                "Цена находится возле поддержки"
            )

        elif position > 0.82:

            down_score += 10

            down_reasons.append(
                "Цена находится возле сопротивления"
            )

        # ATR protection
        if (
            not np.isfinite(
                atr14[-1]
            )
            or atr14[-1] <= 0
        ):

            return None

        up_score = min(
            up_score,
            100,
        )

        down_score = min(
            down_score,
            100,
        )

        quality = max(
            up_score,
            down_score,
        )

        if (
            quality
            < config.min_signal_score
        ):

            return None

        direction = (
            "UP"
            if up_score >= down_score
            else "DOWN"
        )

        probability = min(
            97.0,
            50.0
            + quality * 0.50,
        )

        if (
            probability
            < config.min_probability
        ):

            return None

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
            probability=probability,
            quality=quality,
            entry_time=now,
            close_time=close_time,
            reasons=reasons[:8],
        )


engine = SignalEngine()
