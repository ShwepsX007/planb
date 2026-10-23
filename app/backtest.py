"""Historical replay of the very same strategy engine (see ``strategy.py``)."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence

from .config import BotSettings
from .models import Action, Candle, Fill
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


def _track_backtest_exposure(engine: StrategyEngine, price: float, initial_balance: float,
                             settings: BotSettings) -> None:
    cycles = engine.active_cycles
    if not cycles or price <= 0:
        return
    leverage = max(1.0, float(settings.leverage or 1.0))
    equity = initial_balance + engine.realized_pnl + sum(c.unrealized_pnl(price) for c in cycles)
    for cycle in cycles:
        notional = sum(p.qty * price for p in cycle.open_positions)
        margin = notional / leverage if settings.market_type == "futures" else notional
        cycle.peak_notional_usd = max(cycle.peak_notional_usd, notional)
        cycle.peak_margin_usd = max(cycle.peak_margin_usd, margin)
        if equity > 0:
            cycle.max_deposit_load_pct = max(cycle.max_deposit_load_pct, margin / equity * 100.0)


def _cycle_payload(cycle) -> Dict[str, Any]:
    return {
        "id": cycle.id, "side": cycle.side, "opened_ts": cycle.opened_ts,
        "closed_ts": cycle.closed_ts,
        "duration_ms": max(0, (cycle.closed_ts or cycle.opened_ts) - cycle.opened_ts),
        "positions": len(cycle.positions),
        "avg_entry": round(cycle.avg_entry, 8),
        "close_price": round(cycle.close_price or 0, 8),
        "volume_usd": round(cycle.invested_volume_usd, 6),
        "pnl": round(cycle.pnl, 8), "pnl_pct": round(cycle.pnl_pct, 4),
        "fees": round(cycle.fees, 8), "reason": cycle.close_reason,
        "target": round(cycle.target, 8), "swing_high": round(cycle.swing_high, 8),
        "swing_low": round(cycle.swing_low, 8),
        "max_adverse_pnl_usd": round(cycle.max_adverse_pnl_usd, 8),
        "max_adverse_pnl_pct": round(cycle.max_adverse_pnl_pct, 4),
        "peak_notional_usd": round(cycle.peak_notional_usd, 6),
        "peak_margin_usd": round(cycle.peak_margin_usd, 6),
        "max_deposit_load_pct": round(cycle.max_deposit_load_pct, 4),
        "position_details": [{
            "cycle_id": cycle.id, "side": cycle.side, "idx": p.idx,
            "ts": p.ts, "close_ts": p.close_ts, "duration_ms": max(0, (p.close_ts or p.ts) - p.ts),
            "price": round(p.price, 8), "close_price": round(p.close_price or 0, 8),
            "qty": round(p.qty, 10), "volume_usd": round(p.volume_usd, 6),
            "close_volume_usd": round(p.qty * (p.close_price or 0), 6),
            "multiplier": p.multiplier, "fee": round(p.fee, 8),
            "close_fee": round(p.close_fee, 8), "fees": round(p.fee + p.close_fee, 8),
            "pnl": round(p.pnl, 8),
            "pnl_pct": round(p.pnl / p.volume_usd * 100, 4) if p.volume_usd else 0.0,
        } for p in cycle.positions],
    }


def run_backtest(candles: Sequence[Candle], settings: BotSettings, *,
                 initial_balance: Optional[float] = None,
                 equity_points: int = 600) -> Dict[str, Any]:
    """Run the strategy over ``candles`` and return statistics + trades."""
    if not candles:
        return {"error": "нет свечей"}
    balance = float(initial_balance if initial_balance is not None else settings.paper_balance_usd)
    engine = StrategyEngine(settings)
    warmup = max(settings.candles_count if settings.entry_mode == "impulse" else 2, 5)

    equity_curve: List[Dict[str, float]] = []
    trades: List[Dict[str, Any]] = []
    depth_series: List[int] = []
    peak = balance
    max_dd = 0.0
    max_dd_pct = 0.0
    peak_notional = 0.0
    max_margin_used = 0.0
    max_deposit_load_pct = 0.0
    cycle_results: List[Dict[str, Any]] = []
    margin_rejections = 0
    busted = False
    bust_ts: Optional[int] = None

    for i, candle in enumerate(candles):
        if i < warmup:
            engine.push_candle(candle)
            continue

        actions = engine.on_candle(candle)
        # Capture exposure before any action closes or adds to cycles.
        _track_backtest_exposure(engine, candle.close, balance, settings)
        for action in actions:
            if action.kind in ("entry", "add"):
                leverage = max(1.0, float(settings.leverage or 1.0))
                required_margin = (action.volume_usd / leverage
                                   if settings.market_type == "futures" else action.volume_usd)
                entry_fee_reserve = action.volume_usd * settings.fee_pct / 100.0
                open_margin = sum(
                    sum(p.qty * candle.close for p in c.open_positions) / leverage
                    if settings.market_type == "futures"
                    else sum(p.qty * candle.close for p in c.open_positions)
                    for c in engine.active_cycles)
                open_unrealized = sum(c.unrealized_pnl(candle.close) for c in engine.active_cycles)
                available_equity = balance + engine.realized_pnl + open_unrealized - open_margin
                if required_margin + entry_fee_reserve > max(0.0, available_equity) + 1e-9:
                    margin_rejections += 1
                    if action.kind == "entry" and action.cycle_id:
                        engine.discard_empty_cycle(action.cycle_id)
                    trades.append({"ts": candle.ts, "kind": "rejected", "side": action.side,
                                   "idx": action.position_idx, "price": round(candle.close, 8),
                                   "volume_usd": round(action.volume_usd, 6), "reason": "недостаточно свободной маржи",
                                   "cycle_id": action.cycle_id})
                    continue
                fill = _fill(action, settings, closing=False)
                engine.apply_fill(action, fill)
                trades.append({
                    "ts": candle.ts, "kind": action.kind, "side": action.side,
                    "idx": action.position_idx, "price": round(fill.price, 8),
                    "qty": round(fill.qty, 10), "volume_usd": round(fill.qty * fill.price, 6),
                    "fee": round(fill.fee, 8), "reason": action.reason,
                    "target": round(action.target, 8), "cycle_id": action.cycle_id,
                })
            elif action.kind == "close_all":
                cycle = engine.get_cycle(action.cycle_id, side=action.side)
                if cycle is None:
                    continue
                fill = _fill(action, settings, closing=True)
                cycle = engine.close_cycle(fill.price, candle.ts, action.reason, fee=fill.fee,
                                           cycle_id=cycle.id)
                trades.append({
                    "ts": candle.ts, "kind": "close_all", "side": action.side, "idx": 0,
                    "price": round(fill.price, 8), "qty": round(fill.qty, 10),
                    "volume_usd": round(fill.qty * fill.price, 6), "fee": round(fill.fee, 8),
                    "reason": action.reason, "target": round(action.target or cycle.target, 8),
                    "cycle_pnl": round(cycle.pnl, 8), "cycle_id": cycle.id,
                })
                cycle_results.append(_cycle_payload(cycle))

        _track_backtest_exposure(engine, candle.close, balance, settings)
        # Equity is marked to the candle close. Drawdown is close-to-close equity
        # (realized + unrealized, including fees), over all open cycles.
        unreal = sum(c.unrealized_pnl(candle.close) for c in engine.active_cycles)
        equity = balance + engine.realized_pnl + unreal
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0.0
        if engine.active_cycles:
            depth_series.append(sum(len(c.positions) for c in engine.active_cycles))
        notional = sum(p.qty * candle.close for c in engine.active_cycles for p in c.open_positions)
        margin = notional / max(1.0, float(settings.leverage or 1.0)) \
            if settings.market_type == "futures" else notional
        peak_notional = max(peak_notional, notional)
        max_margin_used = max(max_margin_used, margin)
        deposit_load = margin / equity * 100.0 if equity > 0 else 0.0
        max_deposit_load_pct = max(max_deposit_load_pct, deposit_load)
        equity_curve.append({"ts": candle.ts, "equity": round(equity, 6),
                             "realized": round(engine.realized_pnl, 6),
                             "unrealized": round(unreal, 6), "price": candle.close,
                             "peak_equity": round(peak, 6), "drawdown_usd": round(dd, 6),
                             "drawdown_pct": round(dd / peak * 100 if peak else 0.0, 4),
                             "notional_exposure": round(notional, 6),
                             "margin_used_estimate": round(margin, 6),
                             "deposit_load_pct": round(deposit_load, 4)})
        if equity <= 0:
            busted = True
            bust_ts = candle.ts
            for active in list(engine.active_cycles):
                action = Action(kind="close_all", side=active.side, ref_price=candle.close,
                                qty=active.total_qty, reason="слив: equity <= 0",
                                ts=candle.ts, cycle_id=active.id)
                fill = _fill(action, settings, closing=True)
                closed = engine.close_cycle(fill.price, candle.ts, action.reason,
                                            fee=fill.fee, cycle_id=active.id)
                cycle_results.append(_cycle_payload(closed))
            equity = balance + engine.realized_pnl
            peak = max(peak, equity)
            dd = max(0.0, peak - equity)
            max_dd = max(max_dd, dd)
            max_dd_pct = max(max_dd_pct, dd / peak * 100 if peak else 0.0)
            equity_curve[-1].update({"equity": round(equity, 6),
                                     "realized": round(engine.realized_pnl, 6),
                                     "unrealized": 0.0, "peak_equity": round(peak, 6),
                                     "drawdown_usd": round(dd, 6),
                                     "drawdown_pct": round(dd / peak * 100 if peak else 0.0, 4),
                                     "notional_exposure": 0.0,
                                     "margin_used_estimate": 0.0, "deposit_load_pct": 0.0})
            engine.stop("слив: equity опустился до нуля или ниже")
            break

    # Close dangling cycles at the last price so the statistics add up.
    if engine.active_cycles and candles and not busted:
        last = candles[-1]
        for active in list(engine.active_cycles):
            qty = active.total_qty
            fill = Fill(price=last.close, qty=qty,
                        fee=qty * last.close * settings.fee_pct / 100.0,
                        ts=last.ts, order_id="sim")
            cycle = engine.close_cycle(last.close, last.ts, "конец бэктеста", fee=fill.fee,
                                       cycle_id=active.id)
            cycle_results.append(_cycle_payload(cycle))
        unreal = 0.0
        equity = balance + engine.realized_pnl
        peak = max(peak, equity)
        dd = max(0.0, peak - equity)
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd / peak * 100 if peak else 0.0)
        equity_curve.append({"ts": last.ts, "equity": round(equity, 6),
                             "realized": round(engine.realized_pnl, 6), "unrealized": 0.0,
                             "price": last.close, "peak_equity": round(peak, 6),
                             "drawdown_usd": round(dd, 6),
                             "drawdown_pct": round(dd / peak * 100 if peak else 0.0, 4),
                             "notional_exposure": 0.0, "margin_used_estimate": 0.0,
                             "deposit_load_pct": 0.0})

    total_pnl = engine.realized_pnl
    gross_profit = sum(max(0.0, float(c.get("pnl") or 0.0)) for c in cycle_results)
    gross_loss = sum(max(0.0, -float(c.get("pnl") or 0.0)) for c in cycle_results)
    fees_total = sum(c.get("fees", 0.0) for c in cycle_results)
    final_equity = balance + total_pnl
    step = max(1, len(equity_curve) // equity_points)
    curve = equity_curve[::step]
    if equity_curve and curve[-1] is not equity_curve[-1]:
        curve.append(equity_curve[-1])

    cycle_pnls = [float(c["pnl"]) for c in cycle_results]
    position_results = [p for cycle in cycle_results for p in cycle.get("position_details", [])]
    wins = [p for p in cycle_pnls if p >= 0]
    losses = [p for p in cycle_pnls if p < 0]
    durations = [float(c.get("duration_ms") or 0) / (settings.tf_seconds() * 1000)
                 for c in cycle_results]

    return {
        "settings": settings.model_dump(exclude={"api_key", "api_secret"}),
        "candles": len(candles),
        "from_ts": candles[0].ts,
        "to_ts": candles[-1].ts,
        "initial_balance": round(balance, 4),
        "final_equity": round(final_equity, 4),
        "leverage": max(1, int(settings.leverage)) if settings.market_type == "futures" else 1,
        "direction": settings.direction,
        "allow_opposite_cycles": settings.allow_opposite_cycles,
        "margin_rejections": margin_rejections,
        "busted": busted,
        "bust_ts": bust_ts,
        "total_pnl": round(total_pnl, 6),
        "return_pct": round(total_pnl / balance * 100, 4) if balance else 0.0,
        "cycles": len(cycle_results),
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
        "drawdown_method": "максимальная просадка close-to-close по equity (realized + unrealized, комиссии учтены)",
        "peak_notional_exposure": round(peak_notional, 6),
        "max_margin_used_estimate": round(max_margin_used, 6),
        "max_deposit_load_pct": round(max_deposit_load_pct, 4),
        "margin_method": ("оценка: номинал / плечо" if settings.market_type == "futures"
                          else "спот: полный номинал"),
        "max_positions_used": max(depth_series) if depth_series else 0,
        "avg_cycle_candles": round(statistics.fmean(durations), 2) if durations else 0.0,
        "max_cycle_candles": round(max(durations), 2) if durations else 0.0,
        "signals_found": engine.signals_found,
        "trades": trades[-500:],
        "cycles_list": cycle_results,
        "positions_list": position_results,
        "equity_curve": curve,
        "stopped": engine.stopped,
        "stop_reason": engine.stop_reason,
    }
