from __future__ import annotations

import pytest

from app.bot import BotRuntime
from app.config import BotSettings
from app.db import Database
from app.models import Cycle, now_ms


def closed_cycle(cycle_id: str, closed_ts: int, pnl: float, status: str = "closed") -> Cycle:
    return Cycle(id=cycle_id, side="long", signal_ts=closed_ts - 1000,
                 signal_price=100.0, opened_ts=closed_ts - 1000,
                 swing_high=110.0, swing_low=90.0, goal_pct=50.0,
                 target=100.0, status=status, closed_ts=closed_ts if status == "closed" else None,
                 close_price=100.0 if status == "closed" else None, pnl=pnl)


def test_database_daily_aggregate_counts_only_closed_cycles_in_window(tmp_path):
    db = Database(str(tmp_path / "stats.sqlite3"))
    db.save_cycle(closed_cycle("old", 99, 5.0))
    db.save_cycle(closed_cycle("win", 100, 2.5))
    db.save_cycle(closed_cycle("loss", 101, -1.0))
    db.save_cycle(closed_cycle("open", 102, 100.0, status="open"))

    stats = db.cycle_stats_since(100)

    assert stats == {"cycles": 2, "wins": 1, "losses": 1, "pnl": 1.5,
                     "win_rate": 50.0}


@pytest.mark.asyncio
async def test_daily_and_all_time_statistics_reset_independently_without_deleting_history(tmp_path):
    db = Database(str(tmp_path / "stats-reset.sqlite3"))
    db.save_cycle(closed_cycle("recent", now_ms() - 1000, 2.5))
    runtime = BotRuntime(db, BotSettings())
    runtime.engine.realized_pnl = 12.5
    runtime.engine.cycles_total = 7
    runtime.engine.wins = 5
    runtime.engine.losses = 2

    await runtime.reset_daily_stats()
    after_daily = runtime.snapshot()
    assert after_daily["engine"]["realized_pnl"] == 12.5
    assert after_daily["engine"]["cycles_total"] == 7
    assert after_daily["stats_24h"]["cycles"] == 0

    await runtime.reset_stats()
    after_all_time = runtime.snapshot()
    assert after_all_time["engine"]["realized_pnl"] == 0.0
    assert after_all_time["engine"]["cycles_total"] == 0
    assert after_all_time["stats_24h"]["cycles"] == 0
    assert len(db.list_cycles(limit=10)) == 1
