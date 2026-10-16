"""Источники рыночных данных.

* ``GateFeed``      — реальные свечи Gate.io через ccxt (спот или USDT-перп).
  Публичные данные, API-ключи не нужны: виртуальная торговля (paper) идёт
  по настоящему рынку.
* ``SyntheticFeed`` — выдуманные свечи (детерминированный random walk), нужны
  только чтобы посмотреть панель без интернета. В интерфейсе всегда помечаются
  как «синтетика»: бот никогда не подменяет реальные данные выдуманными молча.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from typing import List, Optional, Sequence

import ccxt.async_support as ccxt  # type: ignore

from .config import BotSettings
from .models import Candle, now_ms


def timeframe_to_ms(timeframe: str) -> int:
    from .config import TIMEFRAMES
    return TIMEFRAMES[timeframe] * 1000


#: максимальный шаг реального времени, который учитывает DEMO-фид за один опрос
MAX_REAL_STEP_SEC = 10.0


class BaseFeed:
    name = "base"
    label = "данные"
    #: True, если свечи приходят с реального рынка
    real = False

    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.candles: List[Candle] = []
        self.last_error: str = ""
        self.last_ok_ts: int = 0        # когда последний раз удачно получили данные
        self.last_candle_ts: int = 0    # время последней реальной свечи

    def status(self) -> dict:
        """Состояние источника данных — показывается в панели."""
        return {
            "name": self.name,
            "label": self.label,
            "real": bool(self.real),
            "last_error": self.last_error,
            "last_ok_ts": self.last_ok_ts,
            "last_candle_ts": self.last_candle_ts,
            "symbol": self.settings.symbol,
            "timeframe": self.settings.timeframe,
        }

    async def history(self, limit: int = 300) -> List[Candle]:
        raise NotImplementedError

    async def poll(self) -> tuple[List[Candle], float]:
        """Return ``(newly closed candles, last price)``."""
        raise NotImplementedError

    async def close(self) -> None:
        return None


class GateFeed(BaseFeed):
    """Реальные свечи Gate.io (публичный API — ключи не нужны)."""

    name = "gate"
    label = "свечи Gate.io (реальные)"
    real = True

    def __init__(self, settings: BotSettings) -> None:
        super().__init__(settings)
        self.ex: Optional[ccxt.Exchange] = None
        self.unified = settings.symbol

    def _unified_symbol(self) -> str:
        if self.settings.market_type == "futures" and ":" not in self.settings.symbol:
            base, _, quote = self.settings.symbol.partition("/")
            return f"{base}/{quote}:{quote}"
        return self.settings.symbol

    async def _client(self) -> ccxt.Exchange:
        if self.ex is None:
            options = {"defaultType": "swap" if self.settings.market_type == "futures" else "spot"}
            self.ex = ccxt.gate({"enableRateLimit": True, "timeout": 20000, "options": options})
            if self.settings.sandbox:
                try:
                    self.ex.set_sandbox_mode(True)
                except Exception:
                    pass
            await self.ex.load_markets()
            self.unified = self._unified_symbol()
        return self.ex

    async def history(self, limit: int = 300) -> List[Candle]:
        try:
            ex = await self._client()
            rows = await ex.fetch_ohlcv(self.unified, self.settings.timeframe, limit=min(limit + 1, 1000))
        except Exception as exc:
            self.last_error = _friendly_error(exc)
            raise
        candles = [Candle.from_ccxt(r) for r in rows]
        if candles and not self._is_closed(candles[-1]):
            candles = candles[:-1]
        self.candles = candles
        self._mark_ok(candles)
        return candles

    async def poll(self) -> tuple[List[Candle], float]:
        try:
            return await self._poll_once()
        except Exception as exc:
            self.last_error = _friendly_error(exc)
            raise

    def _mark_ok(self, candles: List[Candle]) -> None:
        self.last_error = ""
        self.last_ok_ts = now_ms()
        if candles:
            self.last_candle_ts = candles[-1].ts

    async def _poll_once(self) -> tuple[List[Candle], float]:
        ex = await self._client()
        rows = await ex.fetch_ohlcv(self.unified, self.settings.timeframe, limit=3)
        candles = [Candle.from_ccxt(r) for r in rows]
        if not candles:
            return [], 0.0
        last_price = candles[-1].close
        closed = [c for c in candles if self._is_closed(c)]
        new: List[Candle] = []
        known = {c.ts for c in self.candles}
        for c in closed:
            if c.ts not in known:
                new.append(c)
                self.candles.append(c)
        if len(self.candles) > 1500:
            self.candles = self.candles[-1500:]
        self._mark_ok(candles)
        # keep the live candle fresh for the chart
        if candles and not self._is_closed(candles[-1]):
            if self.candles and self.candles[-1].ts == candles[-1].ts:
                self.candles[-1] = candles[-1]
            elif not self.candles or candles[-1].ts > self.candles[-1].ts:
                self.candles.append(candles[-1])
        return new, last_price

    def _is_closed(self, candle: Candle) -> bool:
        return candle.ts + timeframe_to_ms(self.settings.timeframe) <= now_ms() + 500

    async def close(self) -> None:
        if self.ex is not None:
            await self.ex.close()
            self.ex = None


class SyntheticFeed(BaseFeed):
    """Выдуманный рынок (``data_source = synthetic``) — без интернета."""

    name = "synthetic"
    label = "синтетические свечи (выдуманный рынок)"
    real = False

    def __init__(self, settings: BotSettings, *, seed: int = 20240517,
                 start_price: float = 64000.0, speed: Optional[float] = None) -> None:
        super().__init__(settings)
        self.rng = random.Random(seed)
        #: 1.0 = real time, >1 accelerates the synthetic market (handy for demos)
        self.speed = float(speed if speed is not None else getattr(settings, "demo_speed", 1.0) or 1.0)
        self.tf_ms = timeframe_to_ms(settings.timeframe)
        self.price = start_price
        self._drift = 0.0
        self._drift_left = 0
        self.current: Optional[Candle] = None
        self._last_tick = now_ms()
        self.vnow = now_ms()          # virtual clock: advanced by elapsed * speed
        self.candles = self._generate_history(320, start_price)

    # ------------------------------------------------------------- generation
    def _step_range(self, seconds: float) -> float:
        """Random walk step in % for the given amount of time."""
        per_min = 0.16  # % per minute standard deviation
        return per_min * math.sqrt(max(seconds, 1.0) / 60.0)

    def _new_drift(self) -> None:
        if self._drift_left <= 0:
            self._drift = self.rng.gauss(0, 0.05)
            self._drift_left = self.rng.randint(5, 25)

    def _generate_history(self, n: int, start_price: float) -> List[Candle]:
        tf_s = self.tf_ms / 1000.0
        end_ts = (self.vnow // self.tf_ms) * self.tf_ms
        candles: List[Candle] = []
        price = start_price
        for i in range(n):
            self._new_drift()
            self._drift_left -= 1
            open_ = price
            sigma = self._step_range(tf_s)
            close = open_ * (1 + (self._drift + self.rng.gauss(0, sigma)) / 100.0)
            hi = max(open_, close) * (1 + abs(self.rng.gauss(0, sigma / 3)) / 100.0)
            lo = min(open_, close) * (1 - abs(self.rng.gauss(0, sigma / 3)) / 100.0)
            candles.append(Candle(ts=end_ts - (n - i) * self.tf_ms, open=round(open_, 2),
                                  high=round(hi, 2), low=round(lo, 2), close=round(close, 2),
                                  volume=round(abs(self.rng.gauss(50, 20)), 3)))
            price = close
        self.price = price
        self.current = Candle(ts=end_ts, open=price, high=price, low=price, close=price, volume=0.0)
        return candles

    # ------------------------------------------------------------------- feed
    def _advance(self, seconds: float) -> None:
        """Move the synthetic price for ``seconds`` and update the live candle."""
        self._new_drift()
        self._drift_left -= 1
        sigma = self._step_range(max(seconds, 0.5))
        change = (self._drift + self.rng.gauss(0, sigma)) / 100.0
        self.price = max(0.01, self.price * (1 + change))
        cur = self.current
        assert cur is not None
        cur.close = round(self.price, 2)
        cur.high = max(cur.high, cur.close)
        cur.low = min(cur.low, cur.close)
        cur.volume = round(cur.volume + abs(self.rng.gauss(1.5 * max(seconds, 0.5), 1)), 3)

    async def history(self, limit: int = 300) -> List[Candle]:
        return list(self.candles[-limit:])

    async def poll(self) -> tuple[List[Candle], float]:
        now = now_ms()
        real = max(0.0, (now - self._last_tick) / 1000.0)
        self.last_ok_ts = now
        self.last_candle_ts = self.candles[-1].ts if self.candles else now
        self._last_tick = now
        # защита от «прыжка» после паузы окружения (сон VPS, рестарт процесса,
        # отладка): длительность шага ограничена, иначе демо-рынок уезжает на часы
        if real > MAX_REAL_STEP_SEC:
            real = MAX_REAL_STEP_SEC
        elapsed = real * self.speed
        closed: List[Candle] = []
        remaining = elapsed
        guard = 0
        while remaining > 1e-9 and guard < 2000:
            cur = self.current
            assert cur is not None
            left = (cur.ts + self.tf_ms - self.vnow) / 1000.0
            if left <= 0:                                  # candle is finished
                guard += 1
                closed.append(Candle(ts=cur.ts, open=cur.open, high=cur.high, low=cur.low,
                                     close=cur.close, volume=cur.volume))
                self.candles.append(closed[-1])
                self.current = Candle(ts=cur.ts + self.tf_ms, open=cur.close, high=cur.close,
                                      low=cur.close, close=cur.close, volume=0.0)
                self._advance(min(1.0, self.tf_ms / 4000.0))   # не даём свече быть плоской
                continue
            step = min(remaining, left)
            remaining -= step
            self.vnow += step * 1000.0
            self._advance(step)
        if len(self.candles) > 1500:
            self.candles = self.candles[-1500:]
        return closed, self.price

    def chart_candles(self, limit: int = 320) -> List[Candle]:
        out = list(self.candles[-limit:])
        if self.current is not None:
            out.append(self.current)
        return out


#: старое имя класса — оставлено, чтобы ничего не сломалось у тех, кто импортировал его
DemoFeed = SyntheticFeed


def make_feed(settings: BotSettings, *, start_price: Optional[float] = None) -> BaseFeed:
    """Создать источник данных по настройкам.

    ``gate`` (по умолчанию) — реальный рынок; ``synthetic`` — выдуманный.
    API-ключи для реальных свечей не нужны.
    """
    if settings.synthetic_data:
        return SyntheticFeed(settings, speed=getattr(settings, "demo_speed", 1.0),
                             start_price=start_price or 64000.0)
    return GateFeed(settings)


def _friendly_error(exc: BaseException) -> str:
    """Человеческое объяснение сетевой ошибки биржи."""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "451" in low or "restricted" in low or "forbidden" in low or "region" in low:
        return (f"биржа недоступна с этого сервера (гео-ограничение): {text}")
    if any(k in low for k in ("timed out", "timeout", "connection", "network", "dns",
                              "resolve", "unreachable", "ssl", "handshake")):
        return (f"нет связи с биржей Gate.io: {text}. Проверьте интернет на сервере: "
                f"curl -s -m 10 'https://api.gateio.ws/api/v4/spot/time'")
    return text


async def check_gate_connection(settings: BotSettings) -> Dict[str, Any]:
    """Проверить связь с Gate.io по публичным данным (ключи не нужны).

    Возвращает цену, время последней свечи и задержку ответа — чтобы панель
    показала пользователю, что реальные данные действительно идут.
    """
    started = time.perf_counter()
    feed = GateFeed(settings)
    try:
        ex = await feed._client()
        symbol = feed.unified
        rows = await ex.fetch_ohlcv(symbol, settings.timeframe, limit=3)
        latency = int((time.perf_counter() - started) * 1000)
        if not rows:
            return {"ok": False, "error": f"биржа не отдала свечи по {symbol} ({settings.timeframe})"}
        candles = [Candle.from_ccxt(r) for r in rows]
        closed = [c for c in candles if feed._is_closed(c)]
        last = (closed or candles)[-1]
        price = candles[-1].close
        age_sec = max(0, (now_ms() - last.ts) / 1000.0)
        return {
            "ok": True,
            "real_data": True,
            "exchange": settings.exchange_id,
            "symbol": symbol,
            "timeframe": settings.timeframe,
            "market_type": settings.market_type,
            "price": price,
            "candle_ts": last.ts,
            "candle_age_sec": round(age_sec, 1),
            "latency_ms": latency,
            "message": (f"реальные данные идут: {symbol} {settings.timeframe}, "
                        f"цена {price:g}, последняя закрытая свеча "
                        f"{time.strftime('%d.%m.%Y %H:%M', time.gmtime(last.ts / 1000))} UTC"),
        }
    except Exception as exc:
        return {"ok": False, "real_data": True, "exchange": settings.exchange_id,
                "error": _friendly_error(exc)}
    finally:
        try:
            await feed.close()
        except Exception:
            pass


def demo_candles(n: int, *, seed: int = 7, start_price: float = 64000.0,
                 settings: Optional[BotSettings] = None) -> List[Candle]:
    """Standalone candle generator for the backtester."""
    tf_ms = timeframe_to_ms(settings.timeframe if settings else "5m")
    rng = random.Random(seed)
    price = start_price
    candles: List[Candle] = []
    drift, drift_left = 0.0, 0
    ts0 = now_ms() - n * tf_ms
    sigma = 0.16 * math.sqrt((tf_ms / 1000) / 60)
    for i in range(n):
        if drift_left <= 0:
            drift = rng.gauss(0, 0.05)
            drift_left = rng.randint(5, 25)
        drift_left -= 1
        open_ = price
        close = open_ * (1 + (drift + rng.gauss(0, sigma)) / 100.0)
        hi = max(open_, close) * (1 + abs(rng.gauss(0, sigma / 3)) / 100.0)
        lo = min(open_, close) * (1 - abs(rng.gauss(0, sigma / 3)) / 100.0)
        candles.append(Candle(ts=ts0 + i * tf_ms, open=round(open_, 2), high=round(hi, 2),
                              low=round(lo, 2), close=round(close, 2),
                              volume=round(abs(rng.gauss(50, 20)), 3)))
        price = close
    return candles
