"""API smoke tests (httpx ASGI transport keeps everything in one event loop)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import AsyncIterator

import httpx
import pytest


def make_app(tmp_path: str):
    from app.main import create_app
    return create_app(os.path.join(tmp_path, "api.sqlite3"))


@pytest.fixture
async def client(tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    app = make_app(str(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_settings_and_state_endpoints(client: httpx.AsyncClient):
    res = await client.get("/api/health")
    assert res.status_code == 200 and res.json()["ok"]

    state = (await client.get("/api/state")).json()
    assert state["status"] == "stopped"
    assert "engine" in state and state["symbol"]

    settings = (await client.get("/api/settings")).json()
    assert settings["timeframe"] == "5m"
    assert settings["volume_schedule"][:3] == [10.0, 10.0, 20.0]

    payload = {"timeframe": "15m", "fib_goal_pct": 61.8, "min_profit_pct": 0.25,
               "api_key": "abc12345", "api_secret": "supersecret"}
    res = await client.put("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    settings = res.json()["settings"]
    assert settings["timeframe"] == "15m"
    assert settings["fib_goal_pct"] == 61.8
    assert settings["api_key"].startswith("*") and settings["api_key"].endswith("2345")
    assert settings["api_secret"] == ""            # секрет никогда не возвращается

    # a masked key must not overwrite the stored one
    res2 = await client.put("/api/settings", json={"api_key": "********2345"})
    assert res2.status_code == 200
    stored = (await client.get("/api/settings")).json()
    assert stored["api_key"].endswith("2345")

    bad = await client.put("/api/settings", json={"timeframe": "7m"})
    assert bad.status_code == 422

    demo = await client.post("/api/settings/test-connection", json={"data_source": "demo"})
    assert demo.json()["ok"] is True


async def test_candles_backtest_and_bot_lifecycle(client: httpx.AsyncClient):
    await client.put("/api/settings", json={"data_source": "demo", "timeframe": "1m",
                                            "demo_speed": 240, "poll_interval_sec": 1,
                                            "min_move_pct": 0.35, "min_profit_pct": 0.15})
    payload = {"source": "demo", "limit_candles": 1500, "min_move_pct": 0.5,
               "min_profit_pct": 0.3, "seed": 3, "timeframe": "5m"}
    res = await client.post("/api/backtest", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["ok"] is True
    assert data["candles"] == 1500
    assert data["cycles"] > 0
    assert (await client.get(f"/api/backtest/{data['id']}")).json()["ok"] is True
    assert (await client.get("/api/backtests")).json()["backtests"]

    assert "candles" in (await client.get("/api/candles?limit=100")).json()

    start = await client.post("/api/bot/start")
    assert start.status_code == 200 and start.json()["ok"] is True
    state = (await client.get("/api/state")).json()
    assert state["status"] in ("running", "paused")

    await asyncio.sleep(4)                       # let the demo market generate candles
    state = (await client.get("/api/state")).json()
    assert state["engine"]["last_price"] > 0
    assert len((await client.get("/api/candles?limit=50")).json()["candles"]) > 3

    assert (await client.post("/api/bot/pause")).json()["status"] in ("paused", "stopped")
    resumed = (await client.post("/api/bot/resume")).json()["status"]
    assert resumed in ("running", "paused")
    assert (await client.post("/api/bot/stop")).json()["status"] == "stopped"
    assert (await client.post("/api/bot/reset-stats")).json()["ok"] is True

    assert (await client.get("/api/logs?limit=50")).json()["logs"]
    assert (await client.get("/api/cycles")).json()["ok"] is True
    assert (await client.get("/api/markets")).json()["ok"] is True
