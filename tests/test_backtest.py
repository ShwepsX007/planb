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


# ------------------------------------------------------------------ paper broker
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
