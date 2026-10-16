"""Реальная история для бэктеста: загрузка с биржи, CSV, файлы, фоновые задания."""
from __future__ import annotations

import os
import time

import httpx
import pytest

from app.config import BotSettings
from app.history import (HistoryError, HistoryRequest, dedupe, describe, downloaded_filename,
                         estimate_candles, file_info, list_files, normalize_symbol, parse_time_ms,
                         read_csv, safe_filename, write_csv, file_path)
from app.models import Candle, now_ms


# ------------------------------------------------------------------ вспомогательное
def settings(**kwargs) -> BotSettings:
    base = dict(symbol="BTC/USDT", market_type="futures", timeframe="5m", data_source="gate",
                trade_mode="paper", candles_count=3, min_move_pct=0.5, min_profit_pct=0.3,
                base_volume_usd=10.0, max_positions=6, paper_balance_usd=1000.0)
    base.update(kwargs)
    return BotSettings(**base)


def make_candles(count: int, *, tf_ms: int = 300_000, step: float = 0.1, end_ts: int | None = None):
    """Ряд свечей, заканчивающийся на закрытой свече."""
    end = end_ts or (now_ms() // tf_ms - 1) * tf_ms
    out = []
    price = 60_000.0
    for i in range(count):
        ts = end - (count - 1 - i) * tf_ms
        open_ = price
        close = price + step
        out.append(Candle(ts=ts, open=open_, high=max(open_, close), low=min(open_, close),
                          close=close, volume=1.5))
        price = close
    return out


class FakeExchange:
    """Мини-двойник ccxt: отдаёт историю страницами, как настоящая биржа."""

    def __init__(self, candles, page: int = 1000, fail_after: int | None = None):
        self.candles = list(candles)
        self.page = page
        self.fail_after = fail_after
        self.calls: list[dict] = []
        self.closed = False
        self.markets = {"BTC/USDT:USDT": {"id": "BTC_USDT"}, "BTC/USDT": {"id": "BTC_USDT"}}

    async def load_markets(self):
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("connection reset by peer")
        rows = [c for c in self.candles if c.ts >= (since or 0)]
        rows = rows[: min(limit, self.page)]
        return [c.to_list() for c in rows]

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_ccxt(monkeypatch):
    """Подменяет ccxt.gate на двойника, возвращая фабрику двойников."""
    import ccxt.async_support as ccxt_async

    created: list[FakeExchange] = []

    def install(candles, page: int = 1000, fail_after: int | None = None) -> FakeExchange:
        ex = FakeExchange(candles, page=page, fail_after=fail_after)
        created.append(ex)

        def factory(_config):
            return ex

        monkeypatch.setattr(ccxt_async, "gate", factory)
        return ex

    install.created = created  # type: ignore[attr-defined]
    return install


# --------------------------------------------------------------------- символы
def test_normalize_symbol_futures_and_spot():
    assert normalize_symbol("btc_usdt", "spot") == "BTC/USDT"
    assert normalize_symbol("BTC-USDT", "spot") == "BTC/USDT"
    assert normalize_symbol("btc/usdt", "futures") == "BTC/USDT:USDT"
    assert normalize_symbol("ETH/USDT:USDT", "futures") == "ETH/USDT:USDT"
    with pytest.raises(HistoryError):
        normalize_symbol("BTCUSDT", "spot")


def test_parse_time_ms_formats():
    assert parse_time_ms("2026-01-31") == int(
        __import__("datetime").datetime(2026, 1, 31, tzinfo=__import__("datetime").timezone.utc)
        .timestamp() * 1000)
    assert parse_time_ms("31.01.2026 12:00") == parse_time_ms("2026-01-31T12:00")
    assert parse_time_ms(1_700_000_000) == 1_700_000_000_000        # секунды
    assert parse_time_ms(1_700_000_000_000) == 1_700_000_000_000    # миллисекунды
    assert parse_time_ms("") is None and parse_time_ms(None) is None
    with pytest.raises(HistoryError):
        parse_time_ms("позавчера")


def test_estimate_candles_from_period():
    req = HistoryRequest(timeframe="5m", date_from="2026-01-01", date_to="2026-01-02")
    assert estimate_candles(req) == 288
    with pytest.raises(HistoryError):
        estimate_candles(HistoryRequest(timeframe="5m", date_from="2026-01-02", date_to="2026-01-01"))
    assert estimate_candles(HistoryRequest(timeframe="5m", limit=1234)) == 1234


# --------------------------------------------------------------------- загрузка
async def test_download_paginates_with_limits(fake_ccxt):
    tf_ms = 300_000
    ex = fake_ccxt(make_candles(10_000, tf_ms=tf_ms))
    from app.history import download_candles
    req = HistoryRequest(source="gate", symbol="BTC/USDT", market_type="futures",
                         timeframe="5m", limit=2500)
    candles = await download_candles(settings(), req)
    assert 2400 <= len(candles) <= 2500
    assert len(ex.calls) >= 3, "история должна качаться страницами"
    assert ex.closed, "соединение с биржей должно закрываться"
    stamps = [c.ts for c in candles]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert all(c.ts + tf_ms <= now_ms() for c in candles), "незакрытые свечи не берём"


async def test_download_respects_period_and_progress(fake_ccxt):
    tf_ms = 300_000
    fake_ccxt(make_candles(20_000, tf_ms=tf_ms))
    from app.history import download_candles
    start = now_ms() - 500 * tf_ms
    req = HistoryRequest(source="gate", symbol="BTC/USDT", market_type="futures", timeframe="5m",
                         date_from=start, date_to=now_ms())
    progress_calls: list[tuple] = []
    candles = await download_candles(settings(), req, progress=lambda *a: progress_calls.append(a))
    assert 480 <= len(candles) <= 500
    assert candles[0].ts >= start - tf_ms
    assert progress_calls and progress_calls[0][0] > 0
    assert progress_calls[-1][1] == len(candles) or progress_calls[-1][1] is None


async def test_download_rejects_unknown_symbol_and_huge_requests(fake_ccxt):
    fake_ccxt(make_candles(100))
    from app.history import download_candles
    with pytest.raises(HistoryError) as err:
        await download_candles(settings(), HistoryRequest(symbol="NOTACOIN/USDT", limit=100))
    assert "нет пары" in str(err.value)
    with pytest.raises(HistoryError) as err2:
        await download_candles(settings(), HistoryRequest(symbol="BTC/USDT", timeframe="1m",
                                                          date_from="2020-01-01", date_to="2026-01-01"))
    assert "лимит" in str(err2.value).lower()


async def test_download_network_error_is_readable(fake_ccxt):
    fake_ccxt(make_candles(100), fail_after=0)
    from app.history import download_candles
    with pytest.raises(HistoryError) as err:
        await download_candles(settings(), HistoryRequest(symbol="BTC/USDT", limit=100))
    assert "биржа" in str(err.value).lower()


async def test_download_keeps_partial_data_when_connection_breaks(fake_ccxt):
    tf_ms = 300_000
    fake_ccxt(make_candles(10_000, tf_ms=tf_ms), fail_after=2)
    from app.history import download_candles
    candles = await download_candles(settings(), HistoryRequest(symbol="BTC/USDT", timeframe="5m",
                                                               limit=5000))
    assert len(candles) >= 1500, "часть скачанных свечей не должна теряться"


# ------------------------------------------------------------------------ CSV
def test_read_csv_tradingview_style():
    text = ("time,open,high,low,close,Volume\n"
            "2026-01-01T00:00:00,60000,60100,59900,60050,12.5\n"
            "2026-01-01T00:05:00,60050,60200,60000,60150,10\n")
    candles = read_csv(text)
    assert len(candles) == 2
    assert candles[0].close == 60050 and candles[0].volume == 12.5
    assert candles[1].ts - candles[0].ts == 300_000


def test_read_csv_gate_style_milliseconds_without_header():
    text = ("1700000000000,60000,60100,59900,60050,1\n"
            "1700000300000,60050,60200,60000,60150,2\n")
    candles = read_csv(text)
    assert len(candles) == 2 and candles[0].ts == 1_700_000_000_000


def test_read_csv_semicolon_and_decimal_comma():
    text = ("ts;open;high;low;close;volume\n"
            "1700000000000;60000,5;60100,5;59900,5;60050,5;1,5\n"
            "1700000300000;60050,5;60200,5;60000,5;60150,5;2,5\n")
    candles = read_csv(text)
    assert candles[0].open == 60000.5 and candles[0].volume == 1.5


def test_read_csv_skips_broken_rows_and_reports_empty():
    text = ("ts,open,high,low,close\n"
            "1700000000000,60000,60100,59900,60050\n"
            "мусор,,,,\n"
            "1700000300000,60050,60200,60000,60150\n")
    assert len(read_csv(text)) == 2
    with pytest.raises(HistoryError):
        read_csv("")
    with pytest.raises(HistoryError) as err:
        read_csv("ts,open,high,low\n1700000000000,1,1,1\n")
    assert "колонки" in str(err.value)


def test_csv_roundtrip():
    candles = make_candles(50)
    text = write_csv(candles)
    again = read_csv(text)
    assert [c.ts for c in again] == [c.ts for c in candles]
    assert again[-1].close == pytest.approx(candles[-1].close)


def test_dedupe_sorts_and_drops_duplicates():
    a = Candle(ts=1000, open=1, high=2, low=0.5, close=1.5)
    b = Candle(ts=1000, open=9, high=9, low=9, close=9)   # перезапишет первую
    c = Candle(ts=500, open=1, high=1, low=1, close=1)
    out = dedupe([a, b, c])
    assert [x.ts for x in out] == [500, 1000] and out[-1].close == 9


def test_describe_counts_gaps():
    tf_ms = 300_000
    candles = make_candles(10, tf_ms=tf_ms)
    candles.append(Candle(ts=candles[-1].ts + 5 * tf_ms, open=1, high=1, low=1, close=1))
    info = describe(candles, "5m")
    assert info["count"] == 11 and info["gaps"] == 1 and info["from_utc"]


# ---------------------------------------------------------------------- файлы
def test_files_are_saved_listed_and_named_safely(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANB_DATA_DIR", str(tmp_path))
    from app.history import save_file
    name = safe_filename("Мои свечи за январь.csv")
    assert "/" not in name and name.endswith(".csv")
    assert safe_filename("../../etc/passwd") == "etc_passwd.csv"
    save_file(name, write_csv(make_candles(30)))
    files = list_files()
    assert any(f["name"] == name and f.get("count") == 30 for f in files)
    info = file_info("нет-такого.csv")
    assert info["exists"] is False


def test_downloaded_filename_contains_market_and_timeframe():
    req = HistoryRequest(symbol="BTC/USDT", market_type="futures", timeframe="15m",
                         date_from="2026-01-01", date_to="2026-01-05")
    name = downloaded_filename(req)
    assert name.lower().startswith("gate_btcusdt_futures_15m_")
    assert file_path(name).name == name


# ----------------------------------------------------------- API: файлы и задания
def make_app(tmp_path: str):
    from app.main import create_app
    return create_app(os.path.join(tmp_path, "bt.sqlite3"))


@pytest.fixture
async def client(tmp_path) -> httpx.AsyncClient:
    app = make_app(str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def test_csv_upload_endpoint_and_file_list(client: httpx.AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setenv("PLANB_DATA_DIR", str(tmp_path))
    text = write_csv(make_candles(120))
    res = await client.post("/api/backtest/files?name=привет.csv", content=text.encode(),
                            headers={"Content-Type": "text/csv"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] and body["candles"] == 120
    saved = body["file"]["name"]
    files = (await client.get("/api/backtest/files")).json()["files"]
    assert any(f["name"] == saved for f in files)
    # скачивание и удаление
    raw = await client.get(f"/api/backtest/files/{saved}")
    assert raw.status_code == 200 and "ts,datetime_utc" in raw.text
    assert (await client.delete(f"/api/backtest/files/{saved}")).json()["ok"] is True
    assert (await client.get(f"/api/backtest/files/{saved}")).status_code == 404

    bad = await client.post("/api/backtest/files?name=bad.csv",
                            content="мусор,мусор\n1,2\n".encode())
    assert bad.status_code == 400 and "колонки" in bad.json()["error"]


async def test_backtest_job_runs_on_synthetic_and_reports_progress(client: httpx.AsyncClient):
    res = await client.post("/api/backtest/job", json={"source": "synthetic", "limit_candles": 1200})
    assert res.status_code == 200, res.text
    job_id = res.json()["job"]["id"]
    for _ in range(60):
        status = (await client.get(f"/api/backtest/job/{job_id}")).json()
        if status["state"] in ("done", "error", "cancelled"):
            break
        import asyncio
        await asyncio.sleep(0.05)
    assert status["state"] == "done", status
    assert status["progress"]["percent"] == 100
    assert status["data"]["source"] == "synthetic"
    result = status["result"]
    assert result["candles"] == 1200 and result["cycles"] >= 0
    assert result["data"]["count"] == 1200
    # задание сохранилось в историю бэктестов
    listing = (await client.get("/api/backtests")).json()["backtests"]
    assert any(item["id"] == status["result_id"] for item in listing)


async def test_backtest_job_csv_source(client: httpx.AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setenv("PLANB_DATA_DIR", str(tmp_path))
    text = write_csv(make_candles(900))
    uploaded = (await client.post("/api/backtest/files?name=my.csv", content=text.encode())).json()
    name = uploaded["file"]["name"]
    started = (await client.post("/api/backtest/job", json={"source": "csv", "file": name,
                                                            "timeframe": "5m"})).json()
    job_id = started["job"]["id"]
    import asyncio
    for _ in range(60):
        status = (await client.get(f"/api/backtest/job/{job_id}")).json()
        if status["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert status["state"] == "done", status
    assert status["result"]["candles"] == 900
    assert status["data"]["file"] == name

    missing = (await client.post("/api/backtest/job", json={"source": "csv", "file": "нет.csv"})).json()
    for _ in range(60):
        status = (await client.get(f"/api/backtest/job/{missing['job']['id']}")).json()
        if status["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert status["state"] == "error" and "не найден" in status["error"]


async def test_backtest_sync_endpoint_still_works_with_gate_source(client: httpx.AsyncClient,
                                                                   fake_ccxt):
    fake_ccxt(make_candles(4000))
    res = await client.post("/api/backtest", json={"source": "gate", "limit_candles": 1500,
                                                  "symbol": "BTC/USDT", "timeframe": "5m"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] and body["candles"] == 1500
    assert body["data"]["source"] == "gate" and body["data"]["label"].startswith("история Gate.io")


async def test_state_exposes_data_source_and_real_flag(client: httpx.AsyncClient):
    state = (await client.get("/api/state")).json()
    assert state["data_source"] in ("gate", "synthetic")
    assert state["data_source_label"]
    assert "feed" in state
    assert "Cache-Control" in (await client.get("/api/state")).headers
    index = await client.get("/")
    assert index.status_code == 200
    assert "no-store" in index.headers.get("cache-control", "")


async def test_test_connection_without_keys_checks_public_data(client: httpx.AsyncClient,
                                                             monkeypatch):
    called = {}

    async def fake_check(candidate):
        called["symbol"] = candidate.symbol
        return {"ok": True, "real_data": True, "price": 60_000.0, "symbol": candidate.symbol,
                "timeframe": candidate.timeframe, "latency_ms": 42}

    monkeypatch.setattr("app.main.check_gate_connection", fake_check)
    res = await client.post("/api/settings/test-connection", json={"data_source": "gate"})
    body = res.json()
    assert body["ok"] and called["symbol"] == "BTC/USDT"
    assert body["keys_present"] is False and "без API-ключей" in body["note"]

    synth = await client.post("/api/settings/test-connection", json={"data_source": "synthetic"})
    assert synth.json()["synthetic"] is True
