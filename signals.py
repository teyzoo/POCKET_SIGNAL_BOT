from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt
from market import Candle
# ============================================================
# SIGNAL RESULT
# ============================================================
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
# ============================================================
# BASIC HELPERS
# ============================================================
def closes(candles: list[Candle]) -> list[float]:
    return [x.close for x in candles]
def highs(candles: list[Candle]) -> list[float]:
    return [x.high for x in candles]
def lows(candles: list[Candle]) -> list[float]:
    return [x.low for x in candles]
# ============================================================
# EMA
# ============================================================
def ema(
    values: list[float],
    period: int,
) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    value = sum(
        values[:period]
    ) / period
    for price in values[period:]:
        value = (
            (price - value)
            * multiplier
            + value
        )
    return value
# ============================================================
# SMA
# ============================================================
def sma(
    values: list[float],
    period: int,
) -> float | None:
    if len(values) < period:
        return None
    return sum(
        values[-period:]
    ) / period
# ============================================================
# RSI
# ============================================================
def rsi(
    values: list[float],
    period: int = 14,
) -> float | None:
    if len(values) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        diff = (
            values[i]
            - values[i - 1]
        )
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    if len(gains) < period:
        return None
    avg_gain = (
        sum(gains[:period])
        / period
    )
    avg_loss = (
        sum(losses[:period])
        / period
    )
    for i in range(
        period,
        len(gains),
    ):
        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period
        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (
        100.0
        / (1.0 + rs)
    )
# ============================================================
# MACD
# ============================================================
def macd(
    values: list[float],
) -> float | None:
    fast = ema(
        values,
        12,
    )
    slow = ema(
        values,
        26,
    )
    if fast is None or slow is None:
        return None
    return fast - slow
# ============================================================
# BOLLINGER BANDS
# ============================================================
def bollinger(
    values: list[float],
    period: int = 20,
):
    if len(values) < period:
        return None
    window = values[-period:]
    middle = (
        sum(window)
        / period
    )
    variance = (
        sum(
            (x - middle) ** 2
            for x in window
        )
        / period
    )
    deviation = sqrt(
        variance
    )
    return (
        middle,
        middle + 2 * deviation,
        middle - 2 * deviation,
    )
# ============================================================
# ATR
# ============================================================
def atr(
    candles: list[Candle],
    period: int = 14,
) -> float | None:
    if len(candles) <= period:
        return None
    trs: list[float] = []
    for i in range(
        1,
        len(candles),
    ):
        current = candles[i]
        previous = candles[i - 1]
        tr = max(
            current.high
            - current.low,
            abs(
                current.high
                - previous.close
            ),
            abs(
                current.low
                - previous.close
            ),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return (
        sum(trs[-period:])
        / period
    )
# ============================================================
# STOCHASTIC
# ============================================================
def stochastic(
    candles: list[Candle],
    period: int = 14,
) -> float | None:
    if len(candles) < period:
        return None
    window = candles[-period:]
    highest = max(
        x.high
        for x in window
    )
    lowest = min(
        x.low
        for x in window
    )
    if highest == lowest:
        return 50.0
    return (
        (
            window[-1].close
            - lowest
        )
        / (highest - lowest)
    ) * 100.0
# ============================================================
# SUPPORT / RESISTANCE
# ============================================================
def support_resistance(
    candles: list[Candle],
    period: int = 30,
):
    if len(candles) < period:
        return (
            min(x.low for x in candles),
            max(x.high for x in candles),
        )
    window = candles[-period:]
    return (
        min(
            x.low
            for x in window
        ),
        max(
            x.high
            for x in window
        ),
    )
# ============================================================
# CANDLE PATTERNS
# ============================================================
def candle_pattern(
    candles: list[Candle],
):
    if len(candles) < 3:
        return None
    a = candles[-3]
    b = candles[-2]
    c = candles[-1]
    body = abs(
        c.close
        - c.open
    )
    # Avoid treating tiny doji candles as
    # strong rejection candles.
    minimum_body = max(
        body,
        (c.high - c.low) * 0.05,
    )
    upper = (
        c.high
        - max(
            c.open,
            c.close,
        )
    )
    lower = (
        min(
            c.open,
            c.close,
        )
        - c.low
    )
    if (
        lower > minimum_body * 2
        and upper < minimum_body
    ):
        return "bullish_rejection"
    if (
        upper > minimum_body * 2
        and lower < minimum_body
    ):
        return "bearish_rejection"
    # Bullish engulfing
    if (
        a.close < a.open
        and b.close > b.open
        and c.close > c.open
        and c.close > a.open
        and c.open <= b.close
    ):
        return "bullish_engulfing"
    # Bearish engulfing
    if (
        a.close > a.open
        and b.close < b.open
        and c.close < c.open
        and c.close < a.open
        and c.open >= b.close
    ):
        return "bearish_engulfing"
    return None
# ============================================================
# MOMENTUM
# ============================================================
def momentum(
    values: list[float],
    period: int = 5,
) -> float | None:
    if len(values) <= period:
        return None
    previous = values[-period - 1]
    if previous == 0:
        return None
    return (
        (
            values[-1]
            - previous
        )
        / previous
    ) * 100.0
# ============================================================
# SIGNAL ENGINE
# ============================================================
class SignalEngine:
    def __init__(
        self,
        min_score: float = 75.0,
        min_probability: float = 75.0,
    ):
        self.min_score = min_score
        self.min_probability = (
            min_probability
        )
    # ========================================================
    # ANALYZE
    # ========================================================
    def analyze(
        self,
        pair: str,
        timeframe: int,
        candles: list[Candle],
    ) -> SignalResult | None:
        # OTC signals must have enough history.
        if len(candles) < 60:
            return None
        values = closes(candles)
        if len(values) < 60:
            return None
        price = values[-1]
        # ----------------------------------------------------
        # Indicators
        # ----------------------------------------------------
        fast = ema(
            values,
            9,
        )
        slow = ema(
            values,
            21,
        )
        rsi_value = rsi(
            values,
            14,
        )
        macd_value = macd(
            values,
        )
        bb = bollinger(
            values,
            20,
        )
        atr_value = atr(
            candles,
            14,
        )
        stoch = stochastic(
            candles,
            14,
        )
        support, resistance = (
            support_resistance(
                candles,
                30,
            )
        )
        pattern = candle_pattern(
            candles,
        )
        momentum_value = momentum(
            values,
            5,
        )
        if None in (
            fast,
            slow,
            rsi_value,
            macd_value,
            bb,
            atr_value,
            stoch,
            momentum_value,
        ):
            return None
        middle, upper, lower = bb
        # ====================================================
        # SCORING
        # ====================================================
        bullish_score = 0.0
        bearish_score = 0.0
        bullish_reasons: list[str] = []
        bearish_reasons: list[str] = []
        # ----------------------------------------------------
        # EMA TREND
        # ----------------------------------------------------
        if fast > slow:
            bullish_score += 2.0
            bullish_reasons.append(
                "EMA trend UP"
            )
        elif fast < slow:
            bearish_score += 2.0
            bearish_reasons.append(
                "EMA trend DOWN"
            )
        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------
        if rsi_value < 30:
            bullish_score += 2.5
            bullish_reasons.append(
                "RSI strongly oversold"
            )
        elif rsi_value < 40:
            bullish_score += 1.5
            bullish_reasons.append(
                "RSI oversold"
            )
        elif rsi_value > 70:
            bearish_score += 2.5
            bearish_reasons.append(
                "RSI strongly overbought"
            )
        elif rsi_value > 60:
            bearish_score += 1.5
            bearish_reasons.append(
                "RSI overbought"
            )
        elif rsi_value > 50:
            bullish_score += 0.75
            bullish_reasons.append(
                "RSI above 50"
            )
        else:
            bearish_score += 0.75
            bearish_reasons.append(
                "RSI below 50"
            )
        # ----------------------------------------------------
        # MACD
        # ----------------------------------------------------
        if macd_value > 0:
            bullish_score += 1.5
            bullish_reasons.append(
                "MACD positive"
            )
        elif macd_value < 0:
            bearish_score += 1.5
            bearish_reasons.append(
                "MACD negative"
            )
        # ----------------------------------------------------
        # BOLLINGER
        # ----------------------------------------------------
        if price <= lower:
            bullish_score += 2.0
            bullish_reasons.append(
                "Price near lower Bollinger"
            )
        elif price >= upper:
            bearish_score += 2.0
            bearish_reasons.append(
                "Price near upper Bollinger"
            )
        elif price < middle:
            bearish_score += 0.5
        elif price > middle:
            bullish_score += 0.5
        # ----------------------------------------------------
        # STOCHASTIC
        # ----------------------------------------------------
        if stoch < 20:
            bullish_score += 2.0
            bullish_reasons.append(
                "Stochastic oversold"
            )
        elif stoch > 80:
            bearish_score += 2.0
            bearish_reasons.append(
                "Stochastic overbought"
            )
        elif stoch > 50:
            bullish_score += 0.5
        else:
            bearish_score += 0.5
        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------
        if momentum_value > 0:
            bullish_score += 1.0
            bullish_reasons.append(
                "Positive momentum"
            )
        elif momentum_value < 0:
            bearish_score += 1.0
            bearish_reasons.append(
                "Negative momentum"
            )
        # ----------------------------------------------------
        # CANDLE PATTERN
        # ----------------------------------------------------
        if pattern == "bullish_rejection":
            bullish_score += 2.5
            bullish_reasons.append(
                "Bullish rejection"
            )
        elif pattern == "bearish_rejection":
            bearish_score += 2.5
            bearish_reasons.append(
                "Bearish rejection"
            )
        elif pattern == "bullish_engulfing":
            bullish_score += 3.0
            bullish_reasons.append(
                "Bullish engulfing"
            )
        elif pattern == "bearish_engulfing":
            bearish_score += 3.0
            bearish_reasons.append(
                "Bearish engulfing"
            )
        # ----------------------------------------------------
        # SUPPORT / RESISTANCE
        # ----------------------------------------------------
        support_distance = abs(
            price - support
        )
        resistance_distance = abs(
            resistance - price
        )
        if (
            atr_value > 0
            and support_distance
            <= atr_value * 0.8
        ):
            bullish_score += 2.0
            bullish_reasons.append(
                "Near support"
            )
        if (
            atr_value > 0
            and resistance_distance
            <= atr_value * 0.8
        ):
            bearish_score += 2.0
            bearish_reasons.append(
                "Near resistance"
            )
        # ====================================================
        # FINAL DIRECTION
        # ====================================================
        total = (
            bullish_score
            + bearish_score
        )
        if total <= 0:
            return None
        if bullish_score > bearish_score:
            direction = "UP"
            strength = bullish_score
            opposite = bearish_score
            reasons = bullish_reasons
        elif bearish_score > bullish_score:
            direction = "DOWN"
            strength = bearish_score
            opposite = bullish_score
            reasons = bearish_reasons
        else:
            return None
        # ----------------------------------------------------
        # Direction must have a meaningful advantage.
        # ----------------------------------------------------
        edge = (
            strength - opposite
        )
        if edge < 2.0:
            return None
        balance = (
            strength / total
        )
        # ====================================================
        # QUALITY
        # ====================================================
        quality = (
            50.0
            + balance * 50.0
        )
        # Bonus for multiple independent confirmations.
        confirmation_count = len(
            reasons
        )
        quality += min(
            8.0,
            confirmation_count * 1.0,
        )
        # Penalize weak/noisy setups.
        if edge < 3:
            quality -= 5
        quality = max(
            0.0,
            min(
                99.0,
                quality,
            ),
        )
        # ====================================================
        # PROBABILITY
        # ====================================================
        probability = (
            50.0
            + balance * 48.0
        )
        probability += min(
            5.0,
            confirmation_count * 0.7,
        )
        if edge < 3:
            probability -= 4
        probability = max(
            0.0,
            min(
                98.0,
                probability,
            ),
        )
        # ====================================================
        # FILTER
        # ====================================================
        if quality < self.min_score:
            return None
        if probability < self.min_probability:
            return None
        # ====================================================
        # TIME
        # ====================================================
        now = datetime.now(
            timezone.utc
        )
        close_time = (
            now
            + timedelta(
                minutes=timeframe
            )
        )
        # ====================================================
        # RETURN
        # ====================================================
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
            reasons=reasons[:8],
            entry_time=now,
            close_time=close_time,
        )
# ============================================================
# GLOBAL ENGINE
# ============================================================
engine = SignalEngine()
