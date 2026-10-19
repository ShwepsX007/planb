"""Глубокая история Gate.io: месячные архивы, кеш и бэктест на годах."""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import math

import httpx
import pytest

from app import gate_archive as ga
from app.gate_archive import (ArchiveNotFound, archive_timeframe, archive_url, biz_name,
                              cache_paths, list_cache, month_key, months_between,
                              parse_archive_csv, resample, sync_archive)
from app.history import HistoryError, HistoryRequest


# ------------------------------------------------------------------ вспомогательное
def ms(year: int, month: int, day: int = 1, hour: int = 0, minute: int = 0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc).timestamp() * 1000)


def month_rows(year: int, month: int, *, days: int = 28, tf_ms: int = 300_000,
               start_price: float = 100.0, wave_pct: float = 6.0) -> str:
    """Свечи одного месяца в формате архива Gate.io (новые сверху)."""
    start = ms(year, month)
    end = start + days * 86_400_000
    rows = []
    price = start_price
    index = 0
    ts = start
    while ts < end:
        open_ = price
        close = start_price * (1 + wave_pct / 100 * math.sin(index / 60.0))
        high = max(open_, close) * 1.001
        low = min(open_, close) * 0.999
        rows.append(f"{ts // 1000},1.5,{close:.6f},{high:.6f},{low:.6f},{open_:.6f}")
        price = close
        ts += tf_ms
        index += 1
    return "\n".join(reversed(rows))


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


class FakeArchive:
    """Подменяет сеть: отдаёт заранее заданные месяцы и считает запросы."""

    def __init__(self, monkeypatch, tmp_path, files=None):
        monkeypatch.setenv("PLANB_DATA_DIR", str(tmp_path))
        self.files = dict(files or {})
        self.calls: list[str] = []
        self.tail: list = []
        self.tail_calls: list = []
        fake = self

        async def fetch(url: str, **_kwargs) -> bytes:
            fake.calls.append(url)
            for key, text in fake.files.items():
                if key in url:
                    return gz(text)
            raise ArchiveNotFound(f"нет файла: {url}")

        async def tail(req):
            fake.tail_calls.append(req)
            return list(fake.tail)

        monkeypatch.setattr(ga, "fetch_bytes", fetch)
        monkeypatch.setattr(ga, "_tail_candles", tail)


@pytest.fixture
def archive(monkeypatch, tmp_path):
    return FakeArchive(monkeypatch, tmp_path)


def request(**kwargs) -> HistoryRequest:
    base = dict(source="archive", symbol="BTC/USDT", market_type="futures",
                timeframe="5m", date_from="2024-01-01", date_to="2024-03-01")
    base.update(kwargs)
    return HistoryRequest(**base)


# ------------------------------------------------------------------- адреса и месяцы
def test_archive_urls_cover_spot_and_both_futures_markets():
    assert biz_name("futures", "BTC/USDT") == "futures_usdt"
    assert biz_name("spot", "BTC/USDT") == "spot"
    assert biz_name("futures", "BTC/USD") == "futures_btc"
    url = archive_url("BTC/USDT", "futures", "5m", "202402")
    assert url.endswith("/futures_usdt/candlesticks_5m/202402/BTC_USDT-202402.csv.gz")
    spot = archive_url("ETH/USDT", "spot", "1h", "202301")
    assert spot.endswith("/spot/candlesticks_1h/202301/ETH_USDT-202301.csv.gz")


def test_timeframes_without_archive_are_resampled_from_a_base_one():
    assert archive_timeframe("1m") == ("1m", 1)
    assert archive_timeframe("4h") == ("4h", 1)
    assert archive_timeframe("15m") == ("5m", 3)
    assert archive_timeframe("30m") == ("5m", 6)
    assert archive_timeframe("12h") == ("1h", 12)
    with pytest.raises(HistoryError):
        archive_timeframe("7d")


def test_months_between_does_not_take_an_extra_month_at_the_boundary():
    assert months_between(ms(2024, 1, 1), ms(2024, 3, 1)) == ["202401", "202402"]
    assert months_between(ms(2024, 1, 1), ms(2024, 3, 15)) == ["202401", "202402", "202403"]
    assert months_between(ms(2024, 1, 5), ms(2024, 1, 5)) == ["202401"]
    assert month_key(dt.datetime(2023, 7, 1, tzinfo=dt.timezone.utc)) == "202307"


# ------------------------------------------------------------------------- парсер
def test_parse_gate_format_without_header_uses_gate_column_order():
    """Официальный пример Gate.io: timestamp, volume, close, high, low, open."""
    text = ("1626224400,399272,32372.13,32490.9,32265.76,32425.09\n"
            "1626220800,389671,32423.23,32806.43,32358.63,32733.89\n"
            "1626217200,195723,32726.23,32749.01,32530.04,32580.61\n")
    candles = parse_archive_csv(text)
    assert len(candles) == 3
    first = candles[0]
    assert first.ts == 1626217200000
    assert first.open == pytest.approx(32580.61)
    assert first.close == pytest.approx(32726.23)
    assert first.high == pytest.approx(32749.01)
    assert first.low == pytest.approx(32530.04)
    assert first.volume == pytest.approx(195723.0)


def test_parse_accepts_header_and_usual_csv_order_too():
    text = ("timestamp,open,high,low,close,volume\n"
            "1704067200,100,102,99,101,5.5\n"
            "1704067500,101,103,100,102,6.5\n")
    candles = parse_archive_csv(text)
    assert [c.close for c in candles] == [101.0, 102.0]
    assert candles[0].volume == pytest.approx(5.5)


def test_parse_rejects_a_file_that_is_not_candles():
    with pytest.raises(HistoryError):
        parse_archive_csv("hello,world\n1,2\n")


def test_resample_1m_to_15m_keeps_ohlcv_correct():
    base = "1m"
    start = ms(2024, 1, 1)
    candles = []
    from app.models import Candle
    for i in range(30):
        price = 100 + i
        candles.append(Candle(ts=start + i * 60_000, open=price, high=price + 2,
                              low=price - 2, close=price + 1, volume=1.0))
    out = resample(candles, base, "15m")
    assert len(out) == 2
    assert out[0].ts == start and out[0].open == 100 and out[0].close == 115
    assert out[0].high == 116 and out[0].low == 98
    assert out[0].volume == pytest.approx(15.0)
    assert out[1].open == 115 and out[1].close == 130


# --------------------------------------------------------------------------- синк
async def test_sync_downloads_months_caches_them_and_reuses_cache(archive):
    archive.files = {"202401": month_rows(2024, 1, days=20),
                     "202402": month_rows(2024, 2, days=20)}
    candles, info = await sync_archive(request())
    assert len(candles) > 5000
    assert info["source"] == "archive" and info["months_downloaded"] == 2
    assert info["from_utc"].startswith("2024-01-01")
    assert len(archive.calls) == 2

    csv_file, meta_file = cache_paths("BTC/USDT", "futures", "5m")
    assert csv_file.exists() and meta_file.exists()
    cached = list_cache()
    assert cached and cached[0]["count"] == len(candles)

    again, info2 = await sync_archive(request())
    assert len(again) == len(candles)
    assert info2["months_downloaded"] == 0
    assert len(archive.calls) == 2, "повторный прогон не должен качать те же месяцы"


async def test_sync_warns_about_missing_months_instead_of_failing(archive):
    archive.files = {"202401": month_rows(2024, 1, days=20)}
    warnings: list[str] = []
    candles, info = await sync_archive(request(), warnings=warnings)
    assert len(candles) > 1000
    assert warnings and "2024-02" in warnings[0]
    assert info["months_downloaded"] == 1


async def test_sync_reports_progress_in_months(archive):
    archive.files = {"202401": month_rows(2024, 1, days=10),
                     "202402": month_rows(2024, 2, days=10)}
    steps: list[tuple[int, int, str]] = []
    await sync_archive(request(), progress=lambda done, total, text: steps.append((done, total, text)))
    assert steps and steps[-1][0] == steps[-1][1] == 2
    assert any("месяцев" in text for _d, _t, text in steps)


async def test_sync_resamples_15m_from_5m_archive(archive):
    archive.files = {"202401": month_rows(2024, 1, days=10)}
    candles, info = await sync_archive(request(timeframe="15m",
                                              date_to="2024-02-01"))
    tf_ms = 15 * 60 * 1000
    assert all(b.ts - a.ts == tf_ms for a, b in zip(candles, candles[1:]))
    assert info["resampled_from"] == "5m"
    assert info["timeframe"] == "15m"


async def test_resampled_cache_stays_consistent_when_more_months_arrive(archive):
    """Кеш 15m должен хранить исходные 5m: докачка нового месяца не ломает OHLC."""
    archive.files = {"202401": month_rows(2024, 1, days=4)}
    req = request(timeframe="15m", date_to="2024-02-01")
    first, _info = await sync_archive(req)
    assert len(first) > 100

    archive.files["202402"] = month_rows(2024, 2, days=4)
    req.date_to = "2024-03-01"
    second, info = await sync_archive(req)
    assert info["months_downloaded"] == 1
    assert len(second) > len(first)
    # внутри каждого месяца шаг ровно 15 минут, дублей нет
    january = [c for c in second if c.ts < ms(2024, 2, 1)]
    assert all(b.ts - a.ts == 900_000 for a, b in zip(january, january[1:]))
    assert len({c.ts for c in second}) == len(second)
    # в кеше лежат именно 5-минутные свечи (288 строк в сутки)
    csv_file, _meta = cache_paths("BTC/USDT", "futures", "15m")
    rows = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) - 1 == 8 * 288


async def test_recent_tail_is_filled_from_api_when_the_month_is_not_published_yet(archive):
    """Запрос «по сегодня» не должен обрываться на конце прошлого месяца."""
    from app.models import Candle
    from app.models import now_ms
    now = now_ms()
    current = dt.datetime.now(tz=dt.timezone.utc)
    prev = (current.replace(day=1) - dt.timedelta(days=1))
    key = prev.strftime("%Y%m")
    archive.files = {key: month_rows(prev.year, prev.month, days=2)}
    tail_ts = (now // 300_000 - 1) * 300_000
    archive.tail = [Candle(ts=tail_ts, open=100, high=101, low=99, close=100.5, volume=1.0)]

    # Keep the fixture's two synthetic archive days inside the requested range;
    # a fixed 45-day lookback can fall after those rows as the calendar advances.
    start = ms(prev.year, prev.month)
    req = request(date_from=start, date_to=now)
    candles, info = await sync_archive(req)
    assert info["tail_candles"] == 1
    assert candles[-1].ts == tail_ts
    assert archive.tail_calls, "добор последних свечей должен идти через API"
    assert archive.tail_calls[0].date_from is not None


async def test_tail_fill_is_skipped_for_historical_periods(archive):
    archive.files = {"202401": month_rows(2024, 1, days=31),
                     "202402": month_rows(2024, 2, days=29)}
    await sync_archive(request())
    assert archive.tail_calls == [], "для исторического периода API не нужен"


async def test_sync_rejects_too_many_candles_with_a_clear_message(archive, monkeypatch):
    monkeypatch.setenv("PLANB_ARCHIVE_MAX_CANDLES", "10000")
    archive.files = {"202401": month_rows(2024, 1, days=31)}
    with pytest.raises(HistoryError) as exc:
        await sync_archive(request(timeframe="1m", date_to="2024-03-01"))
    assert "лимита" in str(exc.value) and "таймфрейм" in str(exc.value)


async def test_sync_rejects_a_broken_archive(archive):
    archive.files = {"202401": "not,a,candle\nfile,that\n", "202402": "still,broken\n"}
    with pytest.raises(HistoryError):
        await sync_archive(request())


async def test_cache_can_be_deleted(archive):
    archive.files = {"202401": month_rows(2024, 1, days=5)}
    candles, _info = await sync_archive(request(date_to="2024-02-01"))
    csv_file, meta_file = cache_paths("BTC/USDT", "futures", "5m")
    assert csv_file.exists() and meta_file.exists()
    assert ga.delete_cache(csv_file.stem.split(".")[0]) is True
    assert not csv_file.exists() and not meta_file.exists()
    assert list_cache() == []


# ------------------------------------------------------------------ через API
def make_app(tmp_path: str):
    from app.main import create_app
    return create_app(str(tmp_path / "archive.sqlite3"))


async def test_archive_endpoints_and_backtest_use_downloaded_history(archive, tmp_path):
    archive.files = {"202401": month_rows(2024, 1, days=31),
                     "202402": month_rows(2024, 2, days=31)}
    app = make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        listed = (await client.get("/api/history/archive")).json()
        assert listed["ok"] is True and listed["max_candles"] > 100_000

        started = (await client.post("/api/backtest/job", json={
            "source": "archive", "symbol": "BTC/USDT", "market_type": "futures",
            "timeframe": "5m", "date_from": "2024-01-01", "date_to": "2024-03-01",
            "min_move_pct": 0.5, "min_profit_pct": 0.1, "fib_goal_pct": 50,
            "base_volume_usd": 10, "max_positions": 6, "fee_pct": 0.05, "slippage_pct": 0.02,
        })).json()
        job_id = started["job"]["id"]
        for _ in range(120):
            status = (await client.get(f"/api/backtest/job/{job_id}")).json()
            if status["state"] != "running":
                break
            await asyncio.sleep(0.05)
        assert status["state"] == "done", status
        assert status["data"]["source"] == "archive"
        assert status["data"]["months_downloaded"] == 2
        result = status["result"]
        assert result["candles"] > 10_000
        assert result["data"]["source"] == "archive"

        listed = (await client.get("/api/history/archive")).json()
        assert listed["items"] and listed["items"][0]["market_type"] == "futures"
        key = listed["items"][0]["key"]
        assert (await client.delete(f"/api/history/archive/{key}")).json()["ok"] is True
        assert (await client.get("/api/history/archive")).json()["items"] == []


def test_request_period_backed_by_first_available_month(archive):
    """Запрос 2020 года не должен молча давать пустоту: объясняем и начинаем с 2023."""
    archive.files = {}
    warnings: list[str] = []
    req = request(date_from="2020-01-01", date_to="2020-03-01")
    with pytest.raises(HistoryError):
        asyncio.run(sync_archive(req, warnings=warnings))
    assert warnings and "2023-01" in warnings[0]


async def test_current_month_is_refreshed_every_run(archive):
    now = dt.datetime.now(tz=dt.timezone.utc)
    current = now.strftime("%Y%m")
    first = now.replace(day=1)
    archive.files = {current: month_rows(first.year, first.month, days=2)}
    start_ms = int(first.timestamp() * 1000)
    req = request(timeframe="5m", date_from=start_ms, date_to=now)
    candles, _info = await sync_archive(req)
    assert candles
    assert len(archive.calls) == 1
    _again, info = await sync_archive(req)
    assert info["months_downloaded"] == 1, "текущий месяц должен обновляться"
    assert len(archive.calls) == 2
