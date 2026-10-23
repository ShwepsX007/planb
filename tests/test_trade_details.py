from __future__ import annotations

from app.db import Database
from app.models import Cycle, Position
from app.strategy import StrategyEngine
from app.config import BotSettings
import pytest


def _cycle() -> Cycle:
    return Cycle(
        id="detail-cycle", side="long", signal_ts=1, signal_price=100,
        opened_ts=10, swing_high=110, swing_low=90, goal_pct=50, target=100,
        positions=[Position(idx=1, ts=10, price=100, qty=2, volume_usd=200, fee=0.2)],
    )


def test_cycle_tracks_worst_observed_unrealized_loss():
    cycle = _cycle()
    StrategyEngine._record_cycle_adverse(cycle, 95)
    assert cycle.max_adverse_pnl_usd == pytest.approx(10.2)  # loss plus entry fee
    assert cycle.max_adverse_pnl_pct == pytest.approx(5.1)
    StrategyEngine._record_cycle_adverse(cycle, 97)
    assert cycle.max_adverse_pnl_usd == 10.2


def test_closed_cycle_keeps_historical_average_and_quantity():
    cycle = _cycle()
    cycle.positions.append(Position(idx=2, ts=20, price=110, qty=1, volume_usd=110))
    for position in cycle.positions:
        position.close_ts = 30
        position.close_price = 105
    cycle.status = "closed"
    assert cycle.total_qty == 3
    assert cycle.avg_entry == 103.33333333333333


def test_cycle_risk_metrics_survive_runtime_restore():
    cycle = _cycle()
    cycle.max_adverse_pnl_usd = 8.5
    cycle.max_adverse_pnl_pct = 4.25
    cycle.peak_notional_usd = 230
    cycle.peak_margin_usd = 46
    cycle.max_deposit_load_pct = 4.6
    cycle.risk_candle_ts = 12345
    engine = StrategyEngine(BotSettings())
    engine.restore(engine._cycle_raw(cycle))
    restored = engine.cycle
    assert restored is not None
    assert restored.max_adverse_pnl_usd == 8.5
    assert restored.max_adverse_pnl_pct == 4.25
    assert restored.peak_notional_usd == 230
    assert restored.peak_margin_usd == 46
    assert restored.max_deposit_load_pct == 4.6
    assert restored.risk_candle_ts == 12345


def test_database_persists_cycle_risk_and_exposure_fields(tmp_path):
    db = Database(str(tmp_path / "details.sqlite3"))
    cycle = _cycle()
    cycle.max_adverse_pnl_usd = 12.5
    cycle.max_adverse_pnl_pct = 6.25
    cycle.peak_notional_usd = 240
    cycle.peak_margin_usd = 48
    cycle.max_deposit_load_pct = 4.8
    db.save_cycle(cycle)
    db.save_position(cycle, cycle.positions[0])

    stored = db.list_cycles(1)[0]
    position = db.all_positions(1)[0]
    assert stored["max_adverse_pnl_usd"] == 12.5
    assert stored["max_adverse_pnl_pct"] == 6.25
    assert stored["peak_notional_usd"] == 240
    assert stored["peak_margin_usd"] == 48
    assert stored["max_deposit_load_pct"] == 4.8
    assert stored["avg_entry"] == 100
    assert position["ts"] == 10 and position["price"] == 100
