"""Рантайм: автозапуск после перезагрузки, сброс цикла, пауза при смене рынка."""
from __future__ import annotations

import os

import pytest

from app.bot import STATUS_ERROR, STATUS_PAUSED, STATUS_RUNNING, STATUS_STOPPED, BotRuntime
from app.config import BotSettings
from app.db import Database
from app.models import Fill
from app.strategy import StrategyEngine


def make_settings(**kwargs) -> BotSettings:
    base = dict(
        symbol="BTC/USDT", market_type="futures", timeframe="1m", data_source="demo",
        demo_speed=120, trade_mode="paper", poll_interval_sec=1.0, candles_count=3,
        entry_mode="impulse",          # сценарии используют простой импульс из 3 свечей
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
    monkeypatch.setenv("PLANB_PRESET", "synthetic")   # автономный запуск без интернета
    monkeypatch.setenv("PLANB_AUTOSTART_DELAY", "0")
    db_path = os.path.join(str(tmp_path), "app.sqlite3")
    app = create_app(db_path)
    runtime = app.state.runtime
    assert runtime.settings.data_source == "synthetic"
    runtime.db.kv_set("desired_running", True)

    for handler in app.router.on_startup:
        await handler()
    await app.state.autostart_task            # автозапуск идёт фоном

    assert runtime.status in (STATUS_RUNNING, STATUS_PAUSED)
    assert runtime.engine.last_price > 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_startup_does_not_wait_for_the_exchange(tmp_path, monkeypatch):
    """Панель должна подниматься, даже если биржа «висит».

    Регрессия: автозапуск выполнялся прямо в startup-обработчике, а uvicorn открывает
    порт только после него — при медленной бирже панель вообще не открывалась
    («служба активна, порт не слушается»).
    """
    import asyncio
    from app.main import create_app
    monkeypatch.delenv("PLANB_PRESET", raising=False)
    monkeypatch.setenv("PLANB_AUTOSTART_DELAY", "0")
    app = create_app(os.path.join(str(tmp_path), "hang.sqlite3"))
    runtime = app.state.runtime
    runtime.db.kv_set("desired_running", True)

    async def hang(_settings=None):
        await asyncio.sleep(3600)

    monkeypatch.setattr(runtime, "start", hang)
    for handler in app.router.on_startup:
        await asyncio.wait_for(handler(), timeout=1.0)     # обработчик возвращается сразу

    assert runtime.snapshot()["status"] == STATUS_STOPPED  # сервер уже отвечает панели
    app.state.autostart_task.cancel()


@pytest.mark.asyncio
async def test_start_gives_up_on_slow_exchange(tmp_path, monkeypatch):
    """Если биржа не отвечает, запуск завершается понятной ошибкой, а не висит вечно."""
    import asyncio
    runtime = make_runtime(tmp_path)
    monkeypatch.setenv("PLANB_FEED_TIMEOUT", "1")

    class SlowFeed:
        real = False
        label = "медленная биржа"
        last_error = ""

        async def history(self, limit=300):
            await asyncio.sleep(30)
            return []

        async def close(self):
            return None

        def status(self):
            return {"label": self.label, "real": False, "last_error": self.last_error,
                    "last_ok_ts": 0, "last_candle_ts": 0}

    monkeypatch.setattr("app.bot.make_feed", lambda *a, **kw: SlowFeed())
    result = await asyncio.wait_for(runtime.start(runtime.settings), timeout=10)
    assert result["ok"] is False
    assert "не ответила" in result["error"]
    assert runtime.status == STATUS_ERROR
    assert any("не ответила" in log["message"] for log in runtime.logs)


def test_demo_preset_uses_real_exchange_data(tmp_path, monkeypatch):
    """«Демо-режим» установки = виртуальные сделки на реальных свечах Gate.io."""
    from app.main import create_app
    monkeypatch.setenv("PLANB_PRESET", "demo")
    app = create_app(os.path.join(str(tmp_path), "demo.sqlite3"))
    settings = app.state.runtime.settings
    assert settings.data_source == "gate"
    assert settings.synthetic_data is False
    assert settings.trade_mode == "paper", "в демо-режиме сделки виртуальные"
    assert "реальные свечи" in settings.data_source_label()


def test_old_demo_setting_is_migrated_to_real_data(tmp_path, monkeypatch):
    """Старые установки с data_source=demo переводим на реальные свечи и пишем в журнал."""
    from app.main import create_app
    monkeypatch.delenv("PLANB_PRESET", raising=False)
    db_path = os.path.join(str(tmp_path), "old.sqlite3")
    db = Database(db_path)
    db.save_settings(BotSettings(**{**make_settings().model_dump(), "data_source": "synthetic"}))
    db.conn.execute("UPDATE kv SET value=? WHERE key='settings'",
                    ('{"data_source": "demo", "symbol": "BTC/USDT"}',))
    db.conn.commit()
    db.conn.close()

    app = create_app(db_path)
    runtime = app.state.runtime
    assert runtime.settings.data_source == "gate"
    assert any("реальные свечи" in log["message"] for log in runtime.logs)


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


# ------------------------------------- настройки, которым нужен новый фид
@pytest.mark.asyncio
async def test_connection_settings_restart_the_bot_for_real(tmp_path):
    """Скорость демо-рынка и таймфрейм нельзя поменять «на живую» — нужен новый фид.

    Раньше apply_settings звал start(), а тот выходил с «бот уже запущен»: настройка
    сохранялась, но не действовала — и пользователь видел старое поведение.
    """
    runtime = make_runtime(tmp_path)
    await runtime.start(runtime.settings)
    old_feed = runtime.feed

    new_settings = BotSettings(**{**runtime.settings.model_dump(), "demo_speed": 60.0})
    result = await runtime.apply_settings(new_settings)

    assert result["connection_changed"] is True and result["restarted"] is True
    assert runtime.settings.demo_speed == 60.0
    assert runtime.feed is not old_feed, "фид должен быть пересоздан со новой скоростью"
    assert runtime.feed.speed == 60.0
    assert any("перезапущен" in e["message"] for e in runtime.logs)
    await runtime.stop()


@pytest.mark.asyncio
async def test_threshold_settings_apply_without_restart(tmp_path):
    """Пороги движок читает на каждом тике — перезапускать бота для них не нужно."""
    runtime = make_runtime(tmp_path)
    await runtime.start(runtime.settings)
    feed_before = runtime.feed

    new_settings = BotSettings(**{**runtime.settings.model_dump(),
                                  "min_move_pct": 0.25, "min_profit_pct": 0.1,
                                  "candles_count": 4})
    result = await runtime.apply_settings(new_settings)

    assert result["connection_changed"] is False and result["restarted"] is False
    assert runtime.status == STATUS_RUNNING and runtime.feed is feed_before
    assert runtime.engine.settings.min_move_pct == 0.25
    assert runtime.engine.settings.candles_count == 4
    assert any("без перезапуска" in e["message"] for e in runtime.logs)
    await runtime.stop()
