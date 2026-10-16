"""Fibonacci grid helpers and impulse (N candles one direction) detection.

The grid is always stretched between two prices:

    0%   -> swing low   (the running low of the impulse / of the whole cycle)
    100% -> swing high  (the high of the impulse)

which is exactly how the levels are drawn in the reference screenshots
(0 at the bottom, 1 at the top, 0.5 = "цель" = target of a long correction).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Candle, Side

#: levels drawn on the chart (ratio -> label)
FIB_RATIOS: Tuple[float, ...] = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.618)


def fib_level(low: float, high: float, pct: float) -> float:
    """Price of the ``pct`` % level of the grid stretched from ``low`` to ``high``."""
    return low + (pct / 100.0) * (high - low)


def fib_grid(low: float, high: float, ratios: Sequence[float] = FIB_RATIOS) -> List[Dict[str, float]]:
    rng = high - low
    return [{"level": r, "price": low + r * rng, "label": f"{r:g}"} for r in ratios]


@dataclass
class Impulse:
    """Detected movement of ``n`` candles in one direction."""

    side: Side            # side of the *correction* trade: 'long' after a down move
    move_side: str        # 'down' | 'up' - direction of the impulse itself
    high: float
    low: float
    entry_price: float    # close of the last impulse candle
    target: float         # fib goal level (e.g. 50%)
    move_pct: float       # measured movement size in %
    candles: int
    start_ts: int
    end_ts: int

    def fib_levels(self, ratios: Sequence[float] = FIB_RATIOS) -> List[Dict[str, float]]:
        return fib_grid(self.low, self.high, ratios)


def _is_down(window: Sequence[Candle], mode: str) -> bool:
    if mode == "body":
        return all(c.close < c.open for c in window)
    return all(window[i].close < window[i - 1].close for i in range(1, len(window)))


def _is_up(window: Sequence[Candle], mode: str) -> bool:
    if mode == "body":
        return all(c.close > c.open for c in window)
    return all(window[i].close > window[i - 1].close for i in range(1, len(window)))


def detect_impulse(candles: Sequence[Candle], settings) -> Tuple[Optional[Impulse], str]:
    """Look for ``candles_count`` candles in one direction that form a big enough move.

    Returns ``(impulse, reason)`` where ``reason`` is a human readable explanation
    (always filled - handy for the log / UI when there is no signal).
    """
    n = int(settings.candles_count)
    if len(candles) < n:
        return None, f"мало свечей ({len(candles)}/{n})"

    window: List[Candle] = list(candles[-n:])
    down = _is_down(window, settings.candle_direction_mode)
    up = _is_up(window, settings.candle_direction_mode)
    need_up_impulse = "short" in settings.allowed_sides()
    need_down_impulse = "long" in settings.allowed_sides()

    if not (down or up):
        return None, f"нет {n} свечей в одну сторону"

    high = max(c.high for c in window)
    low = min(c.low for c in window)
    entry = float(window[-1].close)
    if low <= 0 or high <= low:
        return None, "некорректные данные свечей"

    if settings.move_measure == "range":
        move_pct = (high - low) / low * 100.0
    else:  # close-to-close of the movement
        move_pct = abs(window[-1].close - window[0].open) / window[0].open * 100.0

    if move_pct < settings.min_move_pct:
        return None, f"движение {move_pct:.2f}% < порога {settings.min_move_pct:g}%"

    side: Optional[Side] = None
    if down and need_down_impulse:
        side = "long"
    elif up and need_up_impulse:
        side = "short"

    if side is None:
        return None, "направление отключено в настройках"

    target = fib_level(low, high, settings.fib_goal_pct)

    # sanity checks: the trade must be opened below (long) / above (short) the target
    if side == "long":
        if entry >= target:
            return None, "цена уже выше цели по фибо"
        profit_pct = (target / entry - 1.0) * 100.0
    else:
        if entry <= target:
            return None, "цена уже ниже цели по фибо"
        profit_pct = (1.0 - target / entry) * 100.0

    if profit_pct < settings.min_profit_pct:
        return None, (f"профит до цели {profit_pct:.2f}% < минимального "
                      f"{settings.min_profit_pct:g}%")

    return Impulse(
        side=side,
        move_side="down" if down else "up",
        high=high,
        low=low,
        entry_price=entry,
        target=target,
        move_pct=move_pct,
        candles=n,
        start_ts=window[0].ts,
        end_ts=window[-1].ts,
    ), (f"сигнал {side} ({'падение' if down else 'рост'} {move_pct:.2f}%, "
        f"цель {target:g}, профит до цели {profit_pct:.2f}%)")
