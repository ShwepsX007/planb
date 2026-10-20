from __future__ import annotations

import pytest

from app.brokers.gate import GateBroker
from app.brokers.paper import PaperBroker
from app.config import BotSettings
from app.db import Database
from app.bot import BotRuntime
from app.models import Candle, Cycle, Position
from app.strategy import StrategyEngine


def cycle() -> Cycle:
    position = Position(idx=1, ts=1, price=100.0, qty=0.1,
                        volume_usd=10.0, fee=0.0)
    return Cycle(id="cycle123456", side="long", signal_ts=1, signal_price=100.0,
                 opened_ts=1, swing_high=110.0, swing_low=100.0, goal_pct=50.0,
                 target=105.0, move_side="down", positions=[position],
                 extreme_at_last_entry=100.0)


def test_next_add_plan_is_calculated_in_advance_from_profit_threshold():
    settings = BotSettings(entry_mode="swing", min_profit_pct=0.5,
                           base_volume_usd=10, max_positions=6,
                           require_new_extreme_for_add=True)
    engine = StrategyEngine(settings)
    engine.cycle = cycle()

    plan = engine.next_add_plan(current_price=100.0)

    assert plan is not None
    assert plan["idx"] == 2
    assert plan["volume_usd"] == 10
    # target = low + 50% * (high - low), and target/avg - 1 = 0.5%
    assert plan["price"] == pytest.approx(91.0, abs=1e-4)


@pytest.mark.asyncio
async def test_paper_add_limit_fills_when_intrabar_low_touches_even_if_close_recovers():
    settings = BotSettings(trade_mode="paper", fee_pct=0.0, slippage_pct=0.0)
    broker = PaperBroker(settings)
    await broker.open_position("long", 10.0, 100.0)
    placed = await broker.place_pending_order(purpose="add", side="long", price=90.0,
                                              volume_usd=9.0, client_id="cycle123456-add-2")

    await broker.update_market(94.0, Candle(ts=10, open=95, high=96, low=89.5,
                                             close=94, volume=10))
    status = await broker.order_status(placed["id"], side="long")

    assert status["status"] == "closed"
    assert status["filled_qty"] == pytest.approx(0.1)
    assert status["average"] == pytest.approx(90.0)
    assert broker.open_qty == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_paper_take_profit_fills_on_wick_even_when_price_returns_below_target():
    settings = BotSettings(trade_mode="paper", fee_pct=0.0, slippage_pct=0.0)
    broker = PaperBroker(settings)
    await broker.open_position("long", 10.0, 100.0)
    placed = await broker.place_pending_order(purpose="tp", side="long", price=105.0,
                                              qty=0.1, client_id="cycle123456-tp-0")

    await broker.update_market(103.0, Candle(ts=10, open=104, high=105.4, low=102.8,
                                              close=103, volume=10))
    status = await broker.order_status(placed["id"], side="long")

    assert status["status"] == "closed"
    assert status["average"] == pytest.approx(105.0)
    assert status["filled_qty"] == pytest.approx(0.1)
    assert broker.open_qty == 0


@pytest.mark.asyncio
async def test_runtime_stages_add_and_tp_then_closes_on_intrabar_tp_wick(tmp_path):
    settings = BotSettings(trade_mode="paper", fee_pct=0.0, slippage_pct=0.0)
    runtime = BotRuntime(Database(str(tmp_path / "pending.sqlite3")), settings)
    runtime.engine.cycle = cycle()
    runtime.broker = PaperBroker(settings)
    runtime.broker.restore_cycle(runtime.engine.cycle)

    await runtime._sync_cycle_orders()
    add_order = runtime._managed_orders["add"]
    tp_order = runtime._managed_orders["tp"]
    assert add_order["idx"] == 2
    assert add_order["volume_usd"] == 10.0
    assert add_order["price"] == pytest.approx(91.0, abs=1e-4)
    assert tp_order["qty"] == pytest.approx(0.1)
    chart_order = runtime.chart_payload()["pending_orders"][0]
    assert chart_order["role"] == "add"
    assert chart_order["volume_usd"] == pytest.approx(10.0)
    assert chart_order["qty"] == pytest.approx(10.0 / 91.0)

    # Add fills on the wick; after observing it, target order is resized to all qty.
    await runtime.broker.update_market(92.0, Candle(ts=20, open=95, high=96,
                                                    low=90.5, close=92, volume=5))
    await runtime._poll_managed_orders()
    assert len(runtime.engine.cycle.positions) == 2
    await runtime._sync_cycle_orders()
    assert runtime._managed_orders["tp"]["qty"] == pytest.approx(runtime.engine.cycle.total_qty)

    # TP is hit intrabar and price closes back below it: the resting order still fills.
    await runtime.broker.update_market(103.0, Candle(ts=21, open=104, high=106,
                                                      low=102, close=103, volume=8))
    await runtime._poll_managed_orders()
    assert runtime.engine.cycle is None


class FakeGateExchange:
    def __init__(self):
        self.created = []

    def amount_to_precision(self, _symbol, amount):
        return str(round(float(amount), 0))

    def price_to_precision(self, _symbol, price):
        return str(round(float(price), 2))

    async def create_order(self, *args):
        self.created.append(args)
        return {"id": "gate-order-1", "status": "open", "price": args[4],
                "amount": args[3], "clientOrderId": args[5].get("clientOrderId")}


@pytest.mark.asyncio
async def test_gate_futures_amounts_are_converted_to_contracts_and_tp_is_reduce_only():
    settings = BotSettings(trade_mode="live", api_key="fake", api_secret="fake",
                           market_type="futures")
    broker = GateBroker(settings)
    broker.ex = FakeGateExchange()
    broker.unified = "BTC/USDT:USDT"
    broker.market = {"contract": True, "contractSize": 0.001,
                     "limits": {"amount": {"min": 1}, "cost": {"min": None}}}

    assert broker._amount(0.01, 1000) == 10
    fill = broker._fill_from_order({"amount": 10, "filled": 10, "average": 1000}, "long", 1000)
    assert fill.qty == pytest.approx(0.01)

    await broker.place_pending_order(purpose="tp", side="long", price=1010,
                                     qty=0.01, client_id="cycle123456-t-0")
    _symbol, order_type, order_side, amount, price, params = broker.ex.created[0]
    assert (order_type, order_side, amount, price) == ("limit", "sell", 10, 1010)
    assert params["reduceOnly"] is True
    assert params["clientOrderId"] == "cycle123456-t-0"
