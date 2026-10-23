"""Backtester + paper broker + end-to-end runtime tests."""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from app.backtest import run_backtest
from app.brokers import PaperBroker, make_broker
from app.config import BotSettings
from app.db import Database
from app.models import Candle
from app.feed import demo_candles


def bt_settings(**kwargs) -> BotSettings:
    base = dict(
        symbol="BTC/USDT", market_type="futures", timeframe="5m", data_source="demo",
        trade_mode="paper", candles_count=3, min_move_pct=0.5, fib_goal_pct=50.0,
        min_profit_pct=0.3, base_volume_usd=10.0, volume_multiplier=2.0,
        multiplier_start_position=3, max_positions=6, direction="both",
        fee_pct=0.05, slippage_pct=0.02, total_profit_target_usd=0.0,
        paper_balance_usd=1000.0,
    )
    base.update(kwargs)
    return BotSettings(**base)


def test_backtest_runs_over_synthetic_history():
    settings = bt_settings()
    candles = demo_candles(4000, seed=11, settings=settings)
    result = run_backtest(candles, settings)
    assert result["candles"] == 4000
    assert result["cycles"] > 0, "на синтетической истории должны быть циклы"
    assert result["cycles"] == result["wins"] + result["losses"]
    assert result["final_equity"] == pytest.approx(result["initial_balance"] + result["total_pnl"])
    assert result["max_positions_used"] <= settings.max_positions
    assert result["equity_curve"]
    assert result["trades"]
    assert result["positions_list"]
    assert result["max_drawdown"] >= 0
    assert result["max_margin_used_estimate"] >= 0
    assert result["peak_notional_exposure"] >= result["max_margin_used_estimate"]
    assert "deposit_load_pct" in result["equity_curve"][0]
    assert all("max_adverse_pnl_usd" in c and "position_details" in c
               for c in result["cycles_list"])
    assert all(p["close_ts"] and p["close_price"] for p in result["positions_list"])
    # fees are accounted for
    assert result["fees_total"] > 0
    # sum of cycle pnl equals the reported total
    assert sum(c["pnl"] for c in result["cycles_list"]) == pytest.approx(
        result["total_pnl"], rel=1e-4, abs=0.05 * max(1, result["cycles"]))


def test_backtest_is_deterministic():
    settings = bt_settings()
    candles = demo_candles(2000, seed=5, settings=settings)
    a = run_backtest(candles, settings)
    b = run_backtest(candles, settings)
    assert a["total_pnl"] == b["total_pnl"]
    assert a["cycles"] == b["cycles"]
    assert [t["price"] for t in a["trades"]] == [t["price"] for t in b["trades"]]


def test_backtest_volume_schedule_is_used():
    settings = bt_settings(max_positions=6, base_volume_usd=10.0)
    candles = demo_candles(6000, seed=3, settings=settings)
    result = run_backtest(candles, settings)
    volumes = {t["idx"]: t["volume_usd"] for t in result["trades"] if t["kind"] in ("entry", "add")}
    assert volumes, "должны быть входы"
    # in every cycle the volume grows according to the doubling schedule
    entries = [t for t in result["trades"] if t["kind"] == "entry"]
    adds = [t for t in result["trades"] if t["kind"] == "add" and t["idx"] >= 3]
    assert entries and all(t["volume_usd"] > 0 for t in entries)
    for t in adds:
        expected = 10.0 * 2 ** (t["idx"] - 3 + 1)
        assert t["volume_usd"] == pytest.approx(expected, rel=0.15)


def test_backtest_fees_reduce_profit():
    settings = bt_settings()
    candles = demo_candles(3000, seed=9, settings=settings)
    no_fee = run_backtest(candles, settings.model_copy(update={"fee_pct": 0.0, "slippage_pct": 0.0}))
    with_fee = run_backtest(candles, settings)
    assert with_fee["total_pnl"] < no_fee["total_pnl"]


def test_backtest_reports_margin_rejections_and_directional_leverage():
    settings = bt_settings(paper_balance_usd=1.0, leverage=1, direction="short")
    result = run_backtest(demo_candles(1000, seed=11, settings=settings), settings)
    assert result["margin_rejections"] > 0
    assert result["cycles"] == 0
    assert result["initial_balance"] == 1.0
    assert result["leverage"] == 1
    assert result["direction"] == "short"
    assert not result["busted"]
    assert all(trade["kind"] == "rejected" for trade in result["trades"])


def test_backtest_can_open_independent_long_and_short_cycles(monkeypatch):
    import app.strategy as strategy_module
    from app.fib import Impulse

    settings = bt_settings(direction="both", allow_opposite_cycles=True, leverage=5,
                           paper_balance_usd=1000.0, fee_pct=0.0, slippage_pct=0.0)

    def both_signals(candles, side_settings):
        side = side_settings.direction
        return Impulse(side=side, move_side="down" if side == "long" else "up",
                       high=105, low=95, entry_price=100, target=100,
                       move_pct=10, candles=3, start_ts=1, end_ts=2), "signal"

    monkeypatch.setattr(strategy_module, "detect_signal", both_signals)
    candles = [Candle(ts=i * 300_000, open=100, high=101, low=99, close=100, volume=1)
               for i in range(6)]
    result = run_backtest(candles, settings)
    entries = [trade for trade in result["trades"] if trade["kind"] == "entry"]
    assert {trade["side"] for trade in entries} == {"long", "short"}
    assert len({trade["cycle_id"] for trade in entries}) == 2
    assert {cycle["side"] for cycle in result["cycles_list"]} == {"long", "short"}
    assert result["leverage"] == 5
    assert result["allow_opposite_cycles"] is True


def test_backtest_marks_equity_below_zero_as_bankruptcy():
    settings = bt_settings(direction="long", entry_mode="impulse", candles_count=3,
                           min_move_pct=1.0, paper_balance_usd=1.0, leverage=100,
                           max_positions=1, multiplier_start_position=1,
                           fee_pct=0.0, slippage_pct=0.0)
    values = [
        (100, 100.1, 99.9, 100), (100, 100.1, 99.9, 100),
        (100, 100.1, 99.4, 99.5), (99.5, 99.6, 97.9, 98),
        (98, 98.1, 94.9, 95), (95, 95.1, 93.9, 94),
        (94, 94, 1, 1.1),
    ]
    candles = [Candle(ts=i * 300_000, open=o, high=h, low=low, close=c, volume=1)
               for i, (o, h, low, c) in enumerate(values)]
    result = run_backtest(candles, settings)
    assert result["busted"] is True
    assert result["bust_ts"] == candles[-1].ts
    assert result["final_equity"] <= 0
    assert result["cycles_list"][-1]["reason"].startswith("слив")
    assert not result["equity_curve"][-1]["notional_exposure"]


# ------------------------------------------------------------------ paper broker
@pytest.mark.asyncio
async def test_paper_broker_keeps_long_and_short_exposure_separate():
    broker = PaperBroker(bt_settings(fee_pct=0, slippage_pct=0))
    await broker.start()
    long_fill = await broker.open_position("long", 100.0, 100.0)
    short_fill = await broker.open_position("short", 100.0, 100.0)
    assert await broker.exchange_position("long") == pytest.approx(long_fill.qty)
    assert await broker.exchange_position("short") == pytest.approx(short_fill.qty)
    await broker.close_position("short", short_fill.qty, 90.0)
    assert await broker.exchange_position("short") == 0
    assert await broker.exchange_position("long") == pytest.approx(long_fill.qty)


@pytest.mark.asyncio
async def test_paper_broker_accounting():
    settings = bt_settings(fee_pct=0.1, slippage_pct=0.05)
    broker = PaperBroker(settings)
    await broker.start()
    await broker.set_price(100.0)
    fill = await broker.open_position("long", 100.0, 100.0)
    assert fill.price == pytest.approx(100.05)           # slippage против нас
    assert fill.qty == pytest.approx(100.0 / 100.05)
    assert fill.fee == pytest.approx(100.0 * 0.001)
    assert (await broker.exchange_position("long")) == pytest.approx(fill.qty)
    close = await broker.close_position("long", fill.qty, 110.0)
    assert close.price == pytest.approx(110.0 * 0.9995)
    assert (await broker.exchange_position("long")) == 0.0
    bal = await broker.balance()
    assert bal["mode"] == "paper"
    assert bal["fees"] > 0
    # куплено на 100 $, продано дороже -> баланс вырос на ~10 % минус комиссии
    assert bal["total"] > settings.paper_balance_usd + 9.0
    assert bal["realized_pnl"] == pytest.approx(bal["total"] - settings.paper_balance_usd, abs=1e-3)


def test_make_broker_respects_mode():
    assert isinstance(make_broker(bt_settings(trade_mode="paper")), PaperBroker)


def test_opposite_cycles_rejected_outside_paper_futures():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="только на фьючерсах"):
        bt_settings(market_type="spot", allow_opposite_cycles=True)
    with pytest.raises(ValidationError, match="только в paper и бэктесте"):
        bt_settings(data_source="gate", trade_mode="live", allow_opposite_cycles=True)


# ------------------------------------------------------------------ live loop
@pytest.mark.asyncio
async def test_runtime_demo_loop_trades_and_persists():
    """Full end-to-end: demo market -> engine -> paper broker -> sqlite."""
    from app.bot import BotRuntime

    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "test.sqlite3"))
    settings = bt_settings(timeframe="1m", demo_speed=240, poll_interval_sec=1.0,
                           min_move_pct=0.35, min_profit_pct=0.15, total_profit_target_usd=0.0)
    db.save_settings(settings)
    runtime = BotRuntime(db, settings)
    res = await runtime.start(settings)
    assert res["ok"] is True, res
    try:
        await asyncio.sleep(12)
        snapshot = runtime.snapshot()
        engine = runtime.engine
        assert snapshot["status"] in ("running", "paused"), snapshot["status"]
        assert engine.last_price > 0
        assert len(runtime.chart_candles()) > 20
        assert engine.signals_found > 0, "демо-рынок должен дать сигналы"
        # either a cycle is open or a cycle was already closed
        assert engine.cycle is not None or engine.cycles_total > 0
        if engine.cycle is not None:
            assert engine.cycle.target > engine.cycle.avg_entry
            assert engine.cycle.positions
            assert engine.cycle.profit_pct_to(engine.cycle.target) >= settings.min_profit_pct - 1e-6
        # persistence
        state = db.load_runtime_state()
        assert "realized_pnl" in state
        if engine.cycles_total:
            assert db.list_cycles(5)
            positions = db.all_positions(50)
            assert positions
    finally:
        await runtime.stop()
    assert runtime.status == "stopped"


# ------------------------------------------------------------- demo feed guard
@pytest.mark.asyncio
async def test_demo_feed_does_not_jump_after_a_long_pause():
    """После сна окружения демо-рынок не должен уезжать на часы за один опрос."""
    from app.feed import MAX_REAL_STEP_SEC, DemoFeed
    import app.feed as feed_module

    settings = bt_settings(timeframe="1m", demo_speed=30)
    feed = DemoFeed(settings, speed=30)
    price_before = feed.price
    # имитируем паузу длиной 25 минут
    feed._last_tick -= int(25 * 60 * 1000)
    closed, price = await feed.poll()
    # 10 реальных секунд × 30 = 300 виртуальных секунд = максимум 5 свечей по 1 минуте
    expected_max = int(MAX_REAL_STEP_SEC * 30 // 60) + 2
    assert len(closed) <= expected_max, f"сгенерировано {len(closed)} свечей за один опрос"
    assert abs(price / price_before - 1) < 0.05, "цена не должна прыгать на десятки процентов"
    # виртуальные часы идут синхронно с реальными (с учётом ускорения)
    assert abs((feed.vnow - feed.current.ts) / 1000) <= feed.tf_ms / 1000

    # обычный опрос в реальном времени по-прежнему даёт движение цены
    feed._last_tick -= 1000
    closed2, price2 = await feed.poll()
    assert price2 != price_before
