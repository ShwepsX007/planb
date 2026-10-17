"""Реальные данные доводят свечи до стратегии — регрессия «бот молчит».

Баг, который здесь проверяется: формирующаяся свеча попадала в общий список истории
ради графика, а когда она закрывалась, фид считал её уже известной и НЕ отдавал движку.
На реальных данных (в отличие от синтетики) это значит, что стратегия не получала ни
одной свечи: «ожидание свечей», ноль сигналов, бот молчит.
"""
from __future__ import annotations

import asyncio
import os

import pytest

import app.feed as feed_mod
from app.bot import STATUS_RUNNING, BotRuntime
from app.config import BotSettings
from app.db import Database
from app.models import Candle, now_ms
from app.strategy import scan_signals
from tests.test_history import FakeExchange, make_candles

MIN = 60_000


def impulse_candles(count: int, *, move_pct: float, tf_ms: int = MIN, end_ts: int,
                    start_price: float = 60_000.0, direction: str = "down"):
    """Свечи с заданным процентным движением: каждые ``count`` свечей — импульс."""
    candles = []
    price = start_price
    for i in range(count):
        open_ = price
        close = price * (1 + (move_pct / 100.0 if direction == "up" else -move_pct / 100.0))
        candles.append(Candle(ts=end_ts - (count - 1 - i) * tf_ms, open=open_,
                              high=max(open_, close), low=min(open_, close), close=close,
                              volume=1.5))
        price = close
    return candles


# --------------------------------------------------------------- доставка свечей
class Clock:
    """Управляемые часы: фид считает свечу закрытой по этому времени."""

    def __init__(self, monkeypatch):
        self.now = (now_ms() // MIN) * MIN + 20_000
        monkeypatch.setattr(feed_mod, "now_ms", lambda: self.now)

    def tick(self, seconds: int) -> None:
        self.now += seconds * 1000


@pytest.fixture
def gate_env(monkeypatch):
    """Фиктивная биржа Gate + управляемые часы; возвращает (feed-фабрика, часы, биржа)."""
    def build(settings: BotSettings, *, candles=None, page: int = 1000):
        clock = Clock(monkeypatch)
        closed = candles or make_candles(300, tf_ms=MIN,
                                        end_ts=(clock.now // MIN - 1) * MIN)
        live = Candle(ts=(clock.now // MIN) * MIN, open=closed[-1].close, high=closed[-1].close,
                      low=closed[-1].close, close=closed[-1].close, volume=0.0)
        exchange = FakeExchange(list(closed) + [live], page=page)
        import ccxt.async_support as ccxt_async
        monkeypatch.setattr(ccxt_async, "gate", lambda _config: exchange)
        feed = feed_mod.GateFeed(settings)
        return feed, clock, exchange
    return build


async def test_closed_candle_is_delivered_even_if_seen_live(gate_env):
    """Свеча, которую фид видел формирующейся, обязана дойти до движка после закрытия."""
    settings = BotSettings(symbol="BTC/USDT", market_type="futures", timeframe="1m",
                           data_source="gate", trade_mode="paper")
    feed, clock, exchange = gate_env(settings)

    history = list(await feed.history(300))
    assert len(history) == 300
    new, price = await feed.poll()
    assert new == [], "новых закрытых свечей пока нет"
    assert feed.live is not None, "формирующаяся свеча должна быть доступна для графика"

    # свеча закрылась — и теперь она ДОЛЖНА быть отдана движку
    clock.tick(60)
    new, price = await feed.poll()
    assert len(new) == 1, "закрытая свеча не дошла до стратегии — бот будет молчать"
    closed_ts = new[0].ts
    assert feed._is_closed(new[0])
    assert closed_ts == history[-1].ts + MIN

    # и ровно один раз: повторные опросы не дублируют свечу
    for _ in range(3):
        again, _ = await feed.poll()
        assert again == []
    assert closed_ts in feed.delivered
    assert feed.delivered_upto == closed_ts


async def test_history_refresh_does_not_swallow_new_candle(gate_env):
    """Обновление графика (history) не должно «съедать» только что закрытую свечу."""
    settings = BotSettings(symbol="BTC/USDT", market_type="futures", timeframe="1m",
                           data_source="gate", trade_mode="paper")
    feed, clock, exchange = gate_env(settings)
    await feed.history(300)

    # добавим на бирже ещё две закрывающиеся свечи, как это бывает в реальности
    last = feed.candles[-1]
    extra = [Candle(ts=last.ts + MIN, open=last.close, high=last.close * 1.001,
                    low=last.close * 0.999, close=last.close, volume=1.0),
             Candle(ts=last.ts + 2 * MIN, open=last.close, high=last.close * 1.001,
                    low=last.close * 0.999, close=last.close, volume=1.0)]
    exchange.candles.extend(extra)

    clock.tick(60)                      # закрылась 1-я «живая» свеча
    await feed.history(300)             # обновление графика ДО опроса — раньше теряло свечу
    new, _ = await feed.poll()
    assert [c.ts for c in new] == [last.ts + MIN]

    clock.tick(60)
    new2, _ = await feed.poll()
    assert [c.ts for c in new2] == [last.ts + 2 * MIN]


async def test_live_candle_never_reaches_the_engine(gate_env):
    """Формирующуюся свечу стратегии отдавать нельзя — иначе вход «по будущему»."""
    settings = BotSettings(symbol="BTC/USDT", market_type="futures", timeframe="1m",
                           data_source="gate", trade_mode="paper")
    feed, clock, exchange = gate_env(settings)
    await feed.history(300)
    for _ in range(5):
        new, _ = await feed.poll()
        assert all(feed._is_closed(c) for c in new)
        assert feed.live is not None and not feed._is_closed(feed.live)
        clock.tick(5)                   # время идёт, но свеча ещё не закрылась (всего 25 с)
    chart = feed.chart_candles(10)
    assert chart[-1].ts == feed.live.ts, "на графике должна быть видна текущая свеча"
    assert all(feed.is_delivered(c.ts) for c in chart[:-1]), "закрытые свечи уже отданы"


# ------------------------------------------------- свечи доходят до стратегии целиком
class ScriptedFeed:
    """Фид как GateFeed, но свечи отдаются по сценарию (для проверки рантайма)."""

    real = True
    label = "свечи Gate.io (тест)"

    def __init__(self, history, batches):
        self._history = list(history)
        self._batches = list(batches)
        self.candles = list(history)
        self.last_error = ""
        self.live = None

    async def history(self, limit: int = 300):
        return list(self._history)[-limit:]

    async def poll(self):
        if self._batches:
            candle = next(iter(self._batches.pop(0)))
            self.candles.append(candle)
            return [candle], candle.close
        await asyncio.sleep(0.05)
        return [], self.candles[-1].close

    async def close(self):
        return None

    def status(self):
        return {"name": "gate", "label": self.label, "real": True, "last_error": "",
                "last_ok_ts": now_ms(), "last_candle_ts": self.candles[-1].ts,
                "last_closed_ts": self.candles[-1].ts, "live_ts": 0,
                "symbol": "BTC/USDT", "timeframe": "1m"}


async def test_runtime_reacts_to_candles_from_the_exchange(tmp_path, monkeypatch):
    """Полный путь: свечи с биржи → движок → счётчик обработанных свечей → вход."""
    base = make_candles(300, tf_ms=MIN, end_ts=(now_ms() // MIN - 1) * MIN)
    price = base[-1].close
    impulse = []
    for _ in range(3):                   # три падающие свечи — импульс вниз → вход в лонг
        open_, close = price, price * 0.995
        impulse.append(Candle(ts=base[-1].ts + MIN * (len(impulse) + 1), open=open_,
                              high=max(open_, close), low=min(open_, close), close=close,
                              volume=2.0))
        price = close

    settings = BotSettings(symbol="BTC/USDT", market_type="futures", timeframe="1m",
                           data_source="gate", trade_mode="paper", candles_count=3,
                           entry_mode="impulse",   # проверяем доставку, а не детектор
                           min_move_pct=0.5, fib_goal_pct=50.0, min_profit_pct=0.1,
                           base_volume_usd=10.0, max_positions=6, total_profit_target_usd=0.0,
                           poll_interval_sec=1.0, paper_balance_usd=1000.0,
                           candle_lookback=300)
    db = Database(os.path.join(str(tmp_path), "gate.sqlite3"))
    db.save_settings(settings)
    runtime = BotRuntime(db, settings)
    feed = ScriptedFeed(base, [[c] for c in impulse])
    monkeypatch.setattr("app.bot.make_feed", lambda *a, **kw: feed)
    monkeypatch.setenv("PLANB_FEED_TIMEOUT", "5")

    result = await runtime.start(settings)
    assert result["ok"], result

    for _ in range(80):                  # ждём обработки свечей (цикл опроса — 1 с)
        await asyncio.sleep(0.25)
        if runtime.engine.candles_seen >= 3:
            break
    assert runtime.status == STATUS_RUNNING
    assert runtime.engine.candles_seen >= 3, "движок не получил свечи с биржи"
    assert runtime.engine.last_candle_ts == impulse[-1].ts
    assert runtime.engine.cycle is not None, "стратегия должна была открыть цикл"
    assert runtime.engine.cycle.side == "long"
    assert runtime.engine.signals_found >= 1
    assert runtime.engine.last_check.startswith("сигнал")
    assert "движение" not in runtime.engine.last_check
    snapshot = runtime.snapshot()
    assert snapshot["data_real"] is True
    assert snapshot["engine"]["last_candle_ts"] == impulse[-1].ts
    await runtime.stop()


# ---------------------------------------------------------------- сканер сигналов
def test_scan_explains_near_misses_without_crashing():
    """«Почти-входы» — объяснение тишины, и они не должны ронять сканер.

    Регрессия: при порогах, когда до входа не хватает профита (а не движения),
    ветка «почти-вошли» вызывала внутренний NameError — и панель вместо подсказки
    «чего не хватает» показывала ошибку. Ровно в этой ситуации оказывается
    пользователь с высокими порогами на реальных данных.
    """
    settings = BotSettings(timeframe="1m", candles_count=3, min_move_pct=0.5,
                           fib_goal_pct=50.0, min_profit_pct=0.5, direction="both")
    for direction in ("down", "up"):
        candles = impulse_candles(120, move_pct=0.2, end_ts=(now_ms() // MIN - 1) * MIN,
                                  direction=direction)
        scan = scan_signals(candles, settings)
        assert "error" not in scan, scan
        assert scan["windows"] > 100
        assert scan["signals"] == 0, "вход не должен проходить по порогу профита"
        assert scan["near"], "движение проходит, профита не хватает — это «почти-вход»"
        assert all(item["side"] in ("up", "down") for item in scan["near"])
        assert scan["near"][0]["move_pct"] >= settings.min_move_pct * 0.8


def test_scan_suggests_threshold_from_real_movements():
    """Сканер сам предлагает порог: не выше того, что рынок реально давал."""
    settings = BotSettings(timeframe="1m", candles_count=3, min_move_pct=3.0,
                           fib_goal_pct=50.0, min_profit_pct=0.15, direction="both")
    candles = impulse_candles(200, move_pct=0.4, end_ts=(now_ms() // MIN - 1) * MIN)
    scan = scan_signals(candles, settings)
    assert "error" not in scan
    assert scan["signals"] == 0 and scan["windows"] > 150
    suggested = scan["suggested_threshold_pct"]
    assert 0.05 <= suggested < settings.min_move_pct, suggested
    assert suggested <= round(scan["move_max_pct"], 3), (suggested, scan["move_max_pct"])
    assert scan["min_profit_pct"] == 0.15


def test_journal_explains_silence_once_per_settings():
    """Тишина должна быть объяснена в журнале — один раз, с цифрами и советом."""
    runtime = BotRuntime(Database(":memory:"),
                         BotSettings(timeframe="1m", candles_count=3, min_move_pct=3.0,
                                     fib_goal_pct=50.0, min_profit_pct=0.15))
    runtime._chart_candles = impulse_candles(200, move_pct=0.4, end_ts=(now_ms() // MIN - 1) * MIN)
    runtime._refresh_signal_scan()
    warnings = [e for e in runtime.logs if e["level"] == "warn" and "Входов не было" in e["message"]]
    assert len(warnings) == 1, [e["message"] for e in warnings]
    text = warnings[0]["message"]
    assert "порог «Минимальное движение цены» — 3.0%" in text
    assert "попробуйте" in text and "%" in text

    # второй скан при тех же настройках молчит: журнал не засоряем
    runtime._refresh_signal_scan()
    assert len([e for e in runtime.logs if "Входов не было" in e["message"]]) == 1

    # настройки поменяли — про тишину сообщаем снова
    runtime.settings.min_move_pct = 2.0
    runtime.settings.min_profit_pct = 0.1
    runtime._refresh_signal_scan()
    assert len([e for e in runtime.logs if "Входов не было" in e["message"]]) == 2


def test_scan_signals_counts_and_explains():
    """Сканер должен отвечать цифрами: сколько сигналов и какие были движения."""
    settings = BotSettings(timeframe="1m", candles_count=3, min_move_pct=0.6,
                           entry_mode="impulse",   # здесь проверяется механика сканера
                           fib_goal_pct=50.0, min_profit_pct=0.1, direction="both")

    # спокойный рынок: 0.02 % на свечу — порог 0.6 % недостижим
    flat = impulse_candles(200, move_pct=0.02, end_ts=(now_ms() // MIN - 1) * MIN)
    flat = [Candle(ts=c.ts, open=c.open, high=c.open * 1.0001, low=c.open * 0.9999,
                   close=c.close, volume=1.0) for c in flat]
    scan_flat = scan_signals(flat, settings)
    assert scan_flat["signals"] == 0
    assert scan_flat["windows"] > 100
    assert scan_flat["move_max_pct"] < settings.min_move_pct
    assert "порога" in scan_flat["last_reason"]

    # волатильный рынок: 1 % на свечу — импульсы находятся
    volatile = impulse_candles(60, move_pct=1.0, end_ts=(now_ms() // MIN - 1) * MIN)
    scan_spiky = scan_signals(volatile, settings)
    assert scan_spiky["signals"] > 0
    assert scan_spiky["signal_list"] and scan_spiky["signal_list"][-1]["side"] in ("long", "short")
    assert scan_spiky["move_max_pct"] >= settings.min_move_pct

    empty = scan_signals(impulse_candles(2, move_pct=0.1, end_ts=now_ms()), settings)
    assert empty["enough_data"] is False and empty["signals"] == 0


def test_scan_signals_reports_near_misses():
    """Случаи «почти вошли» должны быть видны: они объясняют, чего не хватает."""
    settings = BotSettings(timeframe="1m", candles_count=3, min_move_pct=0.5,
                           fib_goal_pct=50.0, min_profit_pct=0.05)
    # падаем по 0.2 % за свечу: за три свечи 0.6 % — выше порога 0.5 %,
    # но профит до цели (50 % сетки) всего 0.3 % — то есть «почти вошли»
    candles = [Candle(ts=c.ts, open=c.open, high=c.open, low=c.close, close=c.close, volume=1.0)
               for c in impulse_candles(90, move_pct=0.2, end_ts=(now_ms() // MIN - 1) * MIN)]
    scan = scan_signals(candles, settings)
    assert scan["enough_data"] and scan["windows"] > 0
    assert scan["threshold_pct"] == 0.5
    assert "move_p90_pct" in scan and "move_median_pct" in scan


async def test_api_signals_scan_endpoint(tmp_path):
    """Эндпоинт сканера: отвечает и при пустой истории, и с данными."""
    import httpx
    from app.main import create_app
    app = create_app(os.path.join(str(tmp_path), "scan.sqlite3"))
    runtime = app.state.runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        res = await client.get("/api/signals/scan?limit=200")
        assert res.status_code == 200
        assert "error" in res.json() or res.json().get("ok") is False

        runtime._chart_candles = make_candles(200, tf_ms=MIN, step=0.02)
        runtime._refresh_signal_scan()
        state = (await client.get("/api/state")).json()
        assert state["signal_scan"]["windows"] > 100
        assert state["signal_scan"]["signals"] == 0

        named = await client.get("/api/signals/scan")
        assert named.json()["ok"] is True
        assert named.json()["threshold_pct"] == runtime.settings.min_move_pct
