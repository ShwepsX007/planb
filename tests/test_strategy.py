"""The core of the project: fibonacci grid + averaging logic.

Every scenario below is hand-computed so that the numbers can be verified
against the description of the strategy (and the reference screenshots).
"""
from __future__ import annotations

import pytest

from app.config import BotSettings
from app.fib import Impulse, detect_impulse, fib_level, fib_grid
from app.models import Candle, Fill
from app.strategy import StrategyEngine


def settings(**kwargs) -> BotSettings:
    base = dict(
        symbol="BTC/USDT", market_type="futures", timeframe="5m",
        entry_mode="impulse",          # эти сценарии проверяют старый метод N свечей
        candles_count=3, min_move_pct=1.0, fib_goal_pct=50.0, min_profit_pct=1.0,
        base_volume_usd=10.0, volume_multiplier=2.0, multiplier_start_position=3,
        max_positions=6, direction="long", fee_pct=0.0, slippage_pct=0.0,
        total_profit_target_usd=0.0, cycle_stop_loss_pct=0.0,
    )
    base.update(kwargs)
    return BotSettings(**base)


def candle(ts, o, h, l, c) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=1.0)


def down_impulse(ts0=0, high=100.2, low=95.0, closes=(99.0, 97.0, 95.5)):
    """Three bearish candles: the swing is [low, high]."""
    opens = (high, closes[0], closes[1])
    out = []
    for i, cl in enumerate(closes):
        o = opens[i]
        h = max(high if i == 0 else o + 0.05, o, cl)
        l = min(low if i == len(closes) - 1 else cl - 0.05, o, cl)
        out.append(candle(ts0 + i * 300_000, o, h, l, cl))
    return out


def feed_falling(engine, ts, price, step=0.03, max_iter=800, until="add"):
    """Drop the price candle by candle until the engine emits ``until``."""
    for _ in range(max_iter):
        price -= step
        ts += 300_000
        acts = engine.on_candle(candle(ts, price + step, price + step + 0.01, price, price))
        if acts and acts[0].kind == until:
            return acts[0], ts, price
    raise AssertionError(f"движение вниз не привело к действию {until}")


def open_cycle(engine, candles, price=None):
    for c in candles[:-1]:
        engine.push_candle(c)
    acts = engine.on_candle(candles[-1])
    assert acts and acts[0].kind == "entry"
    entry = acts[0]
    price = price if price is not None else candles[-1].close
    engine.apply_fill(entry, Fill(price=price, qty=entry.volume_usd / price, fee=0.0,
                                 ts=candles[-1].ts))
    return entry


# ---------------------------------------------------------- intrabar execution
def test_signal_can_open_before_the_live_candle_closes():
    """The third bearish candle is still forming, but its % move already qualifies."""
    candles = down_impulse()
    engine = StrategyEngine(settings())
    engine.set_history(candles[:-1])

    action = engine.on_tick(candles[-1].close, candles[-1])

    assert action and action[0].kind == "entry"
    assert action[0].ref_price == pytest.approx(candles[-1].close)


def test_average_add_can_trigger_on_a_price_tick_before_candle_close():
    engine = StrategyEngine(settings(min_profit_pct=1.0))
    candles = down_impulse()
    open_cycle(engine, candles)

    live = candle(candles[-1].ts + 300_000, 95.5, 95.5, 90.0, 90.0)
    actions = engine.on_tick(90.0, live)

    assert actions and actions[0].kind == "add"
    assert actions[0].ref_price == pytest.approx(90.0)
    assert engine.cycle.swing_low == pytest.approx(90.0)
    assert engine.cycle.target == pytest.approx(95.1)


# --------------------------------------------------------------------- basics
def test_volume_schedule_defaults():
    assert settings().volume_schedule(6) == [10.0, 10.0, 20.0, 40.0, 80.0, 160.0]


def test_volume_schedule_custom_start():
    s = settings(volume_multiplier=3.0, multiplier_start_position=2, base_volume_usd=5.0)
    assert s.volume_schedule(4) == [5.0, 15.0, 45.0, 135.0]


def test_fib_level_math():
    assert fib_level(95, 100, 0) == 95
    assert fib_level(95, 100, 100) == 100
    assert fib_level(95, 100, 50) == pytest.approx(97.5)
    assert fib_level(95, 100, 61.8) == pytest.approx(98.09)
    levels = {lv["level"]: lv["price"] for lv in fib_grid(95, 100)}
    assert levels[0.236] == pytest.approx(96.18)


def test_impulse_detection_and_rejections():
    impulse, reason = detect_impulse(down_impulse(), settings())
    assert impulse is not None, reason
    assert impulse.side == "long"
    assert impulse.high == pytest.approx(100.2)
    assert impulse.low == pytest.approx(95.0)
    assert impulse.target == pytest.approx(97.6)
    assert "сигнал" in reason

    small = down_impulse(high=100.0, low=99.5, closes=(99.9, 99.7, 99.6))
    impulse2, reason2 = detect_impulse(small, settings(min_move_pct=5.0))
    assert impulse2 is None and "порога" in reason2

    up = [candle(c.ts, c.close, c.high, c.low, c.open) for c in down_impulse()]
    impulse3, reason3 = detect_impulse(up, settings(direction="long"))
    assert impulse3 is None and "отключено" in reason3

    # target profit below the minimum -> no entry
    impulse4, reason4 = detect_impulse(down_impulse(), settings(min_profit_pct=10.0))
    assert impulse4 is None and "минимального" in reason4


# --------------------------------------------------------------- main scenario
def test_full_cycle_with_averaging():
    """Signal -> entry -> new lows -> долив -> target hit -> profit."""
    s = settings()
    engine = StrategyEngine(s)
    candles = down_impulse()
    entry = open_cycle(engine, candles)
    assert entry.volume_usd == 10.0

    cycle = engine.cycle
    assert cycle.side == "long"
    assert cycle.swing_low == pytest.approx(95.0)
    assert cycle.swing_high == pytest.approx(100.2)
    assert cycle.target == pytest.approx(97.6)          # 50 % of the grid
    assert cycle.avg_entry == pytest.approx(95.5)
    assert cycle.profit_pct_to(cycle.target) == pytest.approx(2.199, abs=0.01)

    # price keeps falling: the grid is stretched, the target moves down
    engine.on_candle(candle(candles[-1].ts + 300_000, 95.5, 95.6, 94.0, 94.2))
    cycle = engine.cycle
    assert cycle.swing_low == pytest.approx(94.0)
    assert cycle.target == pytest.approx(fib_level(94.0, 100.2, 50.0))
    assert cycle.profit_pct_to(cycle.target) < 2.199

    # keep falling until the profit to the target drops to the minimum -> долив
    add, ts, price = feed_falling(engine, candles[-1].ts + 300_000, 94.2)
    assert add.kind == "add" and add.position_idx == 2
    assert add.volume_usd == 10.0                       # 2-я позиция базовым объёмом
    assert engine.cycle.profit_pct_to(engine.cycle.target) <= s.min_profit_pct + 1e-9
    engine.apply_fill(add, Fill(price=price, qty=add.volume_usd / price, ts=ts))

    cycle = engine.cycle
    assert len(cycle.positions) == 2
    # volume weighted average: qty_i = volume_i / price_i
    expected_avg = (10.0 + 10.0) / (10.0 / 95.5 + 10.0 / price)
    assert cycle.avg_entry == pytest.approx(expected_avg, rel=1e-9)
    assert cycle.avg_entry < cycle.target               # средняя ушла ниже цели
    assert cycle.profit_pct_to(cycle.target) >= s.min_profit_pct - 1e-9

    # price reaches the target -> the whole cycle is closed in profit
    target = cycle.target
    acts = engine.on_candle(candle(ts + 300_000, price, target + 0.5, price, target + 0.2))
    assert acts and acts[0].kind == "close_all"
    closed = engine.close_cycle(acts[0].ref_price, ts + 300_000, acts[0].reason)
    assert closed.pnl > 0
    assert engine.cycle is None
    assert engine.cycles_total == 1 and engine.wins == 1
    assert closed.pnl == pytest.approx(engine.realized_pnl)
    assert closed.pnl / closed.invested_volume_usd * 100 >= s.min_profit_pct - 1e-6


def test_target_moves_towards_average_then_average_moves_below_target():
    """The three positions of the screenshots: 1, 2 and 3 x2."""
    s = settings(min_profit_pct=1.0, max_positions=6)
    engine = StrategyEngine(s)
    candles = down_impulse()
    open_cycle(engine, candles)
    ts = candles[-1].ts
    price = candles[-1].close

    levels = []
    for expected_idx in (2, 3, 4):
        add, ts, price = feed_falling(engine, ts, price, step=0.02)
        assert add.position_idx == expected_idx
        engine.apply_fill(add, Fill(price=price, qty=add.volume_usd / price, ts=ts))
        cycle = engine.cycle
        levels.append({
            "idx": expected_idx,
            "price": price,
            "volume": add.volume_usd,
            "avg": cycle.avg_entry,
            "target": cycle.target,
            "profit_pct": cycle.profit_pct_to(cycle.target),
        })

    volumes = [lv["volume"] for lv in levels]
    assert volumes == [10.0, 20.0, 40.0]                 # удвоение с 3-й позиции

    cycle = engine.cycle
    # every averaging step lowered the average entry and the target followed it down
    assert levels[0]["avg"] > levels[1]["avg"] > levels[2]["avg"]
    assert levels[0]["target"] > levels[1]["target"] > levels[2]["target"]
    # the target always stays above the average entry (that is where we take profit)
    assert cycle.target > cycle.avg_entry
    assert cycle.profit_pct_to(cycle.target) >= s.min_profit_pct
    # every position lowered the average, so the exit price (target) keeps falling too
    assert levels[0]["avg"] > levels[-1]["avg"]


def test_no_duplicate_add_at_same_extreme():
    s = settings(min_profit_pct=1.0)
    engine = StrategyEngine(s)
    candles = down_impulse()
    open_cycle(engine, candles)
    add, ts, price = feed_falling(engine, candles[-1].ts, candles[-1].close)
    engine.apply_fill(add, Fill(price=price, qty=add.volume_usd / price, ts=ts))
    counts = len(engine.cycle.positions)
    acts = engine.on_candle(candle(ts + 300_000, price, price + 0.01, price, price))
    assert not [a for a in acts if a.kind == "add"]
    assert len(engine.cycle.positions) == counts


def feed_until_profit(engine, ts, price, pct, step=0.02, max_iter=800):
    """Fall until the profit from the average entry to the target drops to ``pct``."""
    for _ in range(max_iter):
        price -= step
        ts += 300_000
        engine.on_candle(candle(ts, price + step, price + step + 0.01, price, price))
        assert engine.cycle is not None
        if engine.cycle.profit_pct_to(engine.cycle.target) <= pct:
            return ts, price
    raise AssertionError("цена не дошла до нужного профита")


def test_limit_of_positions_and_close_at_target():
    s = settings(max_positions=3, min_profit_pct=1.0, close_at_target_when_maxed=True)
    engine = StrategyEngine(s)
    candles = down_impulse()
    open_cycle(engine, candles)
    ts = candles[-1].ts
    price = candles[-1].close
    for _ in range(2):
        add, ts, price = feed_falling(engine, ts, price, step=0.02)
        engine.apply_fill(add, Fill(price=price, qty=add.volume_usd / price, ts=ts))
    assert len(engine.cycle.positions) == 3

    # with the position limit reached the bot keeps watching the grid:
    # the target moves down with every new low until only ~0.3% is left
    ts, price = feed_until_profit(engine, ts, price, 0.3)
    assert len(engine.cycle.positions) == 3
    assert engine.cycle.profit_pct_to(engine.cycle.target) < s.min_profit_pct

    # more falling - no more positions are opened (the limit is reached)
    for _ in range(20):
        price -= 0.02
        ts += 300_000
        engine.on_candle(candle(ts, price, price + 0.01, price, price))
    assert len(engine.cycle.positions) == 3

    # price returns to the target -> the cycle is closed in profit even though
    # the profit is below the minimum (otherwise the bot would be stuck)
    target = engine.cycle.target
    assert target > engine.cycle.avg_entry
    acts = engine.on_candle(candle(ts + 300_000, price, target + 1, price, target))
    assert acts and acts[0].kind == "close_all"
    closed = engine.close_cycle(acts[0].ref_price, ts + 300_000, acts[0].reason)
    assert closed.pnl > 0


def test_short_side_mirror():
    s = settings(direction="both")
    engine = StrategyEngine(s)
    up = [candle(0, 100.0, 101.0, 99.9, 100.9),
          candle(300_000, 100.9, 103.0, 100.8, 102.9),
          candle(600_000, 102.9, 105.0, 102.8, 104.9)]
    for c in up[:-1]:
        engine.push_candle(c)
    acts = engine.on_candle(up[-1])
    assert acts and acts[0].kind == "entry" and acts[0].side == "short"
    engine.apply_fill(acts[0], Fill(price=104.9, qty=acts[0].volume_usd / 104.9, ts=600_000))
    cycle = engine.cycle
    assert cycle.target == pytest.approx(fib_level(99.9, 105.0, 50.0))
    assert cycle.target < cycle.avg_entry
    previous_target = cycle.target
    # a new high stretches the grid upwards -> the target climbs closer to the entry
    engine.on_candle(candle(900_000, 104.9, 106.0, 104.5, 105.5))
    assert engine.cycle.swing_high == pytest.approx(106.0)
    assert engine.cycle.target == pytest.approx(fib_level(99.9, 106.0, 50.0))
    assert engine.cycle.target > previous_target


def test_stop_when_total_profit_reached():
    s = settings(total_profit_target_usd=2.0, base_volume_usd=100.0)
    engine = StrategyEngine(s)
    candles = down_impulse()
    open_cycle(engine, candles)
    target = engine.cycle.target
    acts = engine.on_candle(candle(600_001, 95.5, target + 1, 95.5, target))
    assert acts and acts[0].kind == "close_all"
    engine.close_cycle(acts[0].ref_price, acts[0].ts, acts[0].reason)
    assert engine.realized_pnl >= 2.0
    assert engine.stopped and "профит" in engine.stop_reason
    # a stopped engine does not open new cycles
    assert engine.on_candle(candle(900_000, target, target + 1, target - 1, target - 0.5)) == []


def test_stop_loss_closes_cycle():
    s = settings(cycle_stop_loss_pct=2.0)
    engine = StrategyEngine(s)
    candles = down_impulse()
    open_cycle(engine, candles)
    acts = engine.on_candle(candle(600_001, 95.5, 95.6, 93.0, 93.5))   # −2.6 %
    assert acts and acts[0].kind == "close_all" and "стоп" in acts[0].reason


def test_paused_engine_ignores_signals_until_resumed():
    s = settings()
    engine = StrategyEngine(s)
    candles = down_impulse()
    entry = open_cycle(engine, candles)
    engine.stop("тест")
    assert engine.on_candle(candle(600_001, 95.0, 96.0, 94.0, 95.0)) == []
    engine.resume()
    assert engine.cycle is not None        # the open cycle survives a resume


def test_state_snapshot_roundtrip():
    s = settings()
    engine = StrategyEngine(s)
    candles = down_impulse()
    open_cycle(engine, candles)
    snap = engine.snapshot()
    other = StrategyEngine(s)
    other.restore(snap["cycle_raw"])
    assert other.cycle is not None
    assert other.cycle.avg_entry == pytest.approx(95.5)
    assert other.cycle.target == pytest.approx(engine.cycle.target)
    assert other.cycle.side == "long"
    assert other.cycle.positions[0].qty == pytest.approx(engine.cycle.positions[0].qty)


def test_opposite_cycles_are_independent(monkeypatch):
    import app.strategy as strategy_module

    s = settings(direction="both", market_type="futures", trade_mode="paper",
                 allow_opposite_cycles=True)

    def both_signals(candles, side_settings):
        side = side_settings.direction
        return Impulse(
            side=side, move_side="down" if side == "long" else "up",
            high=105.0, low=95.0, entry_price=100.0, target=100.0,
            move_pct=10.0, candles=3, start_ts=1, end_ts=2,
        ), "signal"

    monkeypatch.setattr(strategy_module, "detect_signal", both_signals)
    engine = StrategyEngine(s)
    actions = engine.on_candle(candle(300_000, 100, 101, 99, 100))
    assert [action.side for action in actions] == ["long", "short"]
    assert len({action.cycle_id for action in actions}) == 2

    for action in actions:
        engine.apply_fill(action, Fill(price=100.0, qty=1.0, ts=action.ts))
    assert {cycle.side for cycle in engine.active_cycles} == {"long", "short"}

    short = engine.get_cycle(side="short")
    engine.close_cycle(90.0, 400_000, "test short", cycle_id=short.id)
    assert [cycle.side for cycle in engine.active_cycles] == ["long"]
    assert engine.realized_pnl == pytest.approx(10.0)
    engine.close_cycle(110.0, 500_000, "test long")
    assert not engine.active_cycles
    assert engine.realized_pnl == pytest.approx(20.0)
