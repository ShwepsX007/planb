"""Глубокая история Gate.io из официальных месячных архивов.

Биржа выкладывает свечи файлами по месяцам (ключи и регистрация не нужны):

    https://download.gatedata.org/{biz}/candlesticks_{tf}/{YYYYMM}/{MARKET}-{YYYYMM}.csv.gz

где ``biz`` — ``spot``, ``futures_usdt`` или ``futures_btc``, а ``MARKET`` — пара
через подчёркивание (``BTC_USDT``). В файле по одной свече в строке, порядок
колонок: ``timestamp, volume, close, high, low, open`` (у фьючерсов вместо
volume — size в контрактах).

Почему это нужно: обычный REST-API Gate.io отдаёт по фьючерсам только последние
~10 000 свечей («Candlestick too long ago»), то есть на 5m это около месяца.
Архивы же покрывают историю с 2023 года, поэтому бэктест можно гонять на 1–3
годах и более без ключей и без ручной загрузки файлов.

Данные кешируются в ``data/history``: один раз скачанные месяцы повторно не
качаются, а текущий (незакрытый) месяц обновляется при каждом запуске. Для 3m,
15m и 30m скачиваются минутные архивы и «сворачиваются» в нужный таймфрейм —
отдельных архивов таких таймфреймов у Gate.io нет.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

from .config import TIMEFRAMES
from .history import (HistoryError, data_dir, dedupe, describe as describe_candles,
                      parse_time_ms, write_csv)
from .models import Candle, now_ms

#: адрес архива (можно переопределить своим зеркалом через переменную окружения)
DEFAULT_BASE_URL = "https://download.gatedata.org"
#: первый месяц, за который Gate.io публикует свечные архивы
DEFAULT_FIRST_MONTH = "202301"
#: сколько свечей максимум разрешаем прогнать одним бэктестом
DEFAULT_MAX_CANDLES = 2_000_000

#: таймфреймы, которые есть в архиве напрямую
DIRECT_TIMEFRAMES = ("1m", "5m", "1h", "4h", "1d")
#: остальные собираем из более мелкого архива: целевой -> (базовый, во сколько раз)
RESAMPLE_FROM: Dict[str, Tuple[str, int]] = {
    "3m": ("1m", 3),
    "15m": ("5m", 3),
    "30m": ("5m", 6),
    "2h": ("1h", 2),
    "6h": ("1h", 6),
    "8h": ("1h", 8),
    "12h": ("1h", 12),
}

ProgressFn = Callable[[int, Optional[int], str], None]


class ArchiveNotFound(HistoryError):
    """Месяца нет в архиве (пара ещё не торговалась или файл не выложен)."""


# ------------------------------------------------------------------ настройки
def base_url() -> str:
    return (os.environ.get("PLANB_ARCHIVE_BASE") or DEFAULT_BASE_URL).rstrip("/")


def first_month() -> str:
    value = re.sub(r"[^0-9]", "", os.environ.get("PLANB_ARCHIVE_START", "") or DEFAULT_FIRST_MONTH)
    return (value or DEFAULT_FIRST_MONTH)[:6]


def max_candles() -> int:
    try:
        return max(10_000, int(os.environ.get("PLANB_ARCHIVE_MAX_CANDLES", DEFAULT_MAX_CANDLES)))
    except ValueError:
        return DEFAULT_MAX_CANDLES


def archive_dir() -> Path:
    path = data_dir() / "history"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------- бизнес и пара
def quote_of(symbol: str) -> str:
    text = (symbol or "BTC/USDT").upper().replace("-", "/").replace("_", "/")
    _, _, rest = text.partition("/")
    return rest.split(":")[0].strip() or "USDT"


def market_name(symbol: str) -> str:
    """«BTC/USDT:USDT» → «BTC_USDT» (как в именах файлов Gate.io)."""
    text = (symbol or "").strip().upper().replace("-", "/").replace("_", "/").replace(" ", "")
    if "/" not in text:
        raise HistoryError(f"не понял торговую пару «{symbol}»: нужен вид BTC/USDT")
    base, _, rest = text.partition("/")
    quote = rest.split(":")[0]
    base = re.sub(r"[^0-9A-Z.]", "", base)
    quote = re.sub(r"[^0-9A-Z.]", "", quote)
    if not base or not quote:
        raise HistoryError(f"не понял торговую пару «{symbol}»: нужен вид BTC/USDT")
    return f"{base}_{quote}"


def biz_name(market_type: str, symbol: str) -> str:
    """Каталог архива: spot, futures_usdt (USDT-M) или futures_btc (BTC-M)."""
    if (market_type or "futures").lower() == "spot":
        return "spot"
    # BTC-маржинальные контракты Gate.io котируются в USD и считаются в BTC
    return "futures_btc" if quote_of(symbol) in ("BTC", "USD") else "futures_usdt"


def archive_timeframe(timeframe: str) -> Tuple[str, int]:
    """Какой архив качать и во сколько раз «сворачивать» свечи."""
    tf = (timeframe or "5m").strip()
    if tf in TIMEFRAMES and tf in DIRECT_TIMEFRAMES:
        return tf, 1
    if tf in RESAMPLE_FROM:
        base, factor = RESAMPLE_FROM[tf]
        return base, factor
    raise HistoryError(
        f"для архива Gate.io нет таймфрейма «{tf}»: доступны "
        f"{', '.join(DIRECT_TIMEFRAMES)} и производные {', '.join(RESAMPLE_FROM)}")


def archive_url(symbol: str, market_type: str, timeframe: str, month: str) -> str:
    base_tf, _ = archive_timeframe(timeframe)
    return (f"{base_url()}/{biz_name(market_type, symbol)}/candlesticks_{base_tf}/"
            f"{month}/{market_name(symbol)}-{month}.csv.gz")


# --------------------------------------------------------------------- месяцы
def _month_start(year: int, month: int) -> dt.datetime:
    return dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)


def _next_month(moment: dt.datetime) -> dt.datetime:
    return (moment.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def month_key(moment: dt.datetime) -> str:
    return moment.strftime("%Y%m")


def months_between(start_ms: int, end_ms: int) -> List[str]:
    """Список месяцев ``YYYYMM``, покрывающих период [start, end].

    Если ``end`` совпадает с началом месяца (например ``2024-03-01 00:00``),
    этот месяц уже не нужен: тестируем по последнюю свечу февраля.
    """
    start = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    end = dt.datetime.fromtimestamp(end_ms / 1000, tz=dt.timezone.utc)
    out: List[str] = []
    while start < end:
        out.append(month_key(start))
        start = _next_month(start)
    if not out:
        out.append(month_key(dt.datetime.fromtimestamp(
            start_ms / 1000, tz=dt.timezone.utc)))
    return out


def default_period() -> Tuple[int, int]:
    """Если период не задан — последние два года по сегодня."""
    end = now_ms()
    start = dt.datetime.now(tz=dt.timezone.utc)
    start = start.replace(year=start.year - 2)
    return int(start.timestamp() * 1000), end


# ---------------------------------------------------------------------- парсер
def _looks_like_time(text: str) -> bool:
    text = (text or "").strip()
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", text) or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text))


def _pick_order(rows: Sequence[Sequence[str]]) -> List[str]:
    """Понять порядок колонок: у Gate.io он не такой, как в привычном CSV.

    Gate:     timestamp, volume, close, high, low, open
    обычный:  timestamp, open, high, low, close, volume
    """
    gate = ["ts", "volume", "close", "high", "low", "open"]
    usual = ["ts", "open", "high", "low", "close", "volume"]
    counts = {"gate": 0, "usual": 0}
    for row in rows[:30]:
        if len(row) < 6:
            continue
        try:
            ts = parse_time_ms(row[0])
            gate_ok = _row_ok(row, gate, ts)
            usual_ok = _row_ok(row, usual, ts)
        except Exception:
            continue
        counts["gate"] += 1 if gate_ok else 0
        counts["usual"] += 1 if usual_ok else 0
    return gate if counts["gate"] >= counts["usual"] else usual


def _row_ok(row: Sequence[str], order: Sequence[str], ts: Optional[int]) -> bool:
    if ts is None:
        return False
    values: Dict[str, float] = {}
    for name in ("open", "high", "low", "close", "volume"):
        pos = order.index(name)
        if pos >= len(row):
            return False
        raw = str(row[pos]).strip()
        if raw == "":
            values[name] = 0.0
            continue
        try:
            values[name] = float(raw)
        except ValueError:
            return False
    o, h, l, c = values["open"], values["high"], values["low"], values["close"]
    if min(o, h, l, c) <= 0 or h < l:
        return False
    eps = abs(h) * 1e-9 + 1e-12
    return h + eps >= max(o, c) and l - eps <= min(o, c)


def parse_archive_csv(text: str) -> List[Candle]:
    """Разобрать свечной архив Gate.io (CSV внутри .gz).

    Понимает и файлы с заголовком, и файлы без него, а порядок колонок
    определяет по данным — поэтому не сломается, если Gate.io его поменяет.
    """
    if not text or not text.strip():
        raise HistoryError("архив пустой")
    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise HistoryError("в архиве нет строк с данными")
    delimiter = "," if lines[0].count(",") >= lines[0].count(";") else ";"
    rows = [ln.split(delimiter) for ln in lines]
    rows = [[cell.strip() for cell in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise HistoryError("в архиве нет строк с данными")

    header: Optional[Dict[str, int]] = None
    if not _looks_like_time(rows[0][0]):
        alias = {"time": "ts", "timestamp": "ts", "ts": "ts", "date": "ts", "open_time": "ts",
                 "open": "open", "high": "high", "low": "low", "close": "close",
                 "volume": "volume", "volumn": "volume", "vol": "volume", "size": "volume"}
        header = {}
        for pos, name in enumerate(rows[0]):
            mapped = alias.get(name.strip().lower())
            if mapped and mapped not in header:
                header[mapped] = pos
        rows = rows[1:]
        if not {"ts", "open", "high", "low", "close"}.issubset(header):
            header = None                      # непонятный заголовок — разбираем по данным

    order = None if header is not None else _pick_order(rows)

    candles: List[Candle] = []
    bad = 0
    for row in rows:
        if len(row) < 5:
            bad += 1
            continue
        try:
            ts = parse_time_ms(row[0])
            if ts is None:
                bad += 1
                continue
            if header is not None:
                values = {}
                for name, pos in header.items():
                    if name == "ts":
                        continue
                    raw = row[pos].strip() if pos < len(row) else ""
                    values[name] = float(raw) if raw else 0.0
                volume = values.get("volume", 0.0)
            else:
                values = {name: float(row[order.index(name)]) for name in
                          ("open", "high", "low", "close")}
                vol_pos = order.index("volume") if "volume" in order else None
                volume = float(row[vol_pos]) if vol_pos is not None and vol_pos < len(row) else 0.0
            o, h, l, c = values["open"], values["high"], values["low"], values["close"]
        except (ValueError, IndexError, HistoryError):
            bad += 1
            continue
        if min(o, h, l, c) <= 0 or h < l:
            bad += 1
            continue
        eps = abs(h) * 1e-9 + 1e-12
        if h + eps < max(o, c) or l - eps > min(o, c):
            bad += 1
            continue
        candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=volume))

    candles = dedupe(candles)
    if len(candles) < 2:
        raise HistoryError(
            f"в архиве нашлось только {len(candles)} корректных свечей "
            f"(пропущено строк: {bad}) — файл не похож на свечной архив Gate.io")
    return candles


# ------------------------------------------------------------------ ресемплинг
def resample(candles: Sequence[Candle], base_tf: str, target_tf: str) -> List[Candle]:
    """Свернуть минутные свечи в 3m/15m/30m и т.п. (таймфреймы без архива)."""
    if base_tf == target_tf:
        return list(candles)
    base_ms = TIMEFRAMES[base_tf] * 1000
    target_ms = TIMEFRAMES[target_tf] * 1000
    if target_ms % base_ms:
        raise HistoryError(f"нельзя собрать {target_tf} из {base_tf}")
    groups: Dict[int, Dict[str, float]] = {}
    for c in candles:
        key = c.ts - c.ts % target_ms
        item = groups.get(key)
        if item is None:
            groups[key] = {"open": c.open, "high": c.high, "low": c.low,
                           "close": c.close, "volume": c.volume, "_first": c.ts}
            continue
        item["high"] = max(item["high"], c.high)
        item["low"] = min(item["low"], c.low)
        item["close"] = c.close
        item["volume"] += c.volume
        item["_first"] = min(item["_first"], c.ts)
    now = now_ms()
    out: List[Candle] = []
    for key in sorted(groups):
        item = groups[key]
        if key + target_ms > now:              # последняя свеча ещё не закрыта
            continue
        out.append(Candle(ts=key, open=item["open"], high=item["high"], low=item["low"],
                          close=item["close"], volume=item["volume"]))
    return out


# --------------------------------------------------------------- скачивание
async def fetch_bytes(url: str, *, attempts: int = 3, timeout: float = 90.0) -> bytes:
    """Скачать один файл архива. 404/403 — «месяца нет», остальное — с повторами."""
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                         headers={"User-Agent": "PlanB-backtest/1.0"}) as client:
                response = await client.get(url)
            if response.status_code == 404:
                raise ArchiveNotFound(f"файла нет в архиве: {url}")
            if response.status_code == 403:
                raise HistoryError(
                    f"сервер архива запретил доступ (403): {url} — проверьте, "
                    f"не блокирует ли хостинг download.gatedata.org")
            response.raise_for_status()
            return response.content
        except ArchiveNotFound:
            raise
        except Exception as exc:              # сеть, 5xx, таймаут
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(1.0 * (attempt + 1))
    raise HistoryError(f"не удалось скачать архив Gate.io: {last_error}")


def _unpack(blob: bytes) -> str:
    if blob[:2] == b"\x1f\x8b":
        try:
            blob = gzip.decompress(blob)
        except OSError as exc:
            raise HistoryError(f"не удалось распаковать архив: {exc}") from exc
    return blob.decode("utf-8", errors="replace")


# --------------------------------------------------------------------- кеш
def cache_key(symbol: str, market_type: str, timeframe: str) -> str:
    biz = biz_name(market_type, symbol)
    return f"gate_{biz}_{market_name(symbol).lower()}_{timeframe}"


def cache_paths(symbol: str, market_type: str, timeframe: str) -> Tuple[Path, Path]:
    key = cache_key(symbol, market_type, timeframe)
    folder = archive_dir()
    return folder / f"{key}.csv", folder / f"{key}.json"


def _load_meta(meta_file: Path) -> Dict[str, Any]:
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_cache(csv_file: Path) -> List[Candle]:
    if not csv_file.exists():
        return []
    from .history import read_csv
    try:
        return read_csv(csv_file.read_text(encoding="utf-8", errors="replace"))
    except HistoryError:
        return []


def list_cache() -> List[Dict[str, Any]]:
    """Что уже лежит в кеше (для панели)."""
    items: List[Dict[str, Any]] = []
    folder = archive_dir()
    for meta_file in sorted(folder.glob("gate_*.json")):
        meta = _load_meta(meta_file)
        csv_file = meta_file.with_suffix(".csv")
        if not meta or not csv_file.exists():
            continue
        items.append({
            "key": meta_file.stem,
            "file": csv_file.name,
            "symbol": meta.get("symbol", "—"),
            "market_type": meta.get("market_type", "—"),
            "timeframe": meta.get("timeframe", "—"),
            "months": len(meta.get("months", [])),
            "downloaded_months": len(meta.get("months", [])),
            "count": meta.get("count", 0),
            "from_utc": meta.get("from_utc"),
            "to_utc": meta.get("to_utc"),
            "size_mb": round(csv_file.stat().st_size / (1024 * 1024), 2),
            "updated_at": meta.get("updated_at"),
        })
    items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return items


def delete_cache(key: str) -> bool:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "", key or "")
    if not safe:
        return False
    folder = archive_dir()
    removed = False
    for suffix in (".csv", ".json"):
        path = folder / f"{safe}{suffix}"
        if path.exists():
            path.unlink()
            removed = True
    return removed


# ------------------------------------------------- добор последних свечей
def _tail_request(symbol: str, market_type: str, timeframe: str,
                  start_ms: int, end_ms: int, limit: int):
    from .history import HistoryRequest
    return HistoryRequest(source="gate", symbol=symbol, market_type=market_type,
                          timeframe=timeframe, date_from=start_ms, date_to=end_ms,
                          limit=limit)


async def _tail_candles(req):
    from .history import download_candles
    return await download_candles(None, req)          # settings в этой функции не нужен


# ------------------------------------------------------------------- синк
async def sync_archive(req, *, progress: Optional[ProgressFn] = None,
                       warnings: Optional[List[str]] = None,
                       force: bool = False,
                       fill_tail: bool = True) -> Tuple[List[Candle], Dict[str, Any]]:
    """Скачать недостающие месяцы и вернуть свечи за запрошенный период.

    ``req`` — объект с полями ``symbol``, ``market_type``, ``timeframe``,
    ``date_from``, ``date_to``, ``limit`` (например ``HistoryRequest``).
    """
    def warn(message: str) -> None:
        if warnings is not None and message not in warnings:
            warnings.append(message)

    def report(done: int, total: Optional[int], message: str) -> None:
        if progress is not None:
            progress(done, total, message)     # может бросить CancelledError

    symbol = getattr(req, "symbol", "BTC/USDT")
    market_type = getattr(req, "market_type", "futures")
    timeframe = getattr(req, "timeframe", "5m")
    base_tf, _factor = archive_timeframe(timeframe)

    start = parse_time_ms(getattr(req, "date_from", None))
    end = parse_time_ms(getattr(req, "date_to", None)) or now_ms()
    if start is None:
        limit = int(getattr(req, "limit", 0) or 0)
        start = end - limit * TIMEFRAMES[timeframe] * 1000 if limit else default_period()[0]
    if end <= start:
        raise HistoryError("конец периода раньше начала")

    first = dt.datetime.strptime(first_month(), "%Y%m").replace(tzinfo=dt.timezone.utc)
    first_ms = int(first.timestamp() * 1000)
    if start < first_ms:
        warn(f"архив Gate.io начинается с {first:%Y-%m} — история до этой даты недоступна, "
             f"бэктест пойдёт с {first:%Y-%m-%d} UTC")
        start = first_ms

    months = months_between(start, end)
    if not months:
        raise HistoryError("период не содержит ни одного месяца")
    estimate = int((end - start) / 1000 / TIMEFRAMES[timeframe])
    if estimate > max_candles():
        raise HistoryError(
            f"за этот период получится примерно {estimate:,} свечей — больше лимита "
            f"{max_candles():,}. Возьмите таймфрейм крупнее или период короче"
            .replace(",", " "))

    csv_file, meta_file = cache_paths(symbol, market_type, timeframe)
    meta = _load_meta(meta_file)
    cached_months = set(meta.get("months") or [])
    candles = _load_cache(csv_file)

    current_month = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m")
    missing = [m for m in months if force or m not in cached_months]
    # текущий месяц ещё не закрыт: его всегда перекачиваем, чтобы данные были свежими
    if not force and current_month in months and current_month not in missing:
        missing.append(current_month)
    missing = sorted(set(missing))

    downloaded = 0
    fresh: List[Candle] = []
    if missing:
        report(0, len(missing), f"скачиваю архивы Gate.io: 0 из {len(missing)} месяцев")
        for index, month in enumerate(missing, start=1):
            url = archive_url(symbol, market_type, timeframe, month)
            try:
                blob = await fetch_bytes(url)
            except ArchiveNotFound:
                warn(f"нет архива за {month[:4]}-{month[4:]} ({url}) — месяц пропущен")
                report(index, len(missing), f"нет архива за {month[:4]}-{month[4:]}")
                continue
            except HistoryError as exc:
                if not candles:
                    raise
                warn(f"{exc} — использую уже скачанные месяцы")
                break
            try:
                batch = parse_archive_csv(_unpack(blob))
            except HistoryError as exc:
                raise HistoryError(f"архив за {month[:4]}-{month[4:]} не разобрался: {exc}") from exc
            fresh.extend(batch)
            cached_months.add(month)
            downloaded += 1
            report(index, len(missing),
                   f"скачано {index} из {len(missing)} месяцев, свечей: "
                   f"{len(candles) + len(fresh)}")

    if fresh:
        candles = dedupe(list(candles) + fresh)
    if not candles:
        raise HistoryError(
            "в архиве Gate.io нет данных за выбранный период — проверьте пару, рынок и даты")

    # Архив текущего месяца Gate.io выкладывает не сразу. Если тестируем «по
    # сегодня», добираем последние свечи через обычный API — тогда результат
    # заканчивается именно сегодняшним днём, а не концом прошлого месяца.
    tail = 0
    tf_ms = TIMEFRAMES[timeframe] * 1000
    if fill_tail and candles and end >= now_ms() - 3 * 86_400_000:
        gap = end - candles[-1].ts
        if gap > 1.5 * tf_ms:
            limit = max(10, min(int(gap / tf_ms) + 2, 5000))
            tail_req = _tail_request(symbol, market_type, timeframe,
                                     candles[-1].ts + tf_ms, end, limit)
            try:
                added = await _tail_candles(tail_req)
            except Exception as exc:                    # сеть/биржа — не роняем бэктест
                warn(f"не удалось добрать последние свечи через API Gate.io: {exc}")
                added = []
            if added:
                added = [c for c in added if c.ts > candles[-1].ts]
                if added:
                    candles = dedupe(list(candles) + added)
                    tail = len(added)

    # кеш храним в ИСХОДНОМ таймфрейме архива (например 5m), а не в собранном
    # 15m: иначе при следующей докачке новые 5m-свечи смешались бы с готовыми
    # 15m и OHLC поехал бы. Целиком за все скачанные месяцы — тогда более
    # ранний период потом откроется без повторной загрузки.
    if downloaded or tail or not csv_file.exists():
        csv_file.write_text(write_csv(candles), encoding="utf-8")

    if _factor > 1:
        candles = resample(candles, base_tf, timeframe)
    candles = [c for c in candles if c.ts >= start - TIMEFRAMES[timeframe] * 1000
               and c.ts <= end]
    if len(candles) < 10:
        raise HistoryError(
            f"в архиве за выбранный период нашлось только {len(candles)} свечей — "
            f"проверьте пару, таймфрейм и даты")
    info = describe_candles(candles, timeframe)
    meta = {
        "symbol": symbol, "market_type": market_type, "timeframe": timeframe,
        "biz": biz_name(market_type, symbol), "market": market_name(symbol),
        "base_timeframe": base_tf, "months": sorted(cached_months),
        "updated_at": now_ms(), **{k: info[k] for k in ("count", "from_utc", "to_utc")},
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    info = {
        "source": "archive",
        "label": f"архив Gate.io {market_name(symbol)} {timeframe}",
        "archive": True,
        "symbol": symbol, "market_type": market_type, "timeframe": timeframe,
        "file": csv_file.name,
        "months_in_period": len(months),
        "months_downloaded": downloaded,
        "months_cached": len([m for m in months if m in cached_months]) - downloaded,
        "tail_candles": tail,
        "cached_months_total": len(cached_months),
        "resampled_from": base_tf if base_tf != timeframe else None,
        **describe_candles(candles, timeframe),
    }
    if warnings:
        info["warnings"] = list(warnings)
    return candles, info
