from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt

from market import Candle


@dataclass
class SignalResult:
    pair: str
    timeframe: int
    direction: str
    probability: float
    quality: float
    reasons: list[str]
    entry_time: datetime
    close_time: datetime


def closes(candles):
    return [x.close for x in candles]


def highs(candles):
    return [x.high for x in candles]


def lows(candles):
    return [x.low for x in candles]


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (
            price - value
        ) * multiplier + value

    return value


def sma(values, period):
    if len(values) < period:
        return None

    return sum(values[-period:]) / period


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]

        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None

    return fast - slow


def bollinger(values, period=20):
    if len(values) < period:
        return None

    window = values[-period:]

    middle = sum(window) / period

    variance = sum(
        (x - middle) ** 2
        for x in window
    ) / period

    deviation = sqrt(variance)

    return (
        middle,
        middle + 2 * deviation,
        middle - 2 * deviation,
    )


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    trs = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


def stochastic(candles, period=14):
    if len(candles) < period:
        return None

    window = candles[-period:]

    highest = max(x.high for x in window)
    lowest = min(x.low for x in window)

    if highest == lowest:
        return 50

    return (
        (window[-1].close - lowest)
        / (highest - lowest)
    ) * 100


def support_resistance(candles, period=30):
    window = candles[-period:]

    return (
        min(x.low for x in window),
        max(x.high for x in window),
    )


def candle_pattern(candles):
    if len(candles) < 3:
        return None

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    body = abs(c.close - c.open)

    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low

    if lower > body * 2 and upper < body:
        return "bullish_rejection"

    if upper > body * 2 and lower < body:
        return "bearish_rejection"

    if (
        a.close < a.open
        and b.close > b.open
        and c.close > c.open
        and c.close > a.open
    ):
        return "bullish_engulfing"

    if (
        a.close > a.open
        and b.close < b.open
        and c.close < c.open
        and c.close < a.open
    ):
        return "bearish_engulfing"

    return None


class SignalEngine:
    def __init__(
        self,
        min_score: float = 75,
        min_probability: float = 75,
    ):
        self.min_score = min_score
        self.min_probability = min_probability

    def analyze(
        self,
        pair: str,
        timeframe: int,
        candles: list[Candle],
    ) -> SignalResult | None:

        if len(candles) < 60:
            return None

        values = closes(candles)

        price = values[-1]

        fast = ema(values, 9)
        slow = ema(values, 21)

        rsi_value = rsi(values)
        macd_value = macd(values)

        bb = bollinger(values)
        atr_value = atr(candles)
        stoch = stochastic(candles)

        support, resistance = support_resistance(
            candles
        )

        pattern = candle_pattern(candles)

        if None in (
            fast,
            slow,
            rsi_value,
            macd_value,
            bb,
            atr_value,
            stoch,
        ):
            return None

        middle, upper, lower = bb

        bullish = 0
        bearish = 0
        reasons: list[str] = []

        if fast > slow:
            bullish += 2
            reasons.append("EMA trend UP")
        elif fast < slow:
            bearish += 2
            reasons.append("EMA trend DOWN")

        if rsi_value < 35:
            bullish += 2
            reasons.append("RSI oversold")
        elif rsi_value > 65:
            bearish += 2
            reasons.append("RSI overbought")
        elif rsi_value > 50:
            bullish += 1
        else:
            bearish += 1

        if macd_value > 0:
            bullish += 1
            reasons.append("MACD positive")
        else:
            bearish += 1
            reasons.append("MACD negative")

        if price <= lower:
            bullish += 2
            reasons.append("Price near lower Bollinger")
        elif price >= upper:
            bearish += 2
            reasons.append("Price near upper Bollinger")

        if stoch < 20:
            bullish += 2
            reasons.append("Stochastic oversold")
        elif stoch > 80:
            bearish += 2
            reasons.append("Stochastic overbought")

        if pattern == "bullish_rejection":
            bullish += 2
            reasons.append("Bullish rejection")

        elif pattern == "bearish_rejection":
            bearish += 2
            reasons.append("Bearish rejection")

        elif pattern == "bullish_engulfing":
            bullish += 2
            reasons.append("Bullish engulfing")

        elif pattern == "bearish_engulfing":
            bearish += 2
            reasons.append("Bearish engulfing")

        distance_support = abs(price - support)
        distance_resistance = abs(resistance - price)

        if distance_support < atr_value * 0.8:
            bullish += 1
            reasons.append("Near support")

        if distance_resistance < atr_value * 0.8:
            bearish += 1
            reasons.append("Near resistance")

        total = bullish + bearish

        if total == 0:
            return None

        if bullish > bearish:
            direction = "UP"
            strength = bullish
            opposite = bearish
        else:
            direction = "DOWN"
            strength = bearish
            opposite = bullish

        balance = strength / total

        quality = min(
            99.0,
            50 + balance * 50,
        )

        probability = min(
            98.0,
            50 + balance * 48,
        )

        if quality < self.min_score:
            return None

        if probability < self.min_probability:
            return None

        now = datetime.now(timezone.utc)

        close = now + timedelta(
            minutes=timeframe
        )

        return SignalResult(
            pair=pair,
            timeframe=timeframe,
            direction=direction,
            probability=round(probability, 1),
            quality=round(quality, 1),
            reasons=reasons[:8],
            entry_time=now,
            close_time=close,
        )


engine = SignalEngine()
