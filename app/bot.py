"""Bot runtime: wires market data feed -> strategy engine -> broker.

The runtime is the only stateful, asyncio part of the project. It keeps the
engine, the broker and the feed in sync, persists everything to SQLite and
broadcasts snapshots to the web panel.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

import httpx

from .backtest import run_backtest
from .brokers import BrokerError, PaperBroker, make_broker
from .config import BotSettings
from .db import Database
from .feed import BaseFeed, make_feed
from .fib import fib_grid
from .history import (HistoryRequest, describe as describe_candles, download_candles,
                      downloaded_filename, read_csv, save_file, write_csv)
from .models import Action, Candle, Fill, now_ms
from .strategy import StrategyEngine, scan_signals

STATUS_STOPPED = "stopped"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_TARGET = "target_reached"
STATUS_LOSS = "stopped_by_stop_loss"
DAY_MS = 24 * 60 * 60 * 1000
STATUS_ERROR = "error"


class BotRuntime:
    def __init__(self, db: Database, settings: BotSettings) -> None:
        self.db = db
        self.settings = settings
        self.engine = StrategyEngine(settings)
        self.broker = None
        self.feed: Optional[BaseFeed] = None
        self.status = STATUS_STOPPED
        self.paused = False
        self.error = ""
        self.balance: Dict[str, Any] = {}
        self.exchange_position = 0.0
        self.started_at: Optional[int] = None
        self.last_loop_ts: Optional[int] = None
        self.loop_count = 0
        self.logs: Deque[Dict[str, Any]] = deque(maxlen=400)
        self.listeners: List[asyncio.Queue] = []
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self._lock = asyncio.Lock()
        self._chart_candles: List[Candle] = []
        self._last_status_log = 0.0
        #: результат сканера сигналов: сколько раз стратегия сработала бы на истории
        self.signal_scan: Dict[str, Any] = {}
        #: в журнал про «входов нет» пишем один раз на каждые настройки, а не каждый цикл
        self._silence_warned_for: str = ""
        #: фоновые задания бэктеста (загрузка истории — долгая операция)
        self.backtest_jobs: Dict[str, Dict[str, Any]] = {}
        self._backtest_tasks: Dict[str, asyncio.Task] = {}
        self._managed_orders: Dict[str, Optional[Dict[str, Any]]] = {"add": None, "tp": None}
        self._load_state()

    # ------------------------------------------------------------------ state
    @property
    def desired_running(self) -> bool:
        """Пользователь нажимал «Старт» и не нажимал «Стоп»."""
        return bool(self.db.kv_get("desired_running", False))

    def _set_desired_running(self, value: bool) -> None:
        self.db.kv_set("desired_running", bool(value))

    def should_autostart(self) -> bool:
        """Нужно ли поднимать бота при запуске процесса (после перезагрузки сервера).

        Запускаем, если пользователь ранее нажал «Старт» и не останавливал бота,
        либо если в базе есть незакрытый цикл — так открытая позиция не остаётся
        без управления.
        """
        if self.db.kv_get("desired_running") is None:
            return bool(self.engine.active_cycles)  # первый запуск: не оставлять позицию без контроля
        return self.desired_running

    def _load_state(self) -> None:
        state = self.db.load_runtime_state()
        self.engine.restore(state.get("cycles_raw") or state.get("cycle_raw"))
        stored_orders = self.db.kv_get("managed_orders", {}) or {}
        self._managed_orders = {"add": stored_orders.get("add"), "tp": stored_orders.get("tp")}
        self.engine.realized_pnl = float(state.get("realized_pnl", 0.0))
        self.engine.cycles_total = int(state.get("cycles_total", 0))
        self.engine.wins = int(state.get("wins", 0))
        self.engine.losses = int(state.get("losses", 0))
        if self.engine.active_cycles:
            self.status = STATUS_PAUSED
            self.paused = True

    def _persist_state(self) -> None:
        snap = self.engine.snapshot()
        self.db.save_runtime_state({
            "cycle_raw": snap["cycle_raw"],
            "cycles_raw": snap["cycles_raw"],
            "realized_pnl": snap["realized_pnl"],
            "cycles_total": snap["cycles_total"],
            "wins": snap["wins"],
            "losses": snap["losses"],
            "updated_at": now_ms(),
        })
        for cycle in self.engine.active_cycles:
            self.db.save_cycle(cycle)
            for pos in cycle.positions:
                self.db.save_position(cycle, pos)
        self.db.kv_set("managed_orders", self._managed_orders)

    # ------------------------------------------------------------------- logs
    def log(self, level: str, message: str, *, persist: bool = True) -> None:
        entry = {"ts": now_ms(), "level": level, "message": message}
        self.logs.append(entry)
        if persist:
            self.db.log(level, message)
        self.broadcast({"type": "log", "log": entry})
        if level in ("error", "warn") or persist:
            print(f"[{time.strftime('%H:%M:%S')}] {level.upper():5} {message}", flush=True)

    def broadcast(self, payload: Dict[str, Any]) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.listeners:
            self.listeners.remove(q)

    async def _notify(self, text: str) -> None:
        token = self.settings.notify_telegram_token
        chat = self.settings.notify_telegram_chat
        if not token or not chat:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": text})
        except Exception:
            pass

    # ------------------------------------------------------------- lifecycle
    async def start(self, settings: Optional[BotSettings] = None, *,
                    restart: bool = False) -> Dict[str, Any]:
        """Запустить бота.

        ``restart=True`` — пересобрать подключение к бирже (скорость демо-рынка,
        таймфрейм, ключи и т.п. нельзя поменять «на живую»): останавливаем текущий
        цикл и стартуем заново с новыми настройками.
        """
        async with self._lock:
            if settings is not None:
                self.settings = settings
            if self.status == STATUS_RUNNING and not restart:
                return {"ok": True, "status": self.status, "message": "бот уже запущен"}
            self.error = ""
            try:
                await self._teardown()
                self.feed = make_feed(
                    self.settings,
                    start_price=self.db.kv_get("demo_last_price") if self.settings.synthetic_data else None,
                )
                self.broker = make_broker(self.settings)
                timeout = self._net_timeout()
                await asyncio.wait_for(self.broker.start(), timeout=timeout)
                if isinstance(self.broker, PaperBroker):
                    self.broker.restore_cycle(self.engine.active_cycles)
                    self._managed_orders = {"add": None, "tp": None}
                    self.db.kv_set("managed_orders", self._managed_orders)
                if self.settings.trade_mode == "live":
                    # Проверяем чтение баланса и разрешения API до запуска торгового цикла.
                    self.balance = await asyncio.wait_for(self.broker.balance(), timeout=timeout)
                    if float(self.balance.get("total") or 0.0) <= 0:
                        raise BrokerError("на Gate.io нулевой доступный баланс — live-торговля не запущена")
                else:
                    # Paper starts with a baseline balance too, so deposit-load
                    # estimates are available before its first periodic refresh.
                    self.balance = await asyncio.wait_for(self.broker.balance(), timeout=timeout)
                # сетевые запросы к бирже ограничены по времени: иначе «зависшая»
                # биржа оставляла бота и панель без ответа на неопределённый срок
                history = await asyncio.wait_for(self.feed.history(self.settings.candle_lookback),
                                                 timeout=timeout)
                self._chart_candles = list(history)
                # keep the engine history but do not re-run old signals
                self.engine.set_history(history)
                if not self.engine.active_cycles:
                    self.engine.stopped = False
                    self.engine.stop_reason = ""
                self.paused = False
                self.status = STATUS_RUNNING
                self._set_desired_running(True)
                self.started_at = now_ms()
                self._stop_evt = asyncio.Event()
                self._task = asyncio.create_task(self._run_loop())
                mode = "LIVE" if self.settings.trade_mode == "live" else "PAPER"
                money_label = ("РЕАЛЬНЫЕ ДЕНЬГИ" if mode == "LIVE" else "виртуальные сделки")
                movement_label = (f"автопоиск движения ≥ {self.settings.min_move_pct:g}%"
                                  if self.settings.entry_mode == "swing"
                                  else f"импульс {self.settings.candles_count} свечей, "
                                       f"мин. движение {self.settings.min_move_pct:g}%")
                self.log("info", f"Бот запущен ({mode} — {money_label}, "
                                 f"{self.settings.data_source_label()}, {self.settings.symbol}, "
                                 f"{self.settings.timeframe}, {movement_label}, "
                                 f"цель фибо {self.settings.fib_goal_pct:g}%, "
                                 f"мин. профит {self.settings.min_profit_pct:g}%)")
                for cycle in self.engine.active_cycles:
                    self.log("warn", f"восстановлен незакрытый цикл {cycle.id} ({cycle.side}): "
                                     f"{len(cycle.positions)} поз., "
                                     f"средняя {cycle.avg_entry:g}, цель {cycle.target:g}")
                target = self.settings.total_profit_target_usd
                if target and self.engine.realized_pnl >= target:
                    self.log("warn", f"общий профит {self.engine.realized_pnl:.2f}$ уже не меньше цели "
                                     f"{target:g}$ — увеличьте цель или сбросьте статистику, "
                                     f"иначе бот остановится после первого цикла")
                await self._reconcile(first=True)
                await self._sync_cycle_orders()
                return {"ok": True, "status": self.status}
            except asyncio.TimeoutError:
                self.status = STATUS_ERROR
                self.error = (f"биржа не ответила за {self._net_timeout():g} с — бот не запущен. "
                              f"Проверьте доступ с сервера: "
                              f"curl -s -m 10 https://api.gateio.ws/api/v4/spot/time")
                self.log("error", self.error)
                return {"ok": False, "error": self.error}
            except Exception as exc:
                # если источник данных умеет объяснить ошибку по-человечески
                # (нет связи с биржей, гео-ограничение) — показываем её
                friendly = (self.feed.last_error if self.feed is not None else "") or str(exc)
                self.status = STATUS_ERROR
                self.error = friendly
                self.log("error", f"не удалось запустить бота: {friendly}")
                return {"ok": False, "error": friendly}

    @staticmethod
    def _net_timeout() -> float:
        """Сколько ждать биржу при запуске (секунды)."""
        try:
            return max(5.0, float(os.environ.get("PLANB_FEED_TIMEOUT", "45")))
        except ValueError:
            return 45.0

    async def _teardown(self) -> None:
        if self._task is not None:
            self._stop_evt.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self.feed is not None:
            try:
                await self.feed.close()
            except Exception:
                pass
            self.feed = None
        if self.broker is not None:
            try:
                await self.broker.close()
            except Exception:
                pass
            self.broker = None

    async def stop(self, *, reason: str = "остановлен пользователем") -> Dict[str, Any]:
        async with self._lock:
            if self.settings.allow_opposite_cycles and self.engine.active_cycles:
                return {"ok": False, "error": "сначала закройте активные циклы кнопкой «Закрыть всё»; параллельные paper-циклы требуют наблюдения"}
            self.paused = True
            if not await self._cancel_managed_order("add"):
                return {"ok": False, "error": "биржа не подтвердила отмену доливочного ордера"}
            await self._sync_cycle_orders()  # TP остаётся установленным на обновлённый объём
            if (self.engine.cycle is not None and self.settings.trade_mode == "live"
                    and not self._managed_orders.get("tp")):
                return {"ok": False, "error": "позиция открыта, но TP не подтверждён; бот оставлен запущенным"}
            if self.engine.cycle is None and not await self._cancel_managed_order("tp"):
                return {"ok": False, "error": "биржа не подтвердила отмену TP-ордера"}
            self._persist_state()
            await self._teardown()
            self._set_desired_running(False)
            self.status = STATUS_STOPPED
            self.log("info", f"Бот остановлен: {reason}")
            if self.engine.cycle is not None and self._managed_orders.get("tp"):
                self.log("warn", "на бирже оставлен активный TP открытой позиции; доливы отменены. "
                                  "Не удаляйте TP вручную, если позиция ещё открыта.")
            return {"ok": True, "status": self.status}

    async def pause(self) -> Dict[str, Any]:
        self.paused = True
        if self.status == STATUS_RUNNING:
            self.status = STATUS_PAUSED
        await self._cancel_managed_order("add")
        await self._sync_cycle_orders()  # сохранить/обновить TP на полный объём
        self.log("info", "Пауза: новые сделки и доливы не открываются, цель по открытому циклу "
                         "продолжает отслеживаться")
        return {"ok": True, "status": self.status}

    async def resume(self) -> Dict[str, Any]:
        self.paused = False
        if self.status == STATUS_PAUSED:
            self.status = STATUS_RUNNING
        self.engine.resume()
        self.log("info", "Продолжение работы")
        return {"ok": True, "status": self.status}

    async def close_all(self, reason: str = "ручное закрытие") -> Dict[str, Any]:
        """Panic button: close every active cycle at the market."""
        if not self.engine.active_cycles or self.broker is None:
            return {"ok": False, "error": "нет открытых позиций"}
        try:
            # Отменяем возможные заявки и в parallel mode: настройку могли включить
            # прямо перед нажатием, пока старый TP/add ещё не был снят очередным тиком.
            if not await self._cancel_managed_order("add"):
                return {"ok": False, "error": "биржа не подтвердила отмену доливочного ордера"}
            if not await self._cancel_managed_order("tp"):
                return {"ok": False, "error": "биржа не подтвердила отмену TP-ордера"}
            if not self.engine.active_cycles:
                return {"ok": True, "message": "TP исполнился при отмене заявки"}
            price = await self.broker.last_price() or self.engine.last_price
            closed_cycles = []
            for cycle in list(self.engine.active_cycles):
                fill = await self.broker.close_position(cycle.side, cycle.total_qty, price,
                                                        ts=now_ms(), reason=reason)
                closed = self.engine.close_cycle(fill.price, fill.ts or now_ms(), reason,
                                                 fee=fill.fee, cycle_id=cycle.id)
                closed_cycles.append(closed)
                self.db.save_cycle(closed)
                for pos in closed.positions:
                    self.db.save_position(closed, pos)
            self._managed_orders = {"add": None, "tp": None}
            self._persist_state()
            pnl = sum(cycle.pnl for cycle in closed_cycles)
            self.log("warn", f"Все циклы закрыты вручную по рынку: {pnl:+.4f}$, циклов {len(closed_cycles)}")
            await self._notify(f"PlanB: ручное закрытие всех циклов, результат {pnl:+.4f}$")
            return {"ok": True, "pnl": pnl, "price": price, "cycles": len(closed_cycles)}
        except Exception as exc:
            self.log("error", f"не удалось закрыть позиции: {exc}")
            return {"ok": False, "error": str(exc)}

    async def reset_cycle(self, reason: str = "цикл сброшен вручную") -> Dict[str, Any]:
        """Close active cycle(s) in accounting after manual exchange closure."""
        cycles = list(self.engine.active_cycles)
        if not cycles:
            return {"ok": False, "error": "нет незакрытого цикла"}
        if not await self._cancel_managed_order("add"):
            return {"ok": False, "error": "биржа не подтвердила отмену доливочного ордера"}
        if not await self._cancel_managed_order("tp"):
            return {"ok": False, "error": "биржа не подтвердила отмену TP-ордера"}
        cycles = list(self.engine.active_cycles)
        if not cycles:
            return {"ok": True, "message": "TP исполнился при отмене заявки"}
        price = self.engine.last_price or cycles[0].avg_entry
        closed_cycles = []
        for cycle in list(self.engine.active_cycles):
            closed = self.engine.close_cycle(price, now_ms(), reason, cycle_id=cycle.id)
            closed_cycles.append(closed)
            self.db.save_cycle(closed)
            for pos in closed.positions:
                self.db.save_position(closed, pos)
        self._persist_state()
        self.paused = False
        if self.status == STATUS_PAUSED:
            self.status = STATUS_RUNNING
        pnl = sum(cycle.pnl for cycle in closed_cycles)
        self.log("warn", f"Сброшено циклов: {len(closed_cycles)}, оценка результата {pnl:+.4f}$ "
                         f"(по {price:g}; комиссии выхода не пересчитаны)")
        return {"ok": True, "price": price, "pnl": pnl, "cycles": len(closed_cycles)}

    async def reset_stats(self) -> Dict[str, Any]:
        """Reset all-time counters without deleting the detailed trade history."""
        self.engine.realized_pnl = 0.0
        self.engine.cycles_total = 0
        self.engine.wins = 0
        self.engine.losses = 0
        self.engine.signals_found = 0
        self.engine.closed_cycles = []
        self.engine.resume()
        self.db.kv_set("stats_reset_ts", now_ms())
        self._persist_state()
        self.log("info", "Общая статистика сброшена; история циклов и сделок сохранена")
        self.broadcast({"type": "snapshot", "snapshot": self.snapshot()})
        return {"ok": True}

    async def reset_daily_stats(self) -> Dict[str, Any]:
        """Start a fresh rolling 24-hour statistics window; history is retained."""
        reset_ts = now_ms()
        self.db.kv_set("daily_stats_reset_ts", reset_ts)
        self.log("info", "Статистика за сутки сброшена; история циклов и сделок сохранена")
        self.broadcast({"type": "snapshot", "snapshot": self.snapshot()})
        return {"ok": True, "reset_ts": reset_ts}

    def _daily_stats(self) -> Dict[str, Any]:
        now = now_ms()
        reset_ts = int(self.db.kv_get("daily_stats_reset_ts", 0) or 0)
        since_ts = max(now - DAY_MS, reset_ts)
        return {**self.db.cycle_stats_since(since_ts), "since_ts": since_ts,
                "reset_at": reset_ts or None, "window_hours": 24}

    async def apply_settings(self, new_settings: BotSettings) -> Dict[str, Any]:
        """Save settings; if the bot runs - restart so they take effect."""
        was_running = self.status in (STATUS_RUNNING, STATUS_PAUSED)
        old = self.settings
        market_changed = (
            new_settings.data_source != old.data_source
            or new_settings.symbol != old.symbol
            or new_settings.market_type != old.market_type
            or new_settings.timeframe != old.timeframe
            or new_settings.trade_mode != old.trade_mode
        )
        if (old.allow_opposite_cycles and not new_settings.allow_opposite_cycles
                and len(self.engine.active_cycles) > 1):
            message = "нельзя отключить встречные циклы, пока открыты обе стороны; закройте один цикл"
            return {"ok": False, "error": message, "restarted": False,
                    "connection_changed": False, "paused_after_restart": self.paused}
        if market_changed and self.engine.active_cycles:
            message = ("нельзя сменить рынок, таймфрейм или режим торговли при открытом цикле: "
                       "сначала закройте позицию и отмените отложенные заявки")
            self.log("error", message)
            return {"ok": False, "error": message, "restarted": False,
                    "connection_changed": False, "paused_after_restart": self.paused}
        # Для этих настроек мало поменять поле: нужен новый фид/брокер. Раньше такие
        # изменения (например скорость демо-рынка) молча не применялись, потому что
        # start() выходил с «бот уже запущен» — теперь перезапускаем явно.
        connection_changed = (
            market_changed
            or new_settings.demo_speed != old.demo_speed
            or new_settings.poll_interval_sec != old.poll_interval_sec
            or new_settings.api_key != old.api_key
            or new_settings.api_secret != old.api_secret
            or new_settings.sandbox != old.sandbox
            or new_settings.leverage != old.leverage
            or new_settings.margin_mode != old.margin_mode
        )
        self.settings = new_settings
        self.db.save_settings(new_settings)
        self.engine.settings = new_settings
        self.log("info", "Настройки сохранены"
                         + (" — бот перезапущен, чтобы они вступили в силу"
                            if was_running and connection_changed else ""))
        if was_running and not connection_changed:
            # пороги, объём и цели движок читает на каждом тике: перезапуск не нужен
            movement_label = ("автоматическое число свечей" if new_settings.entry_mode == "swing"
                              else f"{new_settings.candles_count} свечей в импульсе")
            self.log("info", f"Новые параметры применены без перезапуска: {movement_label}, "
                             f"мин. движение {new_settings.min_move_pct:g}%, мин. профит "
                             f"{new_settings.min_profit_pct:g}%")
        if was_running and connection_changed:
            await self.start(new_settings, restart=True)
            if market_changed and self.engine.active_cycles:
                # рынок изменился, а цикл открыт: не торгуем автоматически на новом
                # рынке, пока пользователь не проверит позиции
                self.paused = True
                self.status = STATUS_PAUSED
                self.log("warn", "изменён рынок/символ/таймфрейм/режим торговли, но остался "
                                 "незакрытый цикл — бот на паузе. Проверьте позиции и нажмите "
                                 "«Продолжить» либо «Закрыть всё»/«Сбросить цикл».")
        elif was_running and new_settings.allow_opposite_cycles != old.allow_opposite_cycles:
            # При смене режима сразу убрать прежние managed orders, не ждать следующего тика.
            await self._sync_cycle_orders()
        return {"ok": True, "restarted": bool(was_running and connection_changed),
                "connection_changed": connection_changed, "paused_after_restart": self.paused}

    # ------------------------------------------------------------------- loop
    async def _run_loop(self) -> None:
        assert self.feed is not None and self.broker is not None
        backoff = 1.0
        while not self._stop_evt.is_set():
            try:
                new_candles, price = await self.feed.poll()
                self.loop_count += 1
                self.last_loop_ts = now_ms()
                live_candle = getattr(self.feed, "live", None)
                # Feed.poll returns newly closed candles before the current tick.
                # Process them chronologically: handling the live tick first can
                # open a cycle, then replay an older candle against its fresh TP.
                for candle in new_candles:
                    self._remember_candle(candle)
                    await self.broker.update_market(candle.close, candle)
                    actions = self.engine.on_candle(candle)
                    await self._poll_managed_orders()
                    if self.paused:
                        actions = [a for a in actions if a.kind == "close_all"]
                    await self._execute(actions)
                    self._track_cycle_exposure(candle.close)
                if price > 0:
                    await self.broker.update_market(price, live_candle)
                    # Strategy updates the grid from the current tick before
                    # synchronizing newly derived add/TP orders.
                    actions = self.engine.on_tick(price, live_candle)
                    await self._poll_managed_orders()
                    await self._execute(actions)
                    self._track_cycle_exposure(price)
                await self._sync_cycle_orders()
                # Give paper orders one price-only check after submission. Do not
                # reuse this candle's old high/low: those wicks may predate the order.
                if isinstance(self.broker, PaperBroker) and price > 0:
                    await self.broker.update_market(price)
                    await self._poll_managed_orders()
                if self.loop_count % 5 == 0:
                    await self._refresh_account()
                if self.loop_count % 5 == 0:
                    self._persist_state()
                if self.loop_count % 20 == 0:
                    await self._refresh_chart()
                self.broadcast({"type": "snapshot", "snapshot": self.snapshot()})
                if self.engine.stopped:
                    self.status = (STATUS_TARGET if "профит" in self.engine.stop_reason
                                   else STATUS_LOSS)
                    await self._refresh_account()
                    self.log("info", f"Бот остановлен стратегией: {self.engine.stop_reason}")
                    await self._notify(f"PlanB: {self.engine.stop_reason}")
                    self._stop_evt.set()
                    break
                backoff = 1.0
                await asyncio.sleep(max(1.0, self.settings.poll_interval_sec))
            except asyncio.CancelledError:
                break
            except Exception as exc:  # network / exchange errors must not kill the bot
                self.error = str(exc)
                self.log("error", f"ошибка в цикле бота: {exc}")
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)
        self._persist_state()

    def _remember_candle(self, candle: Candle) -> None:
        if self._chart_candles and self._chart_candles[-1].ts == candle.ts:
            self._chart_candles[-1] = candle
        elif not self._chart_candles or candle.ts > self._chart_candles[-1].ts:
            self._chart_candles.append(candle)
        if len(self._chart_candles) > 1500:
            self._chart_candles = self._chart_candles[-1500:]

    async def _refresh_chart(self) -> None:
        if self.feed is None:
            return
        try:
            limit = self.settings.candle_lookback
            candles = await self.feed.history(limit)
            # the demo feed can also show the still forming candle
            extra = getattr(self.feed, "chart_candles", None)
            if callable(extra):
                candles = list(extra(limit))
            if candles:
                self._chart_candles = list(candles)
        except Exception:
            pass
        self._refresh_signal_scan()

    def _refresh_signal_scan(self) -> None:
        """Посчитать, сколько сигналов дала бы стратегия на загруженной истории.

        Нужно, чтобы панель всегда могла объяснить «почему бот молчит»: показывает
        размер реальных движений против порога и самые близкие случаи.
        """
        try:
            candles = self._chart_candles
            scan_window = 3 if self.settings.entry_mode == "swing" else self.settings.candles_count
            if len(candles) <= scan_window:
                return
            scan = scan_signals(candles, self.settings)
            scan["updated_at"] = now_ms()
            self.signal_scan = scan
            self._warn_about_silence(scan)
        except Exception as exc:      # диагностика не должна ломать торговлю
            self.signal_scan = {"error": str(exc)}

    def _warn_about_silence(self, scan: Dict[str, Any]) -> None:
        """Один раз на настройки объяснить в журнале, почему входов нет.

        Иначе «бот молчит» остаётся без ответа: пользователь видит только тишину.
        Пишем конкретные цифры и что именно уменьшить.
        """
        if not scan.get("enough_data") or scan.get("error"):
            return
        count_key = (self.settings.candles_count if self.settings.entry_mode == "impulse" else "auto")
        key = (f"{self.settings.data_source}|{self.settings.timeframe}|{self.settings.entry_mode}|{count_key}|"
               f"{self.settings.min_move_pct}|{self.settings.min_profit_pct}|{self.settings.direction}")
        if scan.get("signals"):
            self._silence_warned_for = ""      # сигналы снова есть — предупредим при новой тишине
            return
        if scan.get("windows", 0) < 30 or key == self._silence_warned_for:
            return
        self._silence_warned_for = key
        hours = max(1, round(scan.get("minutes", 0) / 60))
        self.log("warn",
                 f"Входов не было: за {scan['windows']} окон истории (≈{hours} ч, "
                 f"{scan['candles']} свечей {scan['timeframe']}) движения в окне настроек были "
                 f"от {scan['move_median_pct']}% (медиана) до {scan['move_max_pct']}% (максимум), "
                 f"а порог «Минимальное движение цены» — {scan['threshold_pct']}%. "
                 f"Похоже, порог выше рынка: попробуйте {scan['suggested_threshold_pct']}% "
                 f"и «Минимальный профит» не больше {round(scan['suggested_threshold_pct'] / 2, 2)}% "
                 f"(сейчас {scan['min_profit_pct']}%), либо таймфрейм крупнее.")

    async def scan_signals_now(self, limit: int = 1000) -> Dict[str, Any]:
        """Сканер по кнопке: тянем свежую историю и считаем сигналы."""
        limit = max(100, min(int(limit or 1000), 1500))
        candles: List[Candle] = []
        if self.feed is not None:
            try:
                candles = await asyncio.wait_for(self.feed.history(limit),
                                                 timeout=self._net_timeout())
            except Exception as exc:
                return {"ok": False, "error": f"не удалось получить свечи: {exc}"}
        if not candles:
            candles = list(self._chart_candles)[-limit:]
        if not candles:
            return {"ok": False, "error": "нет свечей для проверки — запустите бота"}
        scan = scan_signals(candles, self.settings)
        scan["updated_at"] = now_ms()
        self.signal_scan = scan
        self.broadcast({"type": "snapshot", "snapshot": self.snapshot()})
        return {"ok": True, **scan}

    def _track_cycle_exposure(self, mark_price: float) -> None:
        if mark_price <= 0:
            return
        is_futures = self.settings.market_type == "futures"
        leverage = max(1.0, float(self.settings.leverage or 1.0))
        try:
            equity = float((self.balance or {}).get("total") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        for cycle in self.engine.active_cycles:
            notional = sum(p.qty * mark_price for p in cycle.open_positions)
            margin = notional / leverage if is_futures else notional
            cycle.peak_notional_usd = max(cycle.peak_notional_usd, notional)
            cycle.peak_margin_usd = max(cycle.peak_margin_usd, margin)
            if equity > 0:
                cycle.max_deposit_load_pct = max(cycle.max_deposit_load_pct, margin / equity * 100.0)

    async def _refresh_account(self) -> None:
        if self.settings.synthetic_data and self.engine.last_price > 0:
            # чтобы демо-рынок не «прыгал» после перезапуска
            self.db.kv_set("demo_last_price", self.engine.last_price)
        if self.broker is None:
            return
        try:
            self.balance = await self.broker.balance()
        except Exception as exc:
            self.log("warn", f"не удалось получить баланс: {exc}")
        await self._reconcile()

    async def _reconcile(self, *, first: bool = False) -> None:
        """Compare our idea of the position with the exchange one (live mode)."""
        if self.broker is None or self.settings.trade_mode != "live":
            return
        cycles = self.engine.active_cycles
        total = 0.0
        for side in ("long", "short"):
            try:
                total += abs(await self.broker.exchange_position(side))
            except Exception:
                return
        self.exchange_position = total
        expected = sum(cycle.total_qty for cycle in cycles)
        if expected and total and abs(total - expected) / expected > 0.05:
            self.log("warn", f"размер позиции на бирже ({total:g}) не совпадает с расчётным ({expected:g})")
        elif expected and not total:
            self.log("error", "в базе есть незакрытый цикл, а на бирже позиции нет — бот поставлен "
                              "на паузу: проверьте, не закрыта ли позиция вручную, и нажмите "
                              "«Сбросить цикл» на вкладке «Циклы и сделки»")
            self.paused = True
            if self.status == STATUS_RUNNING:
                self.status = STATUS_PAUSED
        elif not expected and total:
            self.log("error", "на бирже есть позиция, о которой бот не знает — "
                              "бот поставлен на паузу, закройте позицию вручную")
            self.paused = True
            if self.status == STATUS_RUNNING:
                self.status = STATUS_PAUSED

    # ----------------------------------------------------------------- actions
    async def _execute(self, actions: List[Action]) -> None:
        if not actions or self.broker is None:
            return
        for action in actions:
            try:
                if action.kind == "add" and not self.settings.allow_opposite_cycles:
                    # В обычном режиме доливы ведутся отложенным limit-ордером.
                    continue
                if action.kind in ("entry", "add"):
                    if self.paused:
                        continue
                    fill = await self.broker.open_position(action.side, action.volume_usd,
                                                           action.ref_price, action.ts)
                    self.engine.apply_fill(action, fill)
                    cycle = self.engine.get_cycle(action.cycle_id, side=action.side)
                    self._persist_state()
                    if cycle:
                        self.db.save_cycle(cycle)
                        self.db.save_position(cycle, cycle.positions[-1])
                    label = ""
                    if cycle is not None and cycle.positions:
                        mult = cycle.positions[-1].multiplier
                        if mult > 1:
                            label = f" x{mult:g}"
                    kind = "Вход" if action.kind == "entry" else f"Долив #{action.position_idx}{label}"
                    extra = (f" Средняя {cycle.avg_entry:g}, цель {cycle.target:g}, "
                             f"профит до цели {cycle.profit_pct_to(cycle.target):.3f}%"
                             if cycle is not None else "")
                    self.log("trade", f"{kind} {action.side} по {fill.price:g}, объём "
                                      f"{fill.qty * fill.price:.2f}$ ({fill.qty:g}), "
                                      f"{action.reason}.{extra}")
                    await self._notify(f"PlanB: {kind} {action.side} {fill.price:g}.{extra}")
                elif action.kind == "close_all":
                    cycle = self.engine.get_cycle(action.cycle_id, side=action.side)
                    if cycle is None:
                        continue
                    if action.reason != "стоп-лосс" and not self.settings.allow_opposite_cycles:
                        # Take-profit исполняется выставленной заранее лимитной заявкой;
                        # не дублируем её market-закрытием по следующему тiku.
                        self.log("info", f"цель {cycle.target:g} затронута — ожидаю исполнения "
                                          f"лимитного TP-ордера")
                        continue
                    if not self.settings.allow_opposite_cycles:
                        if not await self._cancel_managed_order("add"):
                            self.paused = True
                            self.status = STATUS_PAUSED
                            continue
                        if not await self._cancel_managed_order("tp"):
                            self.paused = True
                            self.status = STATUS_PAUSED
                            continue
                    cycle = self.engine.get_cycle(action.cycle_id, side=action.side)
                    if cycle is None:
                        continue
                    fill = await self.broker.close_position(cycle.side, cycle.total_qty,
                                                            action.ref_price, action.ts,
                                                            reason=action.reason)
                    closed = self.engine.close_cycle(fill.price, fill.ts or now_ms(),
                                                     action.reason, fee=fill.fee, cycle_id=cycle.id)
                    self._persist_state()
                    self.db.save_cycle(closed)
                    for pos in closed.positions:
                        self.db.save_position(closed, pos)
                    self.log("trade", f"Цикл закрыт по {fill.price:g} ({action.reason}): "
                                      f"{closed.pnl:+.4f}$ ({closed.pnl_pct:+.3f}% от "
                                      f"{closed.invested_volume_usd:.2f}$), позиций "
                                      f"{len(closed.positions)}. Общий профит "
                                      f"{self.engine.realized_pnl:+.4f}$ из "
                                      f"{self.settings.total_profit_target_usd:g}$")
                    await self._notify(f"PlanB: профит {closed.pnl:+.4f}$, всего "
                                       f"{self.engine.realized_pnl:+.4f}$")
                    await self._refresh_account()
            except BrokerError as exc:
                cycle = self.engine.get_cycle(action.cycle_id, side=action.side)
                if action.kind == "entry" and cycle is not None and not cycle.positions:
                    self.engine.discard_empty_cycle(cycle.id)
                    self._persist_state()
                self.log("error", f"ошибка исполнения ({action.kind}): {exc}")
            except Exception as exc:  # pragma: no cover
                cycle = self.engine.get_cycle(action.cycle_id, side=action.side)
                if action.kind == "entry" and cycle is not None and not cycle.positions:
                    self.engine.discard_empty_cycle(cycle.id)
                    self._persist_state()
                self.log("error", f"непредвиденная ошибка исполнения: {exc}")
        if self.broker is not None:
            await self._sync_cycle_orders()

    async def _cancel_managed_order(self, role: str) -> bool:
        meta = self._managed_orders.get(role)
        if not meta or self.broker is None:
            return True
        try:
            report = await self.broker.cancel_pending_order(meta["id"], side=meta["side"])
        except BrokerError as exc:
            self.log("error", f"не удалось отменить {role}-ордер {meta.get('id')}: {exc}")
            return False
        status = str(report.get("status") or "open").lower()
        if status not in ("closed", "canceled", "cancelled", "expired", "rejected"):
            self.log("error", f"биржа не подтвердила отмену {role}-ордера {meta.get('id')} "
                              f"(статус {status}); новый ордер не выставлен")
            return False
        if role == "add":
            await self._consume_add_report(report, meta)
        elif float(report.get("filled_qty") or 0.0) > 0:
            await self._complete_take_profit(report, meta)
        if self._managed_orders.get(role) is meta:
            self._managed_orders[role] = None
        self.db.kv_set("managed_orders", self._managed_orders)
        return True

    async def _poll_managed_orders(self) -> None:
        if self.broker is None:
            return
        if self.settings.allow_opposite_cycles:
            for role in ("add", "tp"):
                await self._cancel_managed_order(role)
            return
        # Долив сначала: если долив и TP успели исполниться между опросами,
        # закрытие будет рассчитано уже на полный фактический объём позиции.
        for role in ("add", "tp"):
            meta = self._managed_orders.get(role)
            if not meta:
                continue
            try:
                report = await self.broker.order_status(meta["id"], side=meta["side"])
            except BrokerError as exc:
                self.log("warn", f"не удалось проверить {role}-ордер {meta.get('id')}: {exc}")
                continue
            status = str(report.get("status") or "open").lower()
            if role == "add":
                await self._consume_add_report(report, meta)
                if status in ("closed", "canceled", "cancelled", "expired", "rejected"):
                    if self._managed_orders.get(role) is meta:
                        self._managed_orders[role] = None
                    self.db.kv_set("managed_orders", self._managed_orders)
            elif status in ("closed", "canceled", "cancelled", "expired", "rejected"):
                if float(report.get("filled_qty") or 0.0) > 0:
                    await self._complete_take_profit(report, meta)
                elif self._managed_orders.get(role) is meta:
                    self._managed_orders[role] = None
                    self.db.kv_set("managed_orders", self._managed_orders)

    async def _consume_add_report(self, report: Dict[str, Any], meta: Dict[str, Any]) -> None:
        cycle = self.engine.cycle
        if cycle is None or cycle.id != meta.get("cycle_id"):
            return
        cumulative_qty = float(report.get("filled_qty") or 0.0)
        applied_qty = float(meta.get("applied_qty") or 0.0)
        delta_qty = cumulative_qty - applied_qty
        if delta_qty <= max(1e-12, cumulative_qty * 1e-9):
            return
        average = float(report.get("average") or meta.get("price") or 0.0)
        cumulative_cost = float(report.get("filled_cost") or cumulative_qty * average)
        delta_cost = cumulative_cost - float(meta.get("applied_cost") or 0.0)
        delta_price = delta_cost / delta_qty if delta_cost > 0 else average
        cumulative_fee = float(report.get("fee") or 0.0)
        delta_fee = max(0.0, cumulative_fee - float(meta.get("applied_fee") or 0.0))
        fill = Fill(price=delta_price, qty=delta_qty, fee=delta_fee,
                    ts=int(report.get("timestamp") or now_ms()), order_id=meta["id"])
        action = Action(kind="add", side=cycle.side, ref_price=delta_price,
                        volume_usd=delta_price * delta_qty, position_idx=int(meta["idx"]),
                        target=cycle.target, reason="исполнен лимитный долив", ts=fill.ts)
        self.engine.apply_fill(action, fill)
        meta["applied_qty"] = cumulative_qty
        meta["applied_cost"] = cumulative_cost
        meta["applied_fee"] = cumulative_fee
        self._persist_state()
        self.log("trade", f"Исполнен лимитный долив #{action.position_idx} {cycle.side} "
                          f"по средней цене {delta_price:g}, объём {delta_qty * delta_price:.2f}$")

    async def _complete_take_profit(self, report: Dict[str, Any], meta: Dict[str, Any]) -> None:
        cycle = self.engine.cycle
        if cycle is None or cycle.id != meta.get("cycle_id"):
            return
        if not await self._cancel_managed_order("add"):
            self.paused = True
            self.status = STATUS_PAUSED
            self.log("error", "TP исполнен, но доливочный ордер не отменён; цикл оставлен под "
                              "наблюдением, бот на паузе")
            return
        cycle = self.engine.cycle
        if cycle is None:
            return
        tp_qty = min(float(report.get("filled_qty") or 0.0), cycle.total_qty)
        if tp_qty <= 0:
            return
        tp_price = float(report.get("average") or meta.get("price") or cycle.target)
        tp_fee = float(report.get("fee") or 0.0)
        remaining = max(0.0, cycle.total_qty - tp_qty)
        combined_cost = tp_qty * tp_price
        combined_fee = tp_fee
        if remaining > max(1e-12, cycle.total_qty * 1e-9):
            # Защищаемся от частичного исполнения TP: закрываем остаток reduce-only
            # после того, как биржа подтвердила завершение/отмену лимитной заявки.
            try:
                market_fill = await self.broker.close_position(
                    cycle.side, remaining, self.engine.last_price or cycle.target,
                    ts=now_ms(), reason="остаток после лимитного TP")
            except BrokerError as exc:
                self.paused = True
                self.status = STATUS_PAUSED
                self.log("error", f"TP исполнен частично, остаток не закрыт — бот на паузе: {exc}")
                return
            combined_cost += market_fill.qty * market_fill.price
            combined_fee += market_fill.fee
            tp_qty += market_fill.qty
        fill_price = combined_cost / tp_qty if tp_qty > 0 else cycle.target
        closed = self.engine.close_cycle(fill_price, now_ms(), "тейк-профит по Фибо", fee=combined_fee)
        self._managed_orders["tp"] = None
        self._managed_orders["add"] = None
        self._persist_state()
        self.db.save_cycle(closed)
        for pos in closed.positions:
            self.db.save_position(closed, pos)
        self.log("trade", f"Тейк-профит исполнен по средней {fill_price:g}: "
                          f"{closed.pnl:+.4f}$, позиций {len(closed.positions)}")
        await self._notify(f"PlanB: TP по Фибо, результат {closed.pnl:+.4f}$")
        await self._refresh_account()

    async def _sync_cycle_orders(self) -> None:
        """Поддерживать необходимые отложенные ордера для стандартного одиночного цикла."""
        if self.broker is None:
            return
        if self.settings.allow_opposite_cycles:
            for role in ("add", "tp"):
                await self._cancel_managed_order(role)
            return
        cycle = self.engine.cycle
        if cycle is None or cycle.total_qty <= 0:
            add_ok = await self._cancel_managed_order("add")
            tp_ok = await self._cancel_managed_order("tp")
            if add_ok and tp_ok:
                self._managed_orders = {"add": None, "tp": None}
                self.db.kv_set("managed_orders", self._managed_orders)
            return

        add_plan = None if self.paused else self.engine.next_add_plan(self.engine.last_price)
        desired = {
            "add": ({"price": float(add_plan["price"]), "qty": 0.0,
                     "volume_usd": float(add_plan["volume_usd"]), "idx": int(add_plan["idx"]),
                     "side": cycle.side} if add_plan else None),
            "tp": {"price": float(cycle.target), "qty": float(cycle.total_qty),
                   "volume_usd": 0.0, "idx": 0, "side": cycle.side},
        }
        for role in ("add", "tp"):
            want = desired[role]
            current = self._managed_orders.get(role)
            if current and current.get("cycle_id") != cycle.id:
                if not await self._cancel_managed_order(role):
                    continue
                current = None
            if want is None:
                if current:
                    await self._cancel_managed_order(role)
                continue
            price = self.broker.normalize_price(want["price"])
            qty = (want["volume_usd"] / price if role == "add" and price > 0
                   else want["qty"])
            if current:
                if role == "add" and float(current.get("applied_qty") or 0.0) > 0 \
                        and current.get("status", "open") == "open":
                    # Частичный fill той же заявки: не отменяем остаток и не перескакиваем
                    # на следующий индекс, пока биржа не завершила текущий лимит.
                    continue
                same_price = abs(float(current.get("price") or 0.0) - price) <= max(price * 1e-9, 1e-10)
                same_qty = role == "add" or abs(float(current.get("requested_qty", current.get("qty")) or 0.0) - qty) <= max(qty * 1e-8, 1e-12)
                same_size = role == "tp" or abs(float(current.get("requested_volume_usd", current.get("volume_usd")) or 0.0) - want["volume_usd"]) < 1e-8
                if same_price and same_qty and same_size and current.get("status", "open") == "open":
                    continue
                if not await self._cancel_managed_order(role):
                    continue
                if self.engine.cycle is None:
                    return
                cycle = self.engine.cycle
            client_id = f"{cycle.id[:12]}-{role[:1]}-{want['idx']}-{now_ms() % 1_000_000}"
            try:
                placed = await self.broker.place_pending_order(
                    purpose=role, side=cycle.side, price=price,
                    volume_usd=want["volume_usd"], qty=qty, client_id=client_id)
            except BrokerError as exc:
                self.log("error" if role == "tp" else "warn",
                         f"не удалось выставить {('тейк-профит' if role == 'tp' else 'долив')} "
                         f"по {price:g}: {exc}")
                if role == "tp" and self.settings.trade_mode == "live":
                    self.paused = True
                    self.status = STATUS_PAUSED
                    self.log("error", "открытая позиция не защищена TP-ордером — новые сделки остановлены")
                continue
            self._managed_orders[role] = {
                **want, "id": str(placed["id"]), "cycle_id": cycle.id,
                "price": float(placed.get("price") or price),
                "qty": float(placed.get("base_qty") or qty),
                "volume_usd": float(placed.get("volume_usd") or want["volume_usd"]),
                "requested_qty": qty, "requested_volume_usd": want["volume_usd"],
                "status": str(placed.get("status") or "open"),
                "applied_qty": 0.0, "applied_cost": 0.0, "applied_fee": 0.0,
                "client_id": client_id,
            }
            self.db.kv_set("managed_orders", self._managed_orders)
            label = f"лимитный долив #{want['idx']}" if role == "add" else "лимитный TP"
            detail = (f"объём {want['volume_usd']:.2f}$" if role == "add"
                      else f"количество {qty:g}")
            self.log("info", f"Выставлен {label}: {price:g}, {detail}")

    # ---------------------------------------------------------------- snapshot
    def snapshot(self) -> Dict[str, Any]:
        engine_state = self.engine.state()
        cycle = self.engine.cycle
        grid = None
        if cycle is not None:
            grid = {
                "low": cycle.swing_low,
                "high": cycle.swing_high,
                "goal_pct": cycle.goal_pct,
                "move_side": cycle.move_side,
                "zero_at": "low" if cycle.move_side == "down" else "high",
                "levels": [{"level": cycle.goal_pct, "price": cycle.target, "label": "цель"}],
            }
        mark = self.engine.last_price
        cycle_notional = (sum(p.qty * mark for c in self.engine.active_cycles for p in c.open_positions)
                          if mark > 0 else 0.0)
        is_futures = self.settings.market_type == "futures"
        leverage = max(1.0, float(self.settings.leverage or 1.0))
        estimated_margin = cycle_notional / leverage if is_futures else cycle_notional
        try:
            account_total = float((self.balance or {}).get("total") or 0)
        except (TypeError, ValueError):
            account_total = 0.0
        account_metrics = {
            "total_balance": account_total,
            "free_balance": (self.balance or {}).get("free"),
            "exchange_used": (self.balance or {}).get("used"),
            "cycle_notional": cycle_notional,
            "active_cycles": len(self.engine.active_cycles),
            "estimated_margin": estimated_margin,
            "deposit_load_pct": estimated_margin / account_total * 100 if account_total > 0 else None,
            "leverage": leverage if is_futures else 1.0,
            "margin_basis": ("оценка: номинал / плечо; без maintenance margin и funding"
                             if is_futures else "спот: полный номинал"),
            "deposit_load_basis": "оценка маржи / account total balance",
            "used_basis": ("виртуальная номинальная стоимость открытых позиций" if self.settings.trade_mode != "live"
                           else "поле used, возвращённое Gate.io balance"),
        }
        return {
            "status": self.status,
            "paused": self.paused,
            "trade_mode": self.settings.trade_mode,
            "data_source": self.settings.data_source,
            "data_source_label": self.settings.data_source_label(),
            "data_real": bool(self.feed.real) if self.feed is not None else not self.settings.synthetic_data,
            "feed": self.feed.status() if self.feed is not None else None,
            "symbol": self.settings.symbol,
            "timeframe": self.settings.timeframe,
            "market_type": self.settings.market_type,
            "error": self.error,
            "started_at": self.started_at,
            "last_loop_ts": self.last_loop_ts,
            "loop_count": self.loop_count,
            "price": self.engine.last_price,
            "balance": self.balance,
            "account_metrics": account_metrics,
            "exchange_position": self.exchange_position,
            "engine": engine_state,
            "stats_24h": self._daily_stats(),
            "grid": grid,
            "pending_orders": [
                {"role": role, **order} for role, order in self._managed_orders.items() if order
            ],
            "target_total_profit": self.settings.total_profit_target_usd,
            "total_progress_pct": round(
                self.engine.realized_pnl / self.settings.total_profit_target_usd * 100, 2
            ) if self.settings.total_profit_target_usd else None,
            "volume_schedule": self.settings.volume_schedule(min(self.settings.max_positions, 8)),
            "signal_scan": self.signal_scan or None,
            "updated_at": now_ms(),
        }

    # ------------------------------------------------------------------ charts
    def chart_candles(self, limit: int = 300) -> List[Candle]:
        return self._chart_candles[-limit:]

    def chart_payload(self, limit: int = 300) -> Dict[str, Any]:
        candles = self.chart_candles(limit)
        cycle = self.engine.cycle
        markers: List[Dict[str, Any]] = []
        levels: List[Dict[str, Any]] = []
        if cycle is not None:
            levels = [
                {"level": lv["level"], "price": lv["price"], "label": lv["label"]}
                for lv in fib_grid(cycle.swing_low, cycle.swing_high,
                                   move_side=cycle.move_side)
            ]
        for active_cycle in self.engine.active_cycles:
            for pos in active_cycle.positions:
                mult = pos.multiplier
                label = f"{pos.idx}" + (f" x{mult:g}" if mult > 1 else "")
                markers.append({"ts": pos.ts, "price": pos.price, "text": label,
                                "kind": "entry", "side": active_cycle.side})
        for c in self.engine.closed_cycles[-40:]:
            for pos in c.positions:
                markers.append({"ts": pos.ts, "price": pos.price,
                                "text": str(pos.idx), "kind": "closed_entry", "side": c.side})
            if c.closed_ts and c.close_price:
                markers.append({"ts": c.closed_ts, "price": c.close_price,
                                "text": f"{c.pnl:+.3f}$", "kind": "closed_exit",
                                "side": c.side, "win": c.pnl >= 0})
        pending_orders = []
        for role, order in self._managed_orders.items():
            if not order or order.get("cycle_id") != (cycle.id if cycle else None):
                continue
            pending_orders.append({
                "role": role, "price": float(order.get("price") or 0.0),
                "volume_usd": float(order.get("volume_usd") or 0.0),
                "qty": float(order.get("qty") or 0.0),
                "base": self.settings.symbol.split("/")[0],
                "idx": int(order.get("idx") or 0), "status": order.get("status", "open"),
            })
        return {
            "candles": [c.to_list() for c in candles],
            "levels": levels,
            "pending_orders": pending_orders,
            "markers": markers,
            "avg_entry": cycle.avg_entry if cycle else None,
            "target": cycle.target if cycle else None,
            "swing": {"low": cycle.swing_low, "high": cycle.swing_high} if cycle else None,
            "side": cycle.side if cycle else None,
            "move_side": cycle.move_side if cycle else None,
            "grid_zero": ("low" if cycle.move_side == "down" else "high") if cycle else None,
            "timeframe": self.settings.timeframe,
        }

    # --------------------------------------------------------------- backtests
    def _backtest_settings(self, params: Dict[str, Any]) -> BotSettings:
        merged = self.settings.model_dump()
        for key, value in (params or {}).items():
            if key in merged and value is not None:
                merged[key] = value
        # A backtest always uses virtual execution regardless of the runtime's
        # current paper/live setting; no exchange orders are ever sent here.
        merged["trade_mode"] = "paper"
        return BotSettings(**merged)

    def _backtest_request(self, bt_settings: BotSettings, params: Dict[str, Any]) -> HistoryRequest:
        source = str(params.get("source") or "").strip().lower()
        aliases = {"": "synthetic" if bt_settings.synthetic_data else "gate",
                   "demo": "synthetic", "synthetic": "synthetic", "synth": "synthetic",
                   "gate": "gate", "exchange": "gate", "real": "gate", "market": "gate",
                   "archive": "archive", "gate_archive": "archive", "deep": "archive",
                   "history": "archive", "bulk": "archive",
                   "csv": "csv", "file": "csv", "upload": "csv"}
        if source not in aliases:
            source = "gate"
        source = aliases[source]
        return HistoryRequest(
            source=source,
            symbol=params.get("symbol") or bt_settings.symbol,
            market_type=params.get("market_type") or bt_settings.market_type,
            timeframe=params.get("timeframe") or bt_settings.timeframe,
            limit=int(params.get("limit_candles") or params.get("limit") or 3000),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            file=str(params.get("file") or ""),
            seed=int(params.get("seed") or 7),
        )

    async def _load_backtest_candles(self, bt_settings: BotSettings, req: HistoryRequest,
                                    job: Dict[str, Any]) -> tuple[List[Candle], Dict[str, Any]]:
        """Свечи для бэктеста + описание источника данных."""

        def progress(loaded: int, total: Optional[int], message: str) -> None:
            if job.get("cancel"):
                raise asyncio.CancelledError()
            job["progress"] = {
                "candles": loaded, "total": total, "message": message,
                "percent": round(min(99.0, loaded / total * 100)) if total else None,
            }

        if req.source == "synthetic":
            from .feed import demo_candles
            limit = max(200, min(int(req.limit or 3000), 50_000))
            job["progress"] = {"candles": 0, "total": limit, "percent": 0,
                               "message": f"генерирую {limit} синтетических свечей"}
            candles = demo_candles(limit, seed=req.seed, settings=bt_settings)
            info = {"source": "synthetic", "label": "синтетические свечи (выдуманный рынок)"}
            info.update(describe_candles(candles, req.timeframe))
            return candles, info

        if req.source == "csv":
            from .history import file_path
            if not req.file:
                raise ValueError("не выбран файл со свечами")
            path = file_path(req.file)
            if not path.exists():
                raise ValueError(f"файл {path.name} не найден в папке данных (data/candles)")
            job["progress"] = {"candles": 0, "total": None, "percent": None,
                               "message": f"читаю файл {path.name}"}
            candles = read_csv(path.read_text(encoding="utf-8", errors="replace"))
            info = {"source": "csv", "file": path.name, "label": f"файл {path.name}"}
            info.update(describe_candles(candles, req.timeframe))
            return candles, info

        warnings: List[str] = []
        if req.source == "archive":
            # Глубокая история: месячные архивы Gate.io (2023 → сейчас).
            # Обычный API отдаёт только последние ~10 000 свечей, архивы — годы.
            from .gate_archive import sync_archive
            job["progress"] = {"candles": 0, "total": None, "percent": None,
                               "message": "скачиваю архивы Gate.io…"}
            candles, info = await sync_archive(req, progress=progress, warnings=warnings)
            info.setdefault("warnings", warnings)
            return candles, info

        job["progress"] = {"candles": 0, "total": None, "percent": None,
                           "message": "скачиваю историю с Gate.io…"}
        candles = await download_candles(bt_settings, req, progress=progress, warnings=warnings)
        name = downloaded_filename(req)
        try:
            save_file(name, write_csv(candles))
        except Exception:
            name = ""
        info = {"source": "gate", "file": name,
                "label": f"история Gate.io {req.symbol} {req.timeframe}",
                "symbol": req.symbol, "market_type": req.market_type}
        info.update(describe_candles(candles, req.timeframe))
        if warnings:
            info["warnings"] = warnings
        return candles, info

    def _new_backtest_job(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "state": "running",
            "created_at": now_ms(),
            "finished_at": None,
            "params": dict(params or {}),
            "progress": {"candles": 0, "total": None, "percent": None, "message": "подготовка…"},
            "data": {},
            "error": "",
            "result": None,
            "result_id": None,
            "cancel": False,
        }
        self.backtest_jobs[job["id"]] = job
        while len(self.backtest_jobs) > 8:      # держим в памяти только свежие задания
            oldest = next(iter(self.backtest_jobs))
            if oldest == job["id"]:
                break
            self.backtest_jobs.pop(oldest, None)
        return job

    @staticmethod
    def public_job(job: Dict[str, Any]) -> Dict[str, Any]:
        """Задание без тяжёлого результата (для опроса состояния)."""
        return {k: v for k, v in job.items() if k not in ("result", "cancel")}

    def backtest_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.backtest_jobs.get(job_id)

    def list_backtest_jobs(self) -> List[Dict[str, Any]]:
        return [self.public_job(j) for j in sorted(self.backtest_jobs.values(),
                                                   key=lambda j: j["created_at"], reverse=True)]

    def cancel_backtest_job(self, job_id: str) -> bool:
        job = self.backtest_jobs.get(job_id)
        if job is None or job["state"] != "running":
            return False
        job["cancel"] = True
        return True

    def start_backtest_job(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job = self._new_backtest_job(params)
        task = asyncio.create_task(self._execute_backtest(job))
        self._backtest_tasks[job["id"]] = task
        task.add_done_callback(lambda _t: self._backtest_tasks.pop(job["id"], None))
        return {"ok": True, "job": self.public_job(job)}

    async def run_backtest(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Синхронно прогнать бэктест (небольшие периоды, тесты, curl)."""
        job = self._new_backtest_job(params)
        await self._execute_backtest(job)
        if job["state"] == "error":
            return {"ok": False, "error": job["error"], "job_id": job["id"]}
        result = job["result"] or {"ok": False, "error": "нет результата"}
        return result

    async def _execute_backtest(self, job: Dict[str, Any]) -> None:
        """Скачать/прочитать данные и прогнать по ним ту же стратегию."""
        params = job["params"]
        try:
            bt_settings = self._backtest_settings(params)
            req = self._backtest_request(bt_settings, params)
            job["data"] = req.describe()
            candles, info = await self._load_backtest_candles(bt_settings, req, job)
            job["data"].update(info)
            job["progress"] = {"candles": len(candles), "total": len(candles), "percent": 99,
                               "message": "считаю сделки и статистику…"}
            result = run_backtest(candles, bt_settings, initial_balance=bt_settings.paper_balance_usd)
            result["ok"] = True
            result["data"] = info
            result["params"] = req.describe()
            bt_id = self.db.save_backtest(
                {**params, "source": req.source, "data": info,
                 "settings": bt_settings.model_dump(exclude={"api_key", "api_secret"})}, result)
            result["id"] = bt_id
            job["result"] = result
            job["result_id"] = bt_id
            job["state"] = "done"
            job["progress"] = {"candles": len(candles), "total": len(candles), "percent": 100,
                               "message": f"готово: {len(candles)} свечей, {result.get('cycles', 0)} циклов"}
            self.log("info", f"Бэктест готов: {info.get('label', req.source)}, "
                             f"свечей {len(candles)}, циклов {result.get('cycles', 0)}, "
                             f"профит {result.get('total_pnl', 0):+.4f}$", persist=False)
            for warning in info.get("warnings", []):
                self.log("warn", f"бэктест: {warning}", persist=False)
        except asyncio.CancelledError:
            job["state"] = "cancelled"
            job["error"] = "загрузка остановлена пользователем"
            job["progress"]["message"] = "остановлено"
        except Exception as exc:
            job["state"] = "error"
            job["error"] = str(exc)
            job["progress"]["message"] = "ошибка"
            self.log("error", f"бэктест не удался: {exc}", persist=False)
        finally:
            job["finished_at"] = now_ms()
            job["cancel"] = False
            self.broadcast({"type": "backtest", "job": self.public_job(job)})
