"""The strategy engine.

It is a pure, deterministic, exchange-agnostic state machine:

    engine.on_candle(closed_candle) -> [Action, ...]
    engine.on_tick(last_price)      -> [Action, ...]
    engine.apply_fill(action, fill)   # result of an executed Action

The exact same code is used live, in paper mode and by the backtester, which
guarantees that a backtest reproduces what the bot does in production.

Algorithm (long side; the short side is the mirror image)
---------------------------------------------------------
1. Signal: `candles_count` candles in one direction (for a long: bearish
   candles) whose high-low range >= `min_move_pct`.
   The swing is `swing_high` = max high, `swing_low` = min low of those candles
   and the fibonacci grid is stretched from `swing_low` (0%) to `swing_high`
   (100%).
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
from .fib import detect_impulse, fib_level
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
    def on_tick(self, price: float) -> List[Action]:
        """Price update between candles - used to react to the target quickly."""
        if price <= 0:
            return []
        self.last_price = price
        if self.stopped or self.cycle is None:
            return []
        return self._exit_checks(self.cycle, high=price, low=price, open_price=price, ts=now_ms())

    def on_candle(self, candle: Candle) -> List[Action]:
        """Process a single *closed* candle."""
        self.push_candle(candle)
        if self.stopped:
            return []

        if self.cycle is None:
            impulse, reason = detect_impulse(self.candles, self.settings)
            self.last_check = reason
            if impulse is None:
                return []
            self.last_check = reason
            self.signals_found += 1
            return [self._open_cycle(impulse, candle)]

        return self._manage(self.cycle, candle)

    # ------------------------------------------------------------- cycle logic
    def _open_cycle(self, impulse, candle: Candle) -> Action:
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
            extreme_at_last_entry=impulse.low if impulse.side == "long" else impulse.high,
            note=(f"{impulse.candles} свечей {impulse.move_side} {impulse.move_pct:.2f}% "
                  f"[{impulse.low:g}..{impulse.high:g}]"),
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
            ts=candle.ts,
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
        cycle.target = fib_level(cycle.swing_low, cycle.swing_high, cycle.goal_pct)
        cycle.max_positions_used = max(cycle.max_positions_used, len(cycle.positions))

        # 2. target / stop-loss -------------------------------------------------
        actions = self._exit_checks(cycle, high=candle.high, low=candle.low,
                                    open_price=candle.open, ts=candle.ts)
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
                self.last_check = (f"долив #{idx}: профит до цели {profit_pct:.2f}% "
                                   f"<= {s.min_profit_pct:g}%, средняя {avg:g}")
                return [Action(
                    kind="add",
                    side=cycle.side,
                    ref_price=candle.close,
                    volume_usd=volume,
                    position_idx=idx,
                    target=cycle.target,
                    reason=(f"профит до цели {profit_pct:.2f}% <= минимума "
                            f"{s.min_profit_pct:g}% (средняя {avg:g})"),
                    ts=candle.ts,
                )]
            if profit_pct <= s.min_profit_pct + 1e-9 and not guard_ok:
                self.last_check = "ждём новый экстремум для долива"
            else:
                self.last_check = (f"в позиции, профит до цели {profit_pct:.2f}% "
                                   f"(мин. {s.min_profit_pct:g}%)")
        elif cycle.positions:
            self.last_check = f"лимит позиций {s.max_positions} достигнут"
        return []

    def _exit_checks(self, cycle: Cycle, *, high: float, low: float,
                     open_price: float, ts: int) -> List[Action]:
        s = self.settings
        if not cycle.positions:
            return []
        avg = cycle.avg_entry
        target = cycle.target
        maxed = len(cycle.open_positions) >= s.max_positions
        profit_pct = cycle.profit_pct_to(target)
        armed = True
        if s.tp_requires_min_profit:
            armed = profit_pct >= s.min_profit_pct - 1e-9
        if maxed and s.close_at_target_when_maxed:
            armed = True
        profitable = (target > avg) if cycle.side == "long" else (target < avg)

        # emergency stop of the cycle
        if s.cycle_stop_loss_pct > 0:
            loss_pct = -cycle.profit_pct_to(cycle.last_entry_price if cycle.side == "long" else
                                            cycle.last_entry_price)
            price_for_sl = low if cycle.side == "long" else high
            adverse = ((avg - price_for_sl) / avg * 100.0) if cycle.side == "long" else \
                      ((price_for_sl - avg) / avg * 100.0)
            if adverse >= s.cycle_stop_loss_pct:
                return [Action(kind="close_all", side=cycle.side, ref_price=price_for_sl,
                               qty=cycle.total_qty, reason="стоп-лосс", ts=ts)]

        if armed and profitable:
            hit = (high >= target) if cycle.side == "long" else (low <= target)
            if hit:
                ref = max(target, open_price) if cycle.side == "long" else min(target, open_price)
                return [Action(kind="close_all", side=cycle.side, ref_price=ref,
                               qty=cycle.total_qty,
                               reason=f"цель по фибо {cycle.goal_pct:g}% = {target:g}", ts=ts)]
        return []

    # ------------------------------------------------------------------- fills
    def apply_fill(self, action: Action, fill: Fill) -> Position:
        cycle = self.cycle
        if cycle is None:
            raise RuntimeError("нет активного цикла")
        if action.kind == "entry":
            cycle.positions = []
            cycle.opened_ts = fill.ts or now_ms()
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
        cycle.target = fib_level(cycle.swing_low, cycle.swing_high, cycle.goal_pct)
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
            goal_pct=raw.get("goal_pct", self.settings.fib_goal_pct),
            target=raw.get("target", 0.0),
            positions=positions,
            note=raw.get("note", ""),
            extreme_at_last_entry=raw.get("extreme_at_last_entry", 0.0),
            max_positions_used=raw.get("max_positions_used", len(positions) or 1),
        )

# ------------------------------------------------------------------ диагностика
def scan_signals(candles: Sequence[Candle], settings, *, sample: int = 5) -> Dict[str, Any]:
    """Сколько сигналов дала бы стратегия на этих свечах и какие были движения.

    Нужен, чтобы честно ответить на вопрос «почему бот молчит»: показывает размер
    движений в окне настроек против порога и самые близкие к входу случаи.
    """
    n = int(settings.candles_count)
    total = len(candles)
    if total <= n:
        return {"candles": total, "windows": 0, "signals": 0, "enough_data": False,
                "reason": f"мало свечей: {total} из {n}"}

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
        impulse, reason = detect_impulse(candles[:i], settings)
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
    return {
        "candles": total,
        "windows": len(moves),
        "signals": len(signals),
        "signal_list": signals[-sample:],
        "near": near[:sample],
        "enough_data": True,
        "last_reason": last_reason,
        "threshold_pct": settings.min_move_pct,
        "move_median_pct": round(quantile(0.5), 3),
        "move_p90_pct": round(quantile(0.9), 3),
        "move_max_pct": round(max(moves) if moves else 0.0, 3),
        "candles_count": n,
        "timeframe": settings.timeframe,
        "symbol": settings.symbol,
        "minutes": tf_min,
        "direction": settings.direction,
    }
