"""Рантайм: автозапуск после перезагрузки, сброс цикла, пауза при смене рынка."""
from __future__ import annotations

import os

import pytest

from app.bot import STATUS_PAUSED, STATUS_RUNNING, STATUS_STOPPED, BotRuntime
from app.config import BotSettings
from app.db import Database
from app.models import Fill
from app.strategy import StrategyEngine


def make_settings(**kwargs) -> BotSettings:
    base = dict(
        symbol="BTC/USDT", market_type="futures", timeframe="1m", data_source="demo",
        demo_speed=120, trade_mode="paper", poll_interval_sec=1.0, candles_count=3,
        min_move_pct=0.4, min_profit_pct=0.2, base_volume_usd=10.0, max_positions=6,
        total_profit_target_usd=0.0, paper_balance_usd=1000.0,
    )
    base.update(kwargs)
    return BotSettings(**base)


def make_runtime(tmp_path, settings: BotSettings | None = None) -> BotRuntime:
    db = Database(os.path.join(str(tmp_path), "runtime.sqlite3"))
    settings = settings or make_settings()
    db.save_settings(settings)
    return BotRuntime(db, settings)


def candles_down():
    from tests.test_strategy import down_impulse
    return down_impulse()


def put_cycle(runtime: BotRuntime) -> None:
    """Открыть цикл напрямую через движок (без сети)."""
    candles = candles_down()
    engine = runtime.engine
    engine.set_history(candles[:-1])
    action = engine.on_candle(candles[-1])[0]
    engine.apply_fill(action, Fill(price=candles[-1].close,
                                   qty=action.volume_usd / candles[-1].close, ts=candles[-1].ts))
    runtime._persist_state()


# ---------------------------------------------------------------- автозапуск
def test_fresh_install_waits_for_start_button(tmp_path):
    runtime = make_runtime(tmp_path)
    assert runtime.engine.cycle is None
    assert runtime.should_autostart() is False      # ничего не запускаем без команды
    assert runtime.status == STATUS_STOPPED


@pytest.mark.asyncio
async def test_autostart_flag_follows_start_and_stop(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.start(runtime.settings)
    assert runtime.should_autostart() is True       # пользователь нажал «Старт»
    await runtime.stop()
    assert runtime.should_autostart() is False      # и «Стоп» тоже помним


def test_restored_cycle_is_taken_over_after_restart(tmp_path):
    runtime = make_runtime(tmp_path)
    put_cycle(runtime)
    assert runtime.engine.cycle is not None

    # «перезагрузка сервера»: новый процесс читает ту же базу
    again = BotRuntime(runtime.db, runtime.settings)
    assert again.engine.cycle is not None
    assert again.status == STATUS_PAUSED            # ждём подтверждения, но состояние знаем
    assert again.should_autostart() is True         # открытая позиция не остаётся без управления


@pytest.mark.asyncio
async def test_startup_event_starts_bot_when_requested(tmp_path, monkeypatch):
    """Проверяем именно ту связку, что включает автозапуск в systemd."""
    from app.main import create_app
    monkeypatch.setenv("PLANB_PRESET", "demo")      # как в .env, который создаёт install.sh
    db_path = os.path.join(str(tmp_path), "app.sqlite3")
    app = create_app(db_path)
    runtime = app.state.runtime
    assert runtime.settings.data_source == "demo"
    runtime.db.kv_set("desired_running", True)

    for handler in app.router.on_startup:
        await handler()

    assert runtime.status in (STATUS_RUNNING, STATUS_PAUSED)
    assert runtime.engine.last_price > 0
    await runtime.stop()


# ------------------------------------------------------------- сброс цикла
@pytest.mark.asyncio
async def test_reset_cycle_clears_accounting(tmp_path):
    runtime = make_runtime(tmp_path)
    put_cycle(runtime)
    runtime.status = STATUS_PAUSED
    runtime.paused = True

    result = await runtime.reset_cycle("позиция закрыта на бирже вручную")
    assert result["ok"] is True
    assert runtime.engine.cycle is None
    assert runtime.paused is False
    assert runtime.db.list_cycles(5)                 # цикл остался в истории
    assert runtime.db.load_runtime_state()["cycle_raw"] is None
    # повторный сброс без цикла — понятная ошибка
    assert (await runtime.reset_cycle())["ok"] is False


# ------------------------------------------------------- смена рынка/символа
@pytest.mark.asyncio
async def test_market_change_pauses_instead_of_trading_a_new_market(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.start(runtime.settings)
    put_cycle(runtime)
    assert runtime.engine.cycle is not None

    new_settings = BotSettings(**{**runtime.settings.model_dump(), "timeframe": "5m"})
    result = await runtime.apply_settings(new_settings)

    assert result["restarted"] is True
    assert result["paused_after_restart"] is True
    assert runtime.status == STATUS_PAUSED and runtime.paused is True
    assert runtime.engine.cycle is not None          # цикл сохранён, но не торгуется
    await runtime.stop()


@pytest.mark.asyncio
async def test_settings_change_without_cycle_does_not_pause(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.start(runtime.settings)
    new_settings = BotSettings(**{**runtime.settings.model_dump(), "timeframe": "5m"})
    result = await runtime.apply_settings(new_settings)
    assert result["paused_after_restart"] is False
    assert runtime.paused is False
    await runtime.stop()
