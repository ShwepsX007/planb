"""The strategy engine.

It is a pure, deterministic, exchange-agnostic state machine:

    engine.on_tick(last_price, live_candle) -> [Action, ...]
    engine.on_candle(closed_candle)        -> [Action, ...]
    engine.apply_fill(action, fill)        # result of an executed Action

The exact same code is used live, in paper mode and by the backtester, which
guarantees that a backtest reproduces what the bot does in production.

Algorithm (long side; the short side is the mirror image)
---------------------------------------------------------
1. Signal in the default `swing` mode: detect a complete leg between price
   extrema using `min_move_pct`; the number of candles is measured automatically,
   not configured. The old `impulse` mode alone requires `candles_count` candles
   in one direction. For a long, the swing is `swing_high` = prior high and
   `swing_low` = last low; the grid spans the complete move.
2. Entry #1 at the market on the close of the last impulse candle, provided the
   distance from the entry to the fib target (`fib_goal_pct`, e.g. 50%) is at
   least `min_profit_pct`.
3. Every new candle that undercuts the swing low moves the grid (and therefore
   the target) down, closer to the average entry.
4. When the profit between the volume weighted average entry and the target
   drops to `min_profit_pct`, another position is opened at the market with the
   next volume of the doubling schedule (base, base, 2x, 4x, 8x ...). Its goal
   is still the same fib target, but now the position is closed at the *average*
   entry price which is lower than the target.
5. When price touches the target:
   - the profit of the average position is >= `min_profit_pct` -> close the whole
     cycle (all positions) and start looking for a new signal;
   - otherwise (a position limit is reached) -> the cycle is closed at the target
     as soon as the average position is in profit, so the bot never stays stuck.
6. The realized profit of every cycle accumulates; once
   `total_profit_target_usd` is reached the bot stops.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .config import BotSettings
from .fib import _is_down, _is_up, detect_signal, retrace_price
from .models import Action, Candle, Cycle, Fill, Position, Side, now_ms


class StrategyEngine:
    def __init__(self, settings: BotSettings, *, cycles_total: int = 0,
                 realized_pnl: float = 0.0, wins: int = 0, losses: int = 0) -> None:
        self.settings = settings
        self.cycle: Optional[Cycle] = None
        self.candles: List[Candle] = []
        self.last_price: float = 0.0
        self.realized_pnl: float = float(realized_pnl)
        self.cycles_total: int = int(cycles_total)
        self.wins: int = int(wins)
        self.losses: int = int(losses)
        self.stopped: bool = False
        self.stop_reason: str = ""
        self.last_check: str = "ожидание свечей"
        self.last_error: str = ""
        self.signals_found: int = 0
        #: время последней свечи, которую движок реально обработал (диагностика:
        #: «бот молчит» — значит сюда смотреть в первую очередь)
        self.last_candle_ts: int = 0
        self.candles_seen: int = 0
        self.closed_cycles: List[Cycle] = []
        #: Последняя формирующаяся свеча, обновляемая по тикам. Она участвует
        #: в детекторе сигналов, но не добавляется в историю закрытых свечей.
        self._tick_candle: Optional[Candle] = None
        #: Не открываем повторно тот же сигнал после закрытия цикла.
        self._consumed_signals: set[tuple] = set()

    # ------------------------------------------------------------------ feeding
    def set_history(self, candles: Sequence[Candle]) -> None:
        """Preload closed candles (used after a restart)."""
        self.candles = list(candles)[-500:]
        if self.candles:
            self.last_price = self.candles[-1].close
            self.last_candle_ts = self.candles[-1].ts

    def push_candle(self, candle: Candle, *, update_last_price: bool = True) -> None:
        if self.candles and candle.ts <= self.candles[-1].ts:
            return
        self.candles_seen += 1
        self.last_candle_ts = candle.ts
        self.candles.append(candle)
        if len(self.candles) > 500:
            self.candles = self.candles[-500:]
        if update_last_price:
            self.last_price = candle.close

    # ------------------------------------------------------------------ events
    def on_tick(self, price: float, candle: Optional[Candle] = None) -> List[Action]:
        """Обработать текущую цену, не ожидая закрытия таймфрейм-свечи.

        ``candle`` — текущая формирующаяся OHLC-свеча фида. На каждом обновлении
        цены стратегия обновляет экстремумы/цель, проверяет выходы и доливы, а
        также проверяет сигнал на истории с заменённой формирующейся свечой.
        """
        if price <= 0:
            return []
        self.last_price = price
        live = self._live_candle(price, candle)
        if self.stopped:
            return []

        if self.cycle is None:
            signal_candles = list(self.candles)
            if signal_candles and signal_candles[-1].ts == live.ts:
                signal_candles[-1] = live
            elif not signal_candles or live.ts > signal_candles[-1].ts:
                signal_candles.append(live)
            impulse, reason = detect_signal(signal_candles, self.settings)
            self.last_check = reason
            if impulse is None:
                return []
            token = self._signal_token(impulse)
            if token in self._consumed_signals:
                return []
            self._consumed_signals.add(token)
            self.signals_found += 1
            return [self._open_cycle(impulse, live, action_ts=now_ms())]

        cycle = self.cycle
        self._update_cycle_grid(cycle, price, live)
        target_touched_in_profit = ((price >= cycle.target and price > cycle.avg_entry)
                                    if cycle.side == "long" else
                                    (price <= cycle.target and price < cycle.avg_entry))
        actions = self._exit_checks(cycle, high=price, low=price, open_price=price,
                                    ts=now_ms(), close_price=price)
        if actions or target_touched_in_profit:
            # Если цель уже затронута, но минимум профита ещё не набран, сохраняем
            # сообщение _exit_checks и не подменяем его сообщением о готовом доливе.
            return actions
        return self._tick_add(cycle, price, now_ms())

    def _live_candle(self, price: float, candle: Optional[Candle]) -> Candle:
        """Вернуть актуальную формирующуюся свечу, не меняя закрытую историю."""
        if candle is not None:
            ts = int(candle.ts)
            prev = self._tick_candle if self._tick_candle and self._tick_candle.ts == ts else None
            live = Candle(ts=ts, open=float(candle.open),
                          high=max(float(candle.high), price, prev.high if prev else price),
                          low=min(float(candle.low), price, prev.low if prev else price),
                          close=price, volume=float(candle.volume))
        else:
            from .config import TIMEFRAMES
            width = int(TIMEFRAMES.get(self.settings.timeframe, 60) * 1000)
            ts = now_ms() // width * width
            prev = self._tick_candle if self._tick_candle and self._tick_candle.ts == ts else None
            open_price = prev.open if prev else (self.candles[-1].close if self.candles else price)
            live = Candle(ts=ts, open=open_price,
                          high=max(price, prev.high if prev else price),
                          low=min(price, prev.low if prev else price), close=price,
                          volume=prev.volume if prev else 0.0)
        self._tick_candle = live
        return live

    @staticmethod
    def _signal_token(impulse) -> tuple:
        return (impulse.side, impulse.move_side, impulse.start_ts, impulse.end_ts)

    def on_candle(self, candle: Candle) -> List[Action]:
        """Process a single *closed* candle (the backtest path)."""
        self.push_candle(candle)
        if self._tick_candle and self._tick_candle.ts <= candle.ts:
            self._tick_candle = None
        if self.stopped:
            return []

        if self.cycle is None:
            impulse, reason = detect_signal(self.candles, self.settings)
            self.last_check = reason
            if impulse is None:
                return []
            if self._signal_token(impulse) in self._consumed_signals:
                return []
            self._consumed_signals.add(self._signal_token(impulse))
            self.signals_found += 1
            return [self._open_cycle(impulse, candle)]

        return self._manage(self.cycle, candle)

    # ------------------------------------------------------------- cycle logic
    def _update_cycle_grid(self, cycle: Cycle, price: float,
                           live: Optional[Candle] = None) -> None:
        """Тянуть границы движения и пересчитывать фибо по каждому тиковому апдейту."""
        s = self.settings
        high = max(price, live.high if live else price)
        low = min(price, live.low if live else price)
        if cycle.side == "long":
            cycle.swing_low = min(cycle.swing_low, low)
            if s.update_swing_in_cycle:
                cycle.swing_high = max(cycle.swing_high, high)
        else:
            cycle.swing_high = max(cycle.swing_high, high)
            if s.update_swing_in_cycle:
                cycle.swing_low = min(cycle.swing_low, low)
        cycle.target = retrace_price(cycle.swing_low, cycle.swing_high,
                                     cycle.move_side, cycle.goal_pct)

    def next_add_plan(self, current_price: float = 0.0) -> Optional[Dict[str, Any]]:
        """Рассчитать лимитный уровень следующего усреднения.

        Уровень выводится из условия стратегии: расстояние от средней позиции до
        обновляемой цели Фибо становится не больше ``min_profit_pct``. Это дает
        заранее видимую цену заявки, а не сигнал, возникающий только после касания.
        """
        cycle = self.cycle
        s = self.settings
        if cycle is None or not cycle.positions or len(cycle.positions) >= s.max_positions:
            return None
        if not cycle.open_positions:
            return None
        ratio = cycle.goal_pct / 100.0
        if ratio >= 1.0 or ratio <= 0.0:
            return None  # для внешней/нулевой коррекции уровень усреднения не выводится
        epsilon = max(cycle.avg_entry * 1e-6, 1e-8)
        min_profit = s.min_profit_pct / 100.0
        if cycle.side == "long":
            threshold_target = cycle.avg_entry * (1.0 + min_profit)
            trigger = (threshold_target - ratio * cycle.swing_high) / (1.0 - ratio)
            if s.require_new_extreme_for_add:
                trigger = min(trigger, cycle.extreme_at_last_entry - epsilon)
            if current_price > 0 and current_price <= trigger:
                trigger = current_price  # marketable limit; already crossed while polling
        else:
            threshold_target = cycle.avg_entry * (1.0 - min_profit)
            trigger = (threshold_target - ratio * cycle.swing_low) / (1.0 - ratio)
            if s.require_new_extreme_for_add:
                trigger = max(trigger, cycle.extreme_at_last_entry + epsilon)
            if current_price > 0 and current_price >= trigger:
                trigger = current_price
        if trigger <= 0:
            return None
        idx = len(cycle.positions) + 1
        return {"idx": idx, "side": cycle.side, "price": trigger,
                "volume_usd": s.volume_for_position(idx),
                "target": cycle.target}

    def _tick_add(self, cycle: Cycle, price: float, ts: int) -> List[Action]:
        """Проверить условие долива на текущей цене, а не на закрытии свечи."""
        s = self.settings
        if not cycle.positions:
            return []
        if len(cycle.open_positions) >= s.max_positions:
            self.last_check = f"лимит позиций {s.max_positions} достигнут"
            return []
        profit_pct = cycle.profit_pct_to(cycle.target)
        new_extreme = (cycle.swing_low < cycle.extreme_at_last_entry if cycle.side == "long"
                       else cycle.swing_high > cycle.extreme_at_last_entry)
        guard_ok = (not s.require_new_extreme_for_add) or new_extreme
        if profit_pct <= s.min_profit_pct + 1e-9 and guard_ok:
            idx = len(cycle.positions) + 1
            volume = s.volume_for_position(idx)
            self.last_check = (f"долив #{idx} готов по текущей цене: профит до цели "
                               f"{profit_pct:.3f}% <= {s.min_profit_pct:g}%")
            return [Action(kind="add", side=cycle.side, ref_price=price,
                           volume_usd=volume, position_idx=idx, target=cycle.target,
                           reason=(f"профит до цели {profit_pct:.3f}% <= минимума "
                                   f"{s.min_profit_pct:g}% (долив по текущей цене)"),
                           ts=ts)]
        if profit_pct <= s.min_profit_pct + 1e-9 and not guard_ok:
            edge = "минимум" if cycle.side == "long" else "максимум"
            self.last_check = (f"профит до цели {profit_pct:.2f}% <= {s.min_profit_pct:g}%, "
                               f"нужен новый {edge} для долива #{len(cycle.positions) + 1}")
        else:
            self.last_check = (f"в позиции, профит до цели {profit_pct:.2f}% "
                               f"(мин. {s.min_profit_pct:g}%)")
        return []

    def _open_cycle(self, impulse, candle: Candle, *, action_ts: Optional[int] = None) -> Action:
        s = self.settings
        cycle = Cycle(
            id=uuid.uuid4().hex[:12],
            side=impulse.side,
            signal_ts=impulse.end_ts,
            signal_price=impulse.entry_price,
            opened_ts=candle.ts,
            swing_high=impulse.high,
            swing_low=impulse.low,
            goal_pct=s.fib_goal_pct,
            target=impulse.target,
            move_side=impulse.move_side,
            extreme_at_last_entry=impulse.low if impulse.side == "long" else impulse.high,
            note=(f"{'движение целиком' if impulse.mode == 'swing' else 'импульс'}: "
                  f"{impulse.candles} свечей {impulse.move_side} {impulse.move_pct:.2f}% "
                  f"[{impulse.low:g}..{impulse.high:g}]"
                  + (f", коррекция {impulse.correction_pct:.1f}%" if impulse.mode == "swing" else "")),
        )
        self.cycle = cycle
        volume = s.volume_for_position(1)
        return Action(
            kind="entry",
            side=cycle.side,
            ref_price=impulse.entry_price,
            volume_usd=volume,
            position_idx=1,
            target=cycle.target,
            reason=f"сигнал: {impulse.candles} свечей {impulse.move_side} {impulse.move_pct:.2f}%",
            ts=action_ts if action_ts is not None else candle.ts,
        )

    def _manage(self, cycle: Cycle, candle: Candle) -> List[Action]:
        s = self.settings

        # 1. stretch / move the grid -----------------------------------------
        if cycle.side == "long":
            if candle.low < cycle.swing_low:
                cycle.swing_low = candle.low
            if s.update_swing_in_cycle and candle.high > cycle.swing_high:
                cycle.swing_high = candle.high
        else:
            if candle.high > cycle.swing_high:
                cycle.swing_high = candle.high
            if s.update_swing_in_cycle and candle.low < cycle.swing_low:
                cycle.swing_low = candle.low
        cycle.target = retrace_price(cycle.swing_low, cycle.swing_high,
                                     cycle.move_side, cycle.goal_pct)
        cycle.max_positions_used = max(cycle.max_positions_used, len(cycle.positions))

        # 2. target / stop-loss -------------------------------------------------
        actions = self._exit_checks(cycle, high=candle.high, low=candle.low,
                                    open_price=candle.open, ts=candle.ts,
                                    close_price=candle.close)
        if actions:
            return actions

        # 3. add another averaged position --------------------------------------
        if len(cycle.positions) < s.max_positions and cycle.positions:
            avg = cycle.avg_entry
            profit_pct = cycle.profit_pct_to(cycle.target)
            new_extreme = (cycle.swing_low < cycle.extreme_at_last_entry if cycle.side == "long"
                           else cycle.swing_high > cycle.extreme_at_last_entry)
            guard_ok = (not s.require_new_extreme_for_add) or new_extreme
            if profit_pct <= s.min_profit_pct + 1e-9 and guard_ok:
                idx = len(cycle.positions) + 1
                volume = s.volume_for_position(idx)
                # Усреднение делаем ВСЕГДА, когда сработали условия входа: каждая
                # новая позиция сдвигает среднюю к цене и приближает выход в плюс.
                # Раньше здесь стоял запрет «долив не поможет», и на мелких объёмах
                # он блокировал вообще все доливы: цель едет вниз вместе с ценой,
                # профит после усреднения остаётся около нуля, и цикл застревал
                # с одной позицией (см. «бот как бы вошёл и ничего не делает»).
                qty_now = cycle.total_qty
                add_qty = volume / candle.close if candle.close > 0 else 0.0
                new_avg = ((avg * qty_now + candle.close * add_qty) / (qty_now + add_qty)
                           if qty_now + add_qty > 0 else avg)
                profit_after = (new_avg and (
                    (cycle.target / new_avg - 1.0) * 100.0 if cycle.side == "long"
                    else (1.0 - cycle.target / new_avg) * 100.0))
                self.last_check = (f"долив #{idx}: профит до цели {profit_pct:.3f}% "
                                   f"<= {s.min_profit_pct:g}%, средняя {avg:g} → {new_avg:g}")
                return [Action(
                    kind="add",
                    side=cycle.side,
                    ref_price=candle.close,
                    volume_usd=volume,
                    position_idx=idx,
                    target=cycle.target,
                    reason=(f"профит до цели {profit_pct:.3f}% <= минимума "
                            f"{s.min_profit_pct:g}% (средняя {avg:g}, после долива "
                            f"{new_avg:g}, профит {profit_after:.3f}%)"),
                    ts=candle.ts,
                )]
            if profit_pct <= s.min_profit_pct + 1e-9 and not guard_ok:
                self.last_check = (f"ждём новый экстремум для долива #{len(cycle.positions) + 1} "
                                   f"(профит до цели {profit_pct:.2f}% уже <= "
                                   f"{s.min_profit_pct:g}%)")
            else:
                self.last_check = (f"в позиции, профит до цели {profit_pct:.2f}% "
                                   f"(мин. {s.min_profit_pct:g}%)")
        elif cycle.positions:
            self.last_check = f"лимит позиций {s.max_positions} достигнут"
        return []

    def _exit_checks(self, cycle: Cycle, *, high: float, low: float,
                     open_price: float, ts: int,
                     close_price: Optional[float] = None) -> List[Action]:
        s = self.settings
        if not cycle.positions:
            return []
        long = cycle.side == "long"
        avg = cycle.avg_entry
        target = cycle.target
        maxed = len(cycle.open_positions) >= s.max_positions

        # emergency stop of the cycle
        if s.cycle_stop_loss_pct > 0:
            price_for_sl = low if long else high
            adverse = ((avg - price_for_sl) / avg * 100.0) if long else \
                      ((price_for_sl - avg) / avg * 100.0)
            if adverse >= s.cycle_stop_loss_pct:
                return [Action(kind="close_all", side=cycle.side, ref_price=price_for_sl,
                               qty=cycle.total_qty, reason="стоп-лосс", ts=ts)]

        # 1. Дошла ли цена до цели в этом такте/свече?
        target_touched = (high >= target) if long else (low <= target)
        if not target_touched:
            self._note_target_wait(cycle, avg, target)
            return []

        # 2. Какую цену реально можно получить СЕЙЧАС?
        #    Раньше прибыль считалась только «до цели». Если цель совпала со средней
        #    (типичный случай после усреднений), профит до неё ~0 — и бот игнорировал
        #    даже резкий уход цены далеко в нужную сторону: сделка «зависала».
        now_price = close_price if close_price not in (None, 0) else open_price
        now_price = max(low, min(high, float(now_price)))
        if long:
            best = max(now_price, target)          # текущая цена, но не ниже цели
            best = min(best, high)
        else:
            best = min(now_price, target)
            best = max(best, low)
        profit_best = cycle.profit_pct_to(best)

        armed = True
        if s.tp_requires_min_profit:
            armed = profit_best >= s.min_profit_pct - 1e-9
        if maxed and s.close_at_target_when_maxed:
            # лимит позиций: закрываем по цели, как только это вообще в плюс
            armed = True
        profitable = (best > avg) if long else (best < avg)

        if armed and profitable:
            reason = (f"цель по фибо {cycle.goal_pct:g}% = {target:g}"
                      + (f" (цена {best:g}, лучше цели)" if (
                          best > target if long else best < target) else ""))
            return [Action(kind="close_all", side=cycle.side, ref_price=best,
                           qty=cycle.total_qty, reason=reason, ts=ts)]

        # цель достигнута, но профита пока мало — говорим об этом пользователю,
        # а не молчим: именно это выглядело как «сделка висит»
        self.last_check = (f"цель {target:g} достигнута, но профит {profit_best:.2f}% "
                           f"< минимума {s.min_profit_pct:g}% — держим и ждём цену "
                           f"{'выше' if long else 'ниже'}")
        return []

    def _note_target_wait(self, cycle: Cycle, avg: float, target: float) -> None:
        """Понятный статус, пока цена не дошла до цели.

        Вызывается и между свечами (по тикам), поэтому здесь нельзя писать
        «ждём долива», если долив на самом деле запрещён условиями: именно такая
        надпись и выглядела как «бот ничего не делает».
        """
        s = self.settings
        profit_pct = cycle.profit_pct_to(target)
        if len(cycle.positions) < s.max_positions:
            nxt = len(cycle.positions) + 1
            new_extreme = (cycle.swing_low < cycle.extreme_at_last_entry if cycle.side == "long"
                           else cycle.swing_high > cycle.extreme_at_last_entry)
            ready = profit_pct <= s.min_profit_pct + 1e-9
            if ready and (not s.require_new_extreme_for_add or new_extreme):
                self.last_check = (f"долив #{nxt} готов: профит до цели {profit_pct:.2f}% "
                                   f"<= {s.min_profit_pct:g}%, экстремум обновлён — "
                                   f"исполняется по текущей цене")
                return
            if ready:
                edge = "минимум" if cycle.side == "long" else "максимум"
                self.last_check = (f"профит до цели {profit_pct:.2f}% <= "
                                   f"{s.min_profit_pct:g}%, нужен новый {edge} для долива "
                                   f"#{nxt} (сейчас {cycle.swing_low if cycle.side == 'long' else cycle.swing_high:g})")
                return
        elif cycle.positions:
            self.last_check = (f"цель ещё не достигнута, профит до цели {profit_pct:.2f}% — "
                               f"лимит позиций {s.max_positions} достигнут")
            return
        if profit_pct < 0:
            where = "ниже" if cycle.side == "long" else "выше"
            self.last_check = (f"цель {target:g} {where} средней {avg:g}: профита по цели нет, "
                               f"ждём откат цены к средней")
            return
        self.last_check = (f"в позиции, профит до цели {profit_pct:.2f}% "
                           f"(мин. {s.min_profit_pct:g}%)")

    # ------------------------------------------------------------------- fills
    def apply_fill(self, action: Action, fill: Fill) -> Position:
        cycle = self.cycle
        if cycle is None:
            raise RuntimeError("нет активного цикла")
        if action.kind == "entry":
            cycle.positions = []
            cycle.opened_ts = fill.ts or now_ms()
        existing = next((p for p in cycle.positions
                         if p.idx == action.position_idx and p.is_open), None)
        if existing is not None:
            total_qty = existing.qty + fill.qty
            if total_qty > 0:
                existing.price = (existing.price * existing.qty + fill.price * fill.qty) / total_qty
            existing.qty = total_qty
            existing.volume_usd += fill.qty * fill.price
            existing.fee += fill.fee
            pos = existing
        else:
            pos = Position(
                idx=action.position_idx,
                ts=fill.ts or now_ms(),
                price=fill.price,
                qty=fill.qty,
                volume_usd=fill.qty * fill.price,
                fee=fill.fee,
                multiplier=self.settings.volume_for_position(action.position_idx) / self.settings.base_volume_usd,
            )
            cycle.positions.append(pos)
        cycle.extreme_at_last_entry = cycle.swing_low if cycle.side == "long" else cycle.swing_high
        cycle.max_positions_used = max(cycle.max_positions_used, len(cycle.positions))
        cycle.target = retrace_price(cycle.swing_low, cycle.swing_high,
                                     cycle.move_side, cycle.goal_pct)
        return pos

    def close_cycle(self, price: float, ts: int, reason: str, *, qty: Optional[float] = None,
                    fee: Optional[float] = None) -> Cycle:
        """Close every open position of the active cycle at ``price``."""
        cycle = self.cycle
        if cycle is None:
            raise RuntimeError("нет активного цикла")
        total_qty = cycle.total_qty
        if total_qty <= 0:
            cycle.status = "closed"
            cycle.closed_ts = ts
            cycle.close_price = price
            cycle.close_reason = reason
            self.cycle = None
            return cycle
        if fee is None:
            fee = price * total_qty * self.settings.fee_pct / 100.0
        pnl_net = 0.0
        fee_per_qty = fee / total_qty if total_qty else 0.0
        for p in cycle.positions:
            if not p.is_open:
                continue
            gross = (price - p.price) * p.qty if cycle.side == "long" else (p.price - price) * p.qty
            close_fee = fee_per_qty * p.qty
            p.close_ts = ts
            p.close_price = price
            p.close_fee = close_fee
            p.pnl = gross - close_fee - p.fee
            pnl_net += p.pnl
        cycle.status = "closed"
        cycle.closed_ts = ts
        cycle.close_price = price
        cycle.close_reason = reason
        cycle.pnl = pnl_net
        cycle.fees = sum(p.fee + p.close_fee for p in cycle.positions)
        invested = cycle.invested_volume_usd or 1.0
        cycle.pnl_pct = pnl_net / invested * 100.0

        self.realized_pnl += pnl_net
        self.cycles_total += 1
        if pnl_net >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self.closed_cycles.append(cycle)
        if len(self.closed_cycles) > 200:
            self.closed_cycles = self.closed_cycles[-200:]
        self.cycle = None
        self.last_check = f"цикл закрыт: {reason}, результат {pnl_net:+.4f}$"

        if reason == "стоп-лосс":
            self.stop("стоп-лосс по циклу")
        elif (self.settings.stop_after_total_target and self.settings.total_profit_target_usd > 0
              and self.realized_pnl >= self.settings.total_profit_target_usd):
            self.stop(f"достигнут общий профит {self.realized_pnl:.2f}$ "
                      f">= {self.settings.total_profit_target_usd:g}$")
        return cycle

    # -------------------------------------------------------------------- misc
    def stop(self, reason: str) -> None:
        self.stopped = True
        self.stop_reason = reason
        self.last_check = f"остановка: {reason}"

    def resume(self) -> None:
        self.stopped = False
        self.stop_reason = ""

    def state(self) -> Dict[str, Any]:
        cycle = self.cycle
        out: Dict[str, Any] = {
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "last_check": self.last_check,
            "last_candle_ts": self.last_candle_ts,
            "candles_seen": self.candles_seen,
            "last_price": self.last_price,
            "realized_pnl": round(self.realized_pnl, 6),
            "cycles_total": self.cycles_total,
            "wins": self.wins,
            "losses": self.losses,
            "signals_found": self.signals_found,
            "win_rate": round(self.wins / self.cycles_total * 100, 2) if self.cycles_total else 0.0,
            "cycle": None,
        }
        if cycle is not None:
            avg = cycle.avg_entry
            s = self.settings
            next_idx = len(cycle.positions) + 1
            out["cycle"] = {
                **cycle.dict(),
                "profit_pct_to_target": round(cycle.profit_pct_to(cycle.target), 4),
                "unrealized_pnl": round(cycle.unrealized_pnl(self.last_price or avg), 6),
                "expected_pnl_at_target": round(cycle.expected_pnl_at_target(s.fee_pct), 6),
                "next_position_idx": next_idx if next_idx <= s.max_positions else None,
                "next_position_volume_usd": (s.volume_for_position(next_idx)
                                             if next_idx <= s.max_positions else None),
                "positions_left": max(0, s.max_positions - len(cycle.positions)),
                "distance_to_target_pct": round(
                    (cycle.target / self.last_price - 1) * 100 if cycle.side == "long" and self.last_price
                    else (1 - cycle.target / self.last_price) * 100 if self.last_price else 0.0, 4),
            }
        return out

    def snapshot(self) -> Dict[str, Any]:
        """Serializable engine state, used for restart recovery."""
        return {
            "cycle": self.cycle.dict() if self.cycle else None,
            "cycle_raw": self._cycle_raw(self.cycle) if self.cycle else None,
            "realized_pnl": self.realized_pnl,
            "cycles_total": self.cycles_total,
            "wins": self.wins,
            "losses": self.losses,
            "signals_found": self.signals_found,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
        }

    @staticmethod
    def _cycle_raw(cycle: Optional[Cycle]) -> Optional[dict]:
        if cycle is None:
            return None
        return {
            "id": cycle.id, "side": cycle.side, "signal_ts": cycle.signal_ts,
            "signal_price": cycle.signal_price, "opened_ts": cycle.opened_ts,
            "swing_high": cycle.swing_high, "swing_low": cycle.swing_low,
            "goal_pct": cycle.goal_pct, "target": cycle.target, "note": cycle.note,
            "extreme_at_last_entry": cycle.extreme_at_last_entry,
            "max_positions_used": cycle.max_positions_used,
            "positions": [
                {"idx": p.idx, "ts": p.ts, "price": p.price, "qty": p.qty,
                 "volume_usd": p.volume_usd, "fee": p.fee, "multiplier": p.multiplier}
                for p in cycle.positions
            ],
        }

    def restore(self, raw: Optional[dict]) -> None:
        if not raw:
            return
        positions = [Position(**p) for p in raw.get("positions", [])]
        self.cycle = Cycle(
            id=raw.get("id", uuid.uuid4().hex[:12]),
            side=raw.get("side", "long"),
            signal_ts=raw.get("signal_ts", 0),
            signal_price=raw.get("signal_price", 0.0),
            opened_ts=raw.get("opened_ts", 0),
            swing_high=raw.get("swing_high", 0.0),
            swing_low=raw.get("swing_low", 0.0),
            # старые сохранённые циклы не знали направления — выводим его из стороны
            move_side=raw.get("move_side") or ("down" if raw.get("side", "long") == "long" else "up"),
            goal_pct=raw.get("goal_pct", self.settings.fib_goal_pct),
            target=raw.get("target", 0.0),
            positions=positions,
            note=raw.get("note", ""),
            # старые сохранения не знали этого поля: без запасного значения (0)
            # условие «новый экстремум для долива» не выполнилось бы никогда,
            # и восстановленный цикл молча висел бы с одной позицией
            extreme_at_last_entry=(raw.get("extreme_at_last_entry")
                                   or (raw.get("swing_low") if raw.get("side", "long") == "long"
                                       else raw.get("swing_high")) or 0.0),
            max_positions_used=raw.get("max_positions_used", len(positions) or 1),
        )

# ------------------------------------------------------------------ диагностика
def scan_signals(candles: Sequence[Candle], settings, *, sample: int = 5) -> Dict[str, Any]:
    """Сколько сигналов дала бы стратегия на этих свечах и какие были движения.

    Нужен, чтобы честно ответить на вопрос «почему бот молчит»: показывает размер
    движений в окне настроек против порога и самые близкие к входу случаи.
    """
    automatic_swing = str(getattr(settings, "entry_mode", "swing") or "swing").lower() != "impulse"
    # For swing diagnostics use a stable 3-candle sample only to describe local
    # volatility; it is not a trading gate. Impulse mode analyzes its configured N.
    n = 3 if automatic_swing else int(settings.candles_count)
    total = len(candles)
    if total <= n:
        return {"candles": total, "windows": 0, "signals": 0, "enough_data": False,
                "reason": f"мало свечей для сканера: {total} из {n}"}

    moves: List[float] = []
    signals: List[Dict[str, Any]] = []
    near: List[Dict[str, Any]] = []
    last_reason = ""
    for i in range(n, total + 1):
        window = list(candles[i - n:i])
        high = max(c.high for c in window)
        low = min(c.low for c in window)
        if low <= 0 or high <= low:
            continue
        if settings.move_measure == "range":
            move_pct = (high - low) / low * 100.0
        else:
            move_pct = abs(window[-1].close - window[0].open) / window[0].open * 100.0
        moves.append(move_pct)
        impulse, reason = detect_signal(candles[:i], settings)
        last_reason = reason
        if impulse is not None:
            signals.append({"ts": window[-1].ts, "side": impulse.side,
                            "move_pct": round(impulse.move_pct, 3),
                            "target": impulse.target, "entry": impulse.entry_price})
        elif move_pct >= settings.min_move_pct * 0.8:
            near.append({"ts": window[-1].ts, "move_pct": round(move_pct, 3),
                         "reason": reason, "side": "down" if _is_down(window, settings.candle_direction_mode)
                         else ("up" if _is_up(window, settings.candle_direction_mode) else "—")})

    moves_sorted = sorted(moves)
    def quantile(q: float) -> float:
        if not moves_sorted:
            return 0.0
        idx = min(len(moves_sorted) - 1, max(0, int(round(q * (len(moves_sorted) - 1)))))
        return moves_sorted[idx]

    near.sort(key=lambda item: -item["move_pct"])
    tf_min = max(1, int(settings.tf_seconds() / 60)) * max(0, total - n)
    median = quantile(0.5)
    max_move = max(moves) if moves else 0.0
    #: что предложить вместо текущего порога: чуть ниже порога, но не выше
    #: того, что рынок реально давал в этом окне (иначе снова тишина)
    suggested = max(0.05, min(settings.min_move_pct * 0.6, max_move * 0.8,
                              max(median * 2.0, 0.1)))
    return {
        "candles": total,
        "windows": len(moves),
        "signals": len(signals),
        "signal_list": signals[-sample:],
        "near": near[:sample],
        "enough_data": True,
        "last_reason": last_reason,
        "threshold_pct": settings.min_move_pct,
        "min_profit_pct": settings.min_profit_pct,
        "suggested_threshold_pct": round(suggested, 2),
        "move_median_pct": round(median, 3),
        "move_p90_pct": round(quantile(0.9), 3),
        "move_max_pct": round(max_move, 3),
        "candles_count": None if automatic_swing else n,
        "window_candles": n,
        "auto_count": automatic_swing,
        "timeframe": settings.timeframe,
        "symbol": settings.symbol,
        "minutes": tf_min,
        "direction": settings.direction,
    }
