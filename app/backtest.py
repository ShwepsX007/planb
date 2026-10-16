"""Historical replay of the very same strategy engine (see ``strategy.py``)."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence

from .config import BotSettings
from .models import Candle, Fill
from .strategy import StrategyEngine


def _fill(action, settings: BotSettings, *, closing: bool) -> Fill:
    """Simulate a market fill: adverse slippage + taker fee."""
    slip = settings.slippage_pct / 100.0
    price = float(action.ref_price)
    adverse_up = (action.side == "long") != closing  # long entry / short exit -> price goes up
    price = price * (1 + slip) if adverse_up else price * (1 - slip)
    qty = action.qty if closing else action.volume_usd / price
    fee = abs(qty * price) * settings.fee_pct / 100.0
    return Fill(price=price, qty=qty, fee=fee, ts=action.ts, order_id="sim")


def run_backtest(candles: Sequence[Candle], settings: BotSettings, *,
                 initial_balance: Optional[float] = None,
                 equity_points: int = 600) -> Dict[str, Any]:
    """Run the strategy over ``candles`` and return statistics + trades."""
    if not candles:
        return {"error": "нет свечей"}
    balance = float(initial_balance if initial_balance is not None else settings.paper_balance_usd)
    engine = StrategyEngine(settings)
    warmup = max(settings.candles_count, 5)

    equity_curve: List[Dict[str, float]] = []
    trades: List[Dict[str, Any]] = []
    depth_series: List[int] = []
    peak = balance
    max_dd = 0.0
    max_dd_pct = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    cycle_results: List[Dict[str, Any]] = []
    closed_before = 0

    for i, candle in enumerate(candles):
        if i < warmup:
            engine.push_candle(candle)
            continue

        actions = engine.on_candle(candle)
        for action in actions:
            if action.kind in ("entry", "add"):
                fill = _fill(action, settings, closing=False)
                engine.apply_fill(action, fill)
                trades.append({
                    "ts": candle.ts, "kind": action.kind, "side": action.side,
                    "idx": action.position_idx, "price": round(fill.price, 8),
                    "qty": round(fill.qty, 10), "volume_usd": round(fill.qty * fill.price, 6),
                    "fee": round(fill.fee, 8), "reason": action.reason,
                    "target": round(action.target, 8),
                })
            elif action.kind == "close_all":
                fill = _fill(action, settings, closing=True)
                cycle = engine.close_cycle(fill.price, candle.ts, action.reason, fee=fill.fee)
                trades.append({
                    "ts": candle.ts, "kind": "close_all", "side": action.side, "idx": 0,
                    "price": round(fill.price, 8), "qty": round(fill.qty, 10),
                    "volume_usd": round(fill.qty * fill.price, 6), "fee": round(fill.fee, 8),
                    "reason": action.reason, "target": round(action.target or cycle.target, 8),
                    "cycle_pnl": round(cycle.pnl, 8), "cycle_id": cycle.id,
                })
                cycle_results.append({
                    "id": cycle.id, "side": cycle.side, "opened_ts": cycle.opened_ts,
                    "closed_ts": cycle.closed_ts, "positions": len(cycle.positions),
                    "avg_entry": round(cycle.avg_entry, 8), "close_price": round(cycle.close_price or 0, 8),
                    "volume_usd": round(cycle.invested_volume_usd, 6), "pnl": round(cycle.pnl, 8),
                    "pnl_pct": round(cycle.pnl_pct, 4), "reason": cycle.close_reason,
                    "target": round(cycle.target, 8), "swing_high": round(cycle.swing_high, 8),
                    "swing_low": round(cycle.swing_low, 8),
                })
                if cycle.pnl >= 0:
                    gross_profit += cycle.pnl
                else:
                    gross_loss += -cycle.pnl
                closed_before = len(engine.closed_cycles)

        # equity mark-to-market
        unreal = engine.cycle.unrealized_pnl(candle.close) if engine.cycle else 0.0
        equity = balance + engine.realized_pnl + unreal
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0.0
        if engine.cycle:
            depth_series.append(len(engine.cycle.positions))
        equity_curve.append({"ts": candle.ts, "equity": round(equity, 6),
                             "realized": round(engine.realized_pnl, 6),
                             "unrealized": round(unreal, 6),
                             "price": candle.close})

    # close a dangling position at the last price so the numbers add up
    if engine.cycle is not None and candles:
        last = candles[-1]
        fill = Fill(price=last.close, qty=engine.cycle.total_qty,
                    fee=engine.cycle.total_qty * last.close * settings.fee_pct / 100.0,
                    ts=last.ts, order_id="sim")
        cycle = engine.close_cycle(last.close, last.ts, "конец бэктеста", fee=fill.fee)
        cycle_results.append({
            "id": cycle.id, "side": cycle.side, "opened_ts": cycle.opened_ts,
            "closed_ts": cycle.closed_ts, "positions": len(cycle.positions),
            "avg_entry": round(cycle.avg_entry, 8), "close_price": round(last.close, 8),
            "volume_usd": round(cycle.invested_volume_usd, 6), "pnl": round(cycle.pnl, 8),
            "pnl_pct": round(cycle.pnl_pct, 4), "reason": cycle.close_reason,
            "target": round(cycle.target, 8), "swing_high": round(cycle.swing_high, 8),
            "swing_low": round(cycle.swing_low, 8),
        })
        unreal = 0.0
        equity = balance + engine.realized_pnl
        equity_curve.append({"ts": last.ts, "equity": round(equity, 6),
                             "realized": round(engine.realized_pnl, 6), "unrealized": 0.0,
                             "price": last.close})

    total_pnl = engine.realized_pnl
    fees_total = sum(c.fees for c in engine.closed_cycles)
    final_equity = balance + total_pnl
    step = max(1, len(equity_curve) // equity_points)
    curve = equity_curve[::step]
    if equity_curve and curve[-1] is not equity_curve[-1]:
        curve.append(equity_curve[-1])

    cycle_pnls = [c.pnl for c in engine.closed_cycles]
    wins = [p for p in cycle_pnls if p >= 0]
    losses = [p for p in cycle_pnls if p < 0]
    durations = [(c.closed_ts - c.opened_ts) / (settings.tf_seconds() * 1000) for c in engine.closed_cycles
                 if c.closed_ts and c.opened_ts]

    return {
        "settings": settings.model_dump(exclude={"api_key", "api_secret"}),
        "candles": len(candles),
        "from_ts": candles[0].ts,
        "to_ts": candles[-1].ts,
        "initial_balance": round(balance, 4),
        "final_equity": round(final_equity, 4),
        "total_pnl": round(total_pnl, 6),
        "return_pct": round(total_pnl / balance * 100, 4) if balance else 0.0,
        "cycles": len(engine.closed_cycles),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(cycle_pnls) * 100, 2) if cycle_pnls else 0.0,
        "avg_cycle_pnl": round(statistics.fmean(cycle_pnls), 6) if cycle_pnls else 0.0,
        "best_cycle": round(max(cycle_pnls), 6) if cycle_pnls else 0.0,
        "worst_cycle": round(min(cycle_pnls), 6) if cycle_pnls else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "fees_total": round(fees_total, 6),
        "max_drawdown": round(max_dd, 6),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "max_positions_used": max(depth_series) if depth_series else 0,
        "avg_cycle_candles": round(statistics.fmean(durations), 2) if durations else 0.0,
        "max_cycle_candles": round(max(durations), 2) if durations else 0.0,
        "signals_found": engine.signals_found,
        "trades": trades[-500:],
        "cycles_list": cycle_results[-300:],
        "equity_curve": curve,
        "stopped": engine.stopped,
        "stop_reason": engine.stop_reason,
    }
