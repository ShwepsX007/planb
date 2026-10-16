"""Реальная историческая история свечей для бэктеста.

Два способа получить данные:

* **Скачать с биржи** — ``download_candles`` тянет свечи Gate.io через публичный
  API (ключи не нужны, интернет — нужен). Данные страницами по 1000 свечей,
  поэтому доступны любые периоды и таймфреймы.
* **Загрузить свой файл** — ``read_csv`` понимает CSV почти любого вида:
  TradingView, Gate.io, выгрузки из Excel (``;`` или ``,``), время в секундах,
  миллисекундах или в виде даты. Формат описан в README и в самой панели.

Скачанные свечи складываются в ``data/candles/*.csv``, чтобы один и тот же
период можно было прогнать повторно (в том числе с другими параметрами)
без новой загрузки с биржи.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .config import TIMEFRAMES, BotSettings
from .models import Candle, now_ms

#: сколько свечей максимум имеет смысл тянуть (защита от случайного запроса на годы 1m-свечей)
MAX_CANDLES = 200_000
#: максимальная страница истории Gate.io/ccxt
PAGE_LIMIT = 1000
#: сколько свечей просим у биржи в тестовом режиме
SAMPLE_LIMIT = 1000


class HistoryError(RuntimeError):
    """Ошибка, которую можно показать пользователю в панели."""


# --------------------------------------------------------------------- каталог
def data_dir() -> Path:
    root = Path(os.environ.get("PLANB_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
    return root


def history_dir() -> Path:
    """Каталог со свечами (CSV). Создаётся при первом обращении."""
    path = data_dir() / "candles"
    path.mkdir(parents=True, exist_ok=True)
    return path


_SAFE_NAME = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._\- ]+")
_CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"


def _translit(text: str) -> str:
    table = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
             "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
             "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
             "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
             "я": "ya"}
    return "".join(table.get(ch, ch) for ch in text.lower())


def safe_filename(name: str) -> str:
    """Имя файла без путей и опасных символов (кириллица транслитерируется)."""
    name = (name or "").strip()
    name = _translit(name)
    name = _SAFE_NAME.sub("_", name).strip(" ._-")
    if not name:
        name = "candles"
    if not name.lower().endswith(".csv"):
        name += ".csv"
    return name[:120]


def file_path(name: str) -> Path:
    return history_dir() / safe_filename(name)


# ---------------------------------------------------------------- источник
@dataclass
class HistoryRequest:
    """Что именно скачивать и на чём тестировать."""

    source: str = "gate"                 # gate | csv | synthetic
    symbol: str = "BTC/USDT"
    market_type: str = "futures"         # spot | futures
    timeframe: str = "5m"
    limit: Optional[int] = None          # сколько свечей (если не задан период)
    date_from: Optional[Any] = None      # «2026-01-31», «2026-01-31 12:00», мс, сек
    date_to: Optional[Any] = None
    file: str = ""                       # имя CSV в data/candles для source=csv
    seed: int = 7

    def describe(self) -> Dict[str, Any]:
        return {"source": self.source, "symbol": self.symbol, "market_type": self.market_type,
                "timeframe": self.timeframe, "limit": self.limit, "file": self.file,
                "date_from": _fmt_ts(parse_time_ms(self.date_from)) if self.date_from else None,
                "date_to": _fmt_ts(parse_time_ms(self.date_to)) if self.date_to else None}


def _fmt_ts(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def parse_time_ms(value: Any) -> Optional[int]:
    """Понимает «2026-01-31», «31.01.2026 12:00», секунды и миллисекунды.

    Все даты трактуются как **UTC** (в панели это тоже написано).
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number * 1000) if number < 1e11 else int(number)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        number = int(text)
        return number * 1000 if number < 10**11 else number
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_time_ms(float(text))
    formats = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
               "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%d/%m/%Y")
    for fmt in formats:
        try:
            naive = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    raise HistoryError(f"не понял дату «{value}»: нужен вид 2026-01-31 или 2026-01-31 12:00 (время UTC)")


def estimate_candles(req: HistoryRequest) -> Optional[int]:
    """Сколько свечей получится (для прогресса и проверки лимитов)."""
    tf_ms = TIMEFRAMES.get(req.timeframe, 300) * 1000
    start = parse_time_ms(req.date_from)
    end = parse_time_ms(req.date_to)
    if start and end:
        if end <= start:
            raise HistoryError("конец периода раньше начала")
        return max(1, int((end - start) / tf_ms))
    if req.limit:
        return int(req.limit)
    return None


# ------------------------------------------------------------- загрузка свечей
def normalize_symbol(symbol: str, market_type: str) -> str:
    """«btc_usdt» → «BTC/USDT» (для фьючерсов «BTC/USDT:USDT» — как принято в ccxt)."""
    text = (symbol or "").strip().upper().replace("-", "/").replace("_", "/").replace(" ", "")
    if "/" not in text:
        raise HistoryError(f"не понял торговую пару «{symbol}»: нужен вид BTC/USDT")
    base, _, rest = text.partition("/")
    quote = rest.split(":")[0]
    if not base or not quote:
        raise HistoryError(f"не понял торговую пару «{symbol}»: нужен вид BTC/USDT")
    if market_type == "futures" and ":" not in text:
        return f"{base}/{quote}:{quote}"
    return text


ProgressFn = Callable[[int, Optional[int], str], None]


async def download_candles(settings: BotSettings, req: HistoryRequest,
                           progress: Optional[ProgressFn] = None) -> List[Candle]:
    """Скачать закрытые свечи с Gate.io (публичный API, ключи не нужны).

    Страницами по ``PAGE_LIMIT`` свечей; ``progress(получено, всего, текст)``
    вызывается после каждой страницы — панель показывает ход загрузки.
    """
    import ccxt.async_support as ccxt  # импорт внутри, чтобы модуль читался без сети

    if req.timeframe not in TIMEFRAMES:
        raise HistoryError(f"неизвестный таймфрейм «{req.timeframe}»")
    tf_ms = TIMEFRAMES[req.timeframe] * 1000
    symbol = normalize_symbol(req.symbol, req.market_type)
    until = parse_time_ms(req.date_to) or now_ms()
    start = parse_time_ms(req.date_from)
    total = estimate_candles(req)
    if total and total > MAX_CANDLES:
        raise HistoryError(
            f"получится {total:,} свечей — это больше лимита {MAX_CANDLES:,}. "
            f"Возьмите таймфрейм крупнее или период короче".replace(",", " "))
    if start is None:
        limit = int(req.limit or 3000)
        if limit > MAX_CANDLES:
            raise HistoryError(f"лимит свечей — {MAX_CANDLES}")
        # просим на одну свечу больше: последняя (ещё не закрытая) отфильтруется,
        # и пользователь получит ровно столько свечей, сколько указал
        start = until - (limit + 1) * tf_ms

    options = {"defaultType": "swap" if req.market_type == "futures" else "spot"}
    ex = ccxt.gate({"enableRateLimit": True, "timeout": 30000, "options": options})
    out: List[Candle] = []
    try:
        try:
            await ex.load_markets()
        except Exception as exc:
            raise HistoryError(f"нет связи с Gate.io: {exc}") from exc
        if symbol not in ex.markets:
            close = [s for s in ex.markets if s.split(":")[0] == symbol.split(":")[0]][:5]
            hint = f", похожие: {', '.join(close)}" if close else ""
            raise HistoryError(f"на Gate.io нет пары {symbol} ({req.market_type}){hint}")

        since = start
        pages = 0
        while True:
            pages += 1
            try:
                rows = await ex.fetch_ohlcv(symbol, req.timeframe, since=since, limit=PAGE_LIMIT)
            except Exception as exc:
                if out:
                    break          # часть данных уже есть — не теряем её
                raise HistoryError(f"биржа не отдала историю: {exc}") from exc
            if not rows:
                break
            batch = [Candle.from_ccxt(r) for r in rows]
            batch = [c for c in batch if c.ts + tf_ms <= until + 500]
            if not batch:
                break
            out.extend(batch)
            if progress:
                progress(len(out), total, f"скачано {len(out)} свечей")
            last_ts = batch[-1].ts
            if last_ts + tf_ms >= until:      # дошли до нужного конца
                break
            if last_ts < since:               # биржа не двигается вперёд — выходим
                break
            since = last_ts + tf_ms
            if total and len(out) >= total:
                break
            if len(out) >= MAX_CANDLES:
                break
            if pages > MAX_CANDLES // PAGE_LIMIT + 50:
                break
            await asyncio.sleep(0)            # не блокируем другие задачи сервера
    finally:
        try:
            await ex.close()
        except Exception:
            pass

    candles = dedupe(out)
    if start and not req.date_from:
        candles = candles[-int(req.limit or len(candles)):]
    if total and not req.date_from:
        candles = candles[-total:]
    if progress:
        progress(len(candles), len(candles) or None, f"загружено {len(candles)} свечей")
    if len(candles) < 10:
        raise HistoryError(
            f"получилось всего {len(candles)} свечей — проверьте пару, таймфрейм и период")
    return candles


def dedupe(candles: Iterable[Candle]) -> List[Candle]:
    """Убрать дубли и отсортировать по времени (биржа может отдать накладки)."""
    unique: Dict[int, Candle] = {}
    for c in candles:
        unique[c.ts] = c
    return [unique[k] for k in sorted(unique)]


# ------------------------------------------------------------------ CSV
_HEADER_ALIASES = {
    "time": "ts", "ts": "ts", "timestamp": "ts", "date": "ts", "datetime": "ts", "unix": "ts",
    "время": "ts", "дата": "ts", "time_open": "ts", "opentime": "ts",
    "open": "open", "откр": "open", "открытие": "open",
    "high": "high", "макс": "high", "максимум": "high",
    "low": "low", "мин": "low", "минимум": "low",
    "close": "close", "закр": "close", "закрытие": "close", "цена": "close", "price": "close",
    "volume": "volume", "vol": "volume", "объем": "volume", "объём": "volume",
    "basevolume": "volume", "quotevolume": "quote_volume",
}
_REQUIRED = ("ts", "open", "high", "low", "close")


def _detect_delimiter(line: str) -> str:
    counts = {",": line.count(","), ";": line.count(";"), "\t": line.count("\t")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] else ","


def _to_float(text: str, *, decimal_comma: bool = False) -> float:
    text = (text or "").strip().replace(" ", "").replace("\u00a0", "")
    if decimal_comma and "," in text and "." not in text:
        text = text.replace(",", ".")
    return float(text)


def read_csv(text: str) -> List[Candle]:
    """Разобрать CSV со свечами (с заголовком или без).

    Минимально нужны колонки времени и open/high/low/close (volume не обязателен).
    """
    if not text or not text.strip():
        raise HistoryError("файл пустой")
    text = text.lstrip("\ufeff")
    raw_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not raw_lines:
        raise HistoryError("в файле нет строк с данными")
    delimiter = _detect_delimiter(raw_lines[0])
    rows = list(csv.reader(raw_lines, delimiter=delimiter))
    rows = [r for r in rows if any(str(cell).strip() for cell in r)]
    if not rows:
        raise HistoryError("в файле нет строк с данными")

    decimal_comma = delimiter == ";"
    header: Optional[List[str]] = None
    first = [str(c).strip().lower() for c in rows[0]]
    looks_numeric = re.fullmatch(r"-?\d+(\.\d+)?", first[0] or "") or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}.*", first[0] or "")
    if not looks_numeric:
        header = [_HEADER_ALIASES.get(c, c) for c in first]
        rows = rows[1:]
    if not rows:
        raise HistoryError("в файле только заголовок")

    index: Dict[str, int] = {}
    if header:
        for pos, name in enumerate(header):
            if name in _REQUIRED and name not in index:
                index[name] = pos
        missing = [k for k in _REQUIRED if k not in index]
        if missing:
            raise HistoryError(
                "в заголовке CSV не нашёл колонки: " + ", ".join(missing) +
                ". Нужен файл вида ts,open,high,low,close,volume")
        vol_pos = header.index("volume") if "volume" in header else None
    else:
        if len(rows[0]) < 5:
            raise HistoryError("в строке меньше 5 колонок — нужен формат ts,open,high,low,close,volume")
        index = {"ts": 0, "open": 1, "high": 2, "low": 3, "close": 4}
        vol_pos = 5

    candles: List[Candle] = []
    bad = 0
    for row in rows:
        if len(row) < 5:
            bad += 1
            continue
        try:
            ts_ms = parse_time_ms(row[index["ts"]])
            if ts_ms is None:
                bad += 1
                continue
            o = _to_float(row[index["open"]], decimal_comma=decimal_comma)
            h = _to_float(row[index["high"]], decimal_comma=decimal_comma)
            l = _to_float(row[index["low"]], decimal_comma=decimal_comma)
            c = _to_float(row[index["close"]], decimal_comma=decimal_comma)
            v = 0.0
            if vol_pos is not None and len(row) > vol_pos and str(row[vol_pos]).strip():
                try:
                    v = _to_float(row[vol_pos], decimal_comma=decimal_comma)
                except ValueError:
                    v = 0.0
        except (ValueError, IndexError, HistoryError):
            bad += 1
            continue
        if min(o, h, l, c) <= 0 or h < l:
            bad += 1
            continue
        candles.append(Candle(ts=ts_ms, open=o, high=h, low=l, close=c, volume=v))

    candles = dedupe(candles)
    if len(candles) < 2:
        raise HistoryError(
            f"в файле нашлось только {len(candles)} корректных свечей (пропущено строк: {bad}). "
            "Нужен CSV с колонками ts,open,high,low,close,volume (заголовок не обязателен)")
    return candles


def write_csv(candles: Sequence[Candle]) -> str:
    """Свечи → CSV (так же их можно открыть в Excel и посмотреть глазами)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["ts", "datetime_utc", "open", "high", "low", "close", "volume"])
    for c in candles:
        writer.writerow([c.ts, _fmt_ts(c.ts), f"{c.open:g}", f"{c.high:g}", f"{c.low:g}",
                         f"{c.close:g}", f"{c.volume:g}"])
    return buf.getvalue()


def save_file(name: str, text: str) -> Path:
    path = file_path(name)
    path.write_text(text, encoding="utf-8")
    return path


def describe(candles: Sequence[Candle], timeframe: str = "5m") -> Dict[str, Any]:
    if not candles:
        return {"count": 0}
    tf_ms = TIMEFRAMES.get(timeframe, 300) * 1000
    return {
        "count": len(candles),
        "from_ts": candles[0].ts,
        "to_ts": candles[-1].ts,
        "from_utc": _fmt_ts(candles[0].ts),
        "to_utc": _fmt_ts(candles[-1].ts),
        "span_days": round((candles[-1].ts - candles[0].ts) / 86_400_000, 2),
        "timeframe": timeframe,
        "tf_ms": tf_ms,
        "gaps": sum(1 for a, b in zip(candles, candles[1:]) if b.ts - a.ts > tf_ms * 1.5),
    }


def file_info(name: str) -> Dict[str, Any]:
    """Краткая информация о CSV-файле со свечами (для списка в панели)."""
    path = file_path(name)
    info: Dict[str, Any] = {"name": path.name, "exists": path.exists()}
    if not path.exists():
        return info
    info["size_kb"] = round(path.stat().st_size / 1024, 1)
    info["modified_ts"] = int(path.stat().st_mtime * 1000)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(200_000)
        candles = read_csv(head)
        info.update({k: v for k, v in describe(candles).items() if k != "timeframe"})
        info["ok"] = True
    except Exception as exc:
        info["ok"] = False
        info["error"] = str(exc)
    return info


def list_files() -> List[Dict[str, Any]]:
    directory = history_dir()
    files = sorted((p for p in directory.glob("*.csv")), key=lambda p: p.stat().st_mtime, reverse=True)
    return [file_info(p.name) for p in files[:60]]


def delete_file(name: str) -> bool:
    path = file_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def downloaded_filename(req: HistoryRequest) -> str:
    """Имя файла для скачанной с биржи истории."""
    base = req.symbol.split(":")[0].replace("/", "")
    stamps = []
    start, end = parse_time_ms(req.date_from), parse_time_ms(req.date_to)
    if start:
        stamps.append(_fmt_ts(start)[:10])
    if end:
        stamps.append(_fmt_ts(end)[:10])
    tail = "_".join(stamps) if stamps else f"{req.limit or 0}"
    return safe_filename(f"gate_{base}_{req.market_type}_{req.timeframe}_{tail}.csv")
