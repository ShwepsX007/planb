"""Fibonacci grid helpers and movement detection.

Сетка всегда растягивается между двумя точками движения:

    уровень 0 %   -> КОНЕЦ движения (последний экстремум, от которого идёт коррекция)
    уровень 100 % -> НАЧАЛО движения (предыдущий противоположный экстремум)

направление движения задаёт, где нарисован 0:

* движение вниз  (падение): 0 внизу (минимум), 1 вверху (максимум) -> сделка long;
* движение вверх (рост):    0 вверху (максимум), 1 внизу (минимум) -> сделка short.

Поэтому коррекция отсчитывается всегда от конца движения: 50 % — середина сетки,
61.8 % — глубокая коррекция. Раньше сетка всегда рисовалась снизу вверх, из-за чего
на росте уровни были перевёрнуты, а цель считалась не с той стороны.

Движение ищет `detect_signal`:

* ``entry_mode="swing"`` (по умолчанию) — сетка тянется по ВСЕМУ движению: от
  предыдущего максимума до последнего минимума (и наоборот), а не только по
  последним N свечам. Вход — когда после движения началась коррекция;
* ``entry_mode="impulse"`` — старый метод: N свечей в одну сторону.
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


def retrace_price(low: float, high: float, move_side: str, pct: float) -> float:
    """Цена уровня ``pct`` % коррекции движения ``move_side``.

    Уровень 0 % — конец движения, 100 % — его начало. Для падения это снизу вверх
    (0 = минимум), для роста — сверху вниз (0 = максимум): сетка всегда тянется
    НАЗАД по движению, поэтому 50 % — середина, 61.8 % — глубокая коррекция.
    """
    span = high - low
    if move_side == "up":
        return high - (pct / 100.0) * span
    return low + (pct / 100.0) * span


def fib_grid(low: float, high: float, ratios: Sequence[float] = FIB_RATIOS,
             *, move_side: str = "down") -> List[Dict[str, float]]:
    """Уровни сетки с учётом направления движения (0 — конец движения)."""
    span = high - low
    if move_side == "up":
        return [{"level": r, "price": high - r * span, "label": f"{r:g}"} for r in ratios]
    return [{"level": r, "price": low + r * span, "label": f"{r:g}"} for r in ratios]


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
    correction_pct: float = 0.0   # насколько движение уже скорректировалось, %
    mode: str = "impulse"         # impulse — N свечей, swing — движение целиком

    def fib_levels(self, ratios: Sequence[float] = FIB_RATIOS) -> List[Dict[str, float]]:
        return fib_grid(self.low, self.high, ratios, move_side=self.move_side)


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

    # цель — уровень коррекции, отсчитанный от КОНЦА движения (низ для падения,
    # верх для роста), а не всегда «снизу вверх»
    target = retrace_price(low, high, "down" if down else "up", settings.fib_goal_pct)

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
        mode="impulse",
    ), (f"сигнал {side} ({'падение' if down else 'рост'} {move_pct:.2f}%, "
        f"цель {target:g}, профит до цели {profit_pct:.2f}%)")


def _zigzag(window: Sequence[Candle], thr_pct: float):
    """Развороты движения: список подтверждённых экстремумов + текущий экстремум.

    Классический зигзаг: тянем текущий экстремум, пока цена идёт в его сторону;
    как только цена отошла от него на ``thr_pct`` %, экстремум считается
    подтверждённым, направление меняется. Порог = минимальное движение, поэтому
    более мелкие откаты считаются шумом и не «режут» движение на куски.

    Возвращает ``(pivots, current)``: ``pivots`` — список ``(idx, kind, price)``
    подтверждённых экстремумов, ``current`` — текущий (ещё не подтверждённый).
    """
    n = len(window)
    if n < 2:
        return [], (0, "high", window[0].high if n else 0.0)

    lo_i, lo_p = 0, window[0].low
    hi_i, hi_p = 0, window[0].high
    direction: Optional[str] = None
    pivots: List[Tuple[int, str, float]] = []

    for i in range(1, n):
        c = window[i]
        if direction is None:
            if c.high > hi_p:
                hi_i, hi_p = i, c.high
            if c.low < lo_p:
                lo_i, lo_p = i, c.low
            if hi_i > lo_i and hi_p >= lo_p * (1.0 + thr_pct / 100.0):
                direction = "up"          # выросли от минимума
                pivots.append((lo_i, "low", lo_p))
            elif lo_i > hi_i and lo_p <= hi_p * (1.0 - thr_pct / 100.0):
                direction = "down"        # упали от максимума
                pivots.append((hi_i, "high", hi_p))
        elif direction == "up":
            if c.high > hi_p:
                hi_i, hi_p = i, c.high
            elif c.low <= hi_p * (1.0 - thr_pct / 100.0):
                pivots.append((hi_i, "high", hi_p))
                direction = "down"
                lo_i, lo_p = i, c.low
        else:  # down
            if c.low < lo_p:
                lo_i, lo_p = i, c.low
            elif c.high >= lo_p * (1.0 + thr_pct / 100.0):
                pivots.append((lo_i, "low", lo_p))
                direction = "up"
                hi_i, hi_p = i, c.high

    if direction == "up":
        current = (hi_i, "high", hi_p)
    elif direction == "down":
        current = (lo_i, "low", lo_p)
    else:
        current = (hi_i, "high", hi_p) if hi_i >= lo_i else (lo_i, "low", lo_p)
    return pivots, current


def _leg_from_zigzag(window: Sequence[Candle], pivots, current, thr_pct: float):
    """Выделить последнюю НОГУ движения по разворотам зигзага.

    Точки разворота чередуются (максимум/минимум). Берём три последние точки
    ``A -> B -> C``:

    * ``A -> B`` — последняя завершённая нога;
    * ``B -> C`` — текущая коррекция к ней.

    Пока коррекция не прошла начало ноги ``A``, движение — это ``A -> B``
    (именно его сетку и надо тянуть), а ``C`` — коррекция, на которой входим.
    Если цена ушла за ``A`` — движение развернулось, и нога теперь ``B -> C``.
    """
    pts = list(pivots)
    if not pts or pts[-1][1] != current[1]:
        pts.append(current)

    if len(pts) >= 3:
        a, b, c = pts[-3], pts[-2], pts[-1]
        if b[1] == "low":                       # A(high) -> B(low), коррекция вверх
            if c[2] < a[2]:
                return a, b                     # начало падения не пройдено
        else:                                   # A(low) -> B(high), коррекция вниз
            if c[2] > a[2]:
                return a, b
        return b, c

    if len(pts) == 2:
        a, b = pts
        if a[1] != b[1] and a[0] != b[0]:
            return a, b
        return None

    # подтверждённых разворотов нет: движение — весь диапазон до экстремума
    idx, kind, _price = pts[0]
    if kind == "low":
        hi_i = max(range(idx + 1), key=lambda i: window[i].high)
        return (hi_i, "high", window[hi_i].high), pts[0]
    lo_i = min(range(idx + 1), key=lambda i: window[i].low)
    return (lo_i, "low", window[lo_i].low), pts[0]


def detect_swing(candles: Sequence[Candle], settings) -> Tuple[Optional[Impulse], str]:
    """Найти ДВИЖЕНИЕ ЦЕЛИКОМ, а не только последние N свечей.

    Берём окно ``swing_lookback`` свечей, строим зигзаг с порогом ``min_move_pct``
    и выделяем последнюю ногу: от предыдущего противоположного экстремума
    (максимум при падении, минимум при росте) до последнего экстремума. Сетка
    тянется по всему этому движению:

        падение: 1 = предыдущий максимум, 0 = последний минимум  (0 внизу);
        рост:    1 = предыдущий минимум,   0 = последний максимум (0 вверху).

    Число свечей не задаётся заранее: начало и конец ноги выбираются по экстремумам,
    а длительность лишь записывается в сигнал. Сигнал появляется, когда движение
    достигло минимального процента, экстремум свежий и началась коррекция: цена
    отошла от экстремума, но ещё не дошла до цели. Поэтому сетка строится по
    реальному размаху рынка, а не по фиксированному числу последних свечей.
    """
    lookback = int(getattr(settings, "swing_lookback", 120) or 120)
    fresh = int(getattr(settings, "swing_fresh_candles", 24) or 24)
    # Swing mode needs only enough data to establish two price points. The
    # movement's actual candle count is measured from its detected pivots; the
    # legacy candles_count setting applies only to impulse mode.
    min_data_candles = 2
    if len(candles) < min_data_candles:
        return None, f"мало свечей ({len(candles)}/{min_data_candles})"

    window: List[Candle] = list(candles[-max(lookback, min_data_candles):])
    m = len(window)
    thr = max(0.01, float(settings.min_move_pct))
    pivots, current = _zigzag(window, thr)
    leg = _leg_from_zigzag(window, pivots, current, thr)
    if leg is None:
        return None, "нет выраженного движения: зигзаг не нашёл разворотов"
    start, end = leg

    start_i, end_i = start[0], end[0]
    leg_candles = end_i - start_i + 1

    leg_candles_slice = window[start_i:end_i + 1]
    high = max(c.high for c in leg_candles_slice)
    low = min(c.low for c in leg_candles_slice)
    if low <= 0 or high <= low:
        return None, "некорректные данные свечей"
    move_side = "down" if start[1] == "high" else "up"

    entry = float(window[-1].close)
    span = high - low
    move_pct = ((high - low) / low * 100.0 if settings.move_measure == "range"
                else abs(entry - window[start_i].open) / window[start_i].open * 100.0)
    # сначала размер движения: именно он чаще всего объясняет тишину
    if move_pct < settings.min_move_pct:
        return None, (f"движение {move_pct:.2f}% < порога {settings.min_move_pct:g}% "
                      f"(за {leg_candles} свечей)")
    age = m - 1 - end_i
    if age > fresh:
        return None, (f"экстремум движения был {age} свечей назад "
                      f"(порог свежести {fresh}) — движение уже отработано")

    if move_side == "down":
        if entry <= low:
            return None, "падение ещё продолжается — коррекции нет, входить не на чем"
        if entry >= high:
            return None, "цена вернулась к началу падения — движения уже нет"
        correction_pct = (entry - low) / span * 100.0
    else:
        if entry >= high:
            return None, "рост ещё продолжается — коррекции нет, входить не на чем"
        if entry <= low:
            return None, "цена вернулась к началу роста — движения уже нет"
        correction_pct = (high - entry) / span * 100.0

    # вход именно на коррекции: цена должна заметно отойти от экстремума,
    # иначе это вход «в самом экстремуме» — старое поведение, а не коррекция
    min_corr = float(getattr(settings, "min_correction_pct", 5.0) or 0.0)
    if correction_pct < min_corr:
        return None, (f"коррекция {correction_pct:.1f}% < минимальной {min_corr:g}% — "
                      f"цена ещё у экстремума, входить рано")

    side: Optional[Side] = "long" if move_side == "down" else "short"
    if side not in settings.allowed_sides():
        return None, "направление отключено в настройках"

    target = retrace_price(low, high, move_side, settings.fib_goal_pct)
    if side == "long":
        if entry >= target:
            return None, (f"коррекция {correction_pct:.2f}% уже дошла до цели — "
                          f"входить поздно")
        profit_pct = (target / entry - 1.0) * 100.0
    else:
        if entry <= target:
            return None, (f"коррекция {correction_pct:.2f}% уже дошла до цели — "
                          f"входить поздно")
        profit_pct = (1.0 - target / entry) * 100.0

    if profit_pct < settings.min_profit_pct:
        return None, (f"профит до цели {profit_pct:.2f}% < минимального "
                      f"{settings.min_profit_pct:g}%")

    return Impulse(
        side=side,
        move_side=move_side,
        high=high,
        low=low,
        entry_price=entry,
        target=target,
        move_pct=move_pct,
        candles=leg_candles,
        start_ts=window[start_i].ts,
        end_ts=window[end_i].ts,
        correction_pct=correction_pct,
        mode="swing",
    ), (f"движение {leg_candles} свечей {'вниз' if move_side == 'down' else 'вверх'} "
        f"{move_pct:.2f}% [{low:g}..{high:g}], коррекция {correction_pct:.1f}% -> "
        f"сигнал {side}, цель {target:g}, профит до цели {profit_pct:.2f}%")


def detect_signal(candles: Sequence[Candle], settings) -> Tuple[Optional[Impulse], str]:
    """Диспетчер: какой метод поиска движения выбран в настройках."""
    mode = str(getattr(settings, "entry_mode", "swing") or "swing").lower()
    if mode == "impulse":
        return detect_impulse(candles, settings)
    return detect_swing(candles, settings)
