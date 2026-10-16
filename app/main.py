"""FastAPI application: REST API + WebSocket + web panel."""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .bot import BotRuntime
from .brokers.gate import test_credentials
from .config import DEFAULT_SETTINGS, BotSettings
from .db import Database
from .feed import check_gate_connection
from .history import (MAX_CANDLES, HistoryError, describe as describe_candles, delete_file,
                      file_info, list_files, read_candles, save_file, write_csv)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

#: preset applied on the very first start (handy for local demo)
PRESETS: Dict[str, Dict[str, Any]] = {
    # «демо-режим» = виртуальные деньги (paper) на РЕАЛЬНЫХ свечах Gate.io.
    # API-ключи не нужны, интернет нужен.
    "demo": {
        "data_source": "gate",
        "trade_mode": "paper",
        "timeframe": "5m",
        "candles_count": 3,
        "min_move_pct": 0.45,
        "fib_goal_pct": 50.0,
        "min_profit_pct": 0.2,
        "base_volume_usd": 10.0,
        "volume_multiplier": 2.0,
        "multiplier_start_position": 3,
        "max_positions": 6,
        "total_profit_target_usd": 20.0,
        "symbol": "BTC/USDT",
    },
    # полностью выдуманный рынок — только посмотреть панель без интернета
    "synthetic": {
        "data_source": "synthetic",
        "trade_mode": "paper",
        "timeframe": "1m",
        "candles_count": 3,
        "min_move_pct": 0.45,
        "fib_goal_pct": 50.0,
        "min_profit_pct": 0.2,
        "base_volume_usd": 10.0,
        "volume_multiplier": 2.0,
        "multiplier_start_position": 3,
        "max_positions": 6,
        "total_profit_target_usd": 20.0,
        "symbol": "BTC/USDT",
        "demo_speed": 30.0,
    },
}


def _db_path() -> str:
    return os.environ.get("PLANB_DB", str(ROOT / "data" / "planb.sqlite3"))


def autostart_enabled() -> bool:
    """Автозапуск бота при старте процесса (для systemd/Docker).

    По умолчанию включён: если сервер перезагрузился, открытая позиция не должна
    остаться без управления. Отключается ``PLANB_AUTOSTART=0``.
    """
    return os.environ.get("PLANB_AUTOSTART", "1").strip().lower() not in ("0", "false", "no", "off", "")


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db = Database(db_path or _db_path())

    if db.kv_get("settings") is None:
        settings = DEFAULT_SETTINGS
        preset = os.environ.get("PLANB_PRESET", "").strip().lower()
        if preset in PRESETS:
            settings = BotSettings(**{**settings.model_dump(), **PRESETS[preset]})
        db.save_settings(settings)
    settings = db.load_settings(DEFAULT_SETTINGS)

    # Миграция: раньше "demo" означало выдуманный рынок. Пользователь просил демо-режим
    # на реальных данных, поэтому переводим такие настройки на свечи Gate.io
    # (виртуальные сделки сохраняются) и пишем об этом в журнал — молча ничего не меняем.
    stored = db.kv_get("settings") or {}
    if isinstance(stored, dict) and str(stored.get("data_source", "")).lower() == "demo":
        settings.data_source = "gate"
        db.save_settings(settings)

    runtime = BotRuntime(db, settings)
    if isinstance(stored, dict) and str(stored.get("data_source", "")).lower() == "demo":
        runtime.log("info", "Источник данных переключён с выдуманного рынка на реальные свечи "
                            "Gate.io — сделки по-прежнему виртуальные (paper), API-ключи не нужны",
                    persist=False)

    auth_user = os.environ.get("PLANB_AUTH_USER", "").strip()
    auth_pass = os.environ.get("PLANB_AUTH_PASSWORD", "").strip()

    def _decode_credentials(raw: bytes) -> list:
        """Basic-auth приходит в разных кодировках: UTF-8 (RFC 7617) или Latin-1
        (старые браузеры, Windows-1251 в русских паролях). Пробуем все варианты,
        чтобы «кириллический» пароль работал в любом браузере."""
        variants = []
        for encoding in ("utf-8", "cp1251", "latin-1"):
            try:
                variants.append(raw.decode(encoding))
            except (UnicodeDecodeError, LookupError):
                continue
        return variants

    def _secure_equal(a: str, b: str) -> bool:
        """Сравнение без утечек по времени.

        Важно: ``secrets.compare_digest`` для строк бросает TypeError на не-ASCII
        символах (например, если пароль на кириллице). Сравниваем байты — тогда
        работают пароли в любой кодировке.
        """
        try:
            return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
        except Exception:
            return False

    def _check_auth(header: Optional[str]) -> bool:
        if not auth_pass:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:], validate=False)
        except Exception:
            return False
        expect_user = auth_user or "admin"
        for decoded in _decode_credentials(raw):
            user, _, pwd = decoded.partition(":")
            if _secure_equal(user, expect_user) and _secure_equal(pwd, auth_pass):
                return True
        return False

    def _needs_auth(header: Optional[str]) -> bool:
        return bool(auth_pass) and not _check_auth(header)

    def _auth_page(reason: str = "auth") -> Response:
        """Читаемая страница вместо «белого экрана» при 401."""
        if reason == "wrong":
            title, hint = "Неверный логин или пароль", (
                "Браузер отправил логин и пароль, но они не совпадают с настройками бота. "
                "Проверьте их в файле <code>.env</code> рядом с ботом "
                "(переменные <code>PLANB_AUTH_USER</code> и <code>PLANB_AUTH_PASSWORD</code>).")
        else:
            title, hint = "Требуется авторизация", (
                "Введите логин и пароль панели (см. <code>PLANB_AUTH_USER</code> и "
                "<code>PLANB_AUTH_PASSWORD</code> в файле <code>.env</code>). "
                "Если окно с паролем не появилось — обновите страницу или откройте панель "
                "в приватном окне браузера.")
        html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PlanB — {title}</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;
   font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
 .card{{max-width:520px;padding:28px 32px;background:#131a23;border:1px solid #243146;border-radius:12px}}
 h1{{font-size:18px;margin:0 0 10px}} p{{margin:0 0 10px;color:#c6d2df}}
 code{{background:#0b1017;padding:2px 6px;border-radius:4px;font-size:13px;color:#ffd479}}
 .muted{{color:#8b98a9;font-size:13px}} a{{color:#4da3ff}}
</style></head><body><div class="card">
 <h1>{title}</h1><p>{hint}</p>
 <p class="muted">Проверка без браузера:<br>
   <code>curl -u ЛОГИН:ПАРОЛЬ http://127.0.0.1:ПОРТ/api/health</code></p>
 <p class="muted">Пароль можно сменить в файле <code>.env</code> и перезапустить сервис:
   <code>systemctl restart planb</code></p>
</div></body></html>"""
        return Response(status_code=401, media_type="text/html; charset=utf-8",
                        headers={"WWW-Authenticate": 'Basic realm="PlanB", charset="UTF-8"',
                                 "Cache-Control": "no-store, must-revalidate"},
                        content=html)

    app = FastAPI(title="PlanB — Fibonacci bot for Gate.io", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def auth_and_cache_middleware(request: Request, call_next):
        """Авторизация панели + запрет кеша на страницы и API.

        Кеш важен отдельно: из-за него браузер показывал старую (в том числе пустую)
        версию страницы даже после исправления на сервере. Теперь страница и API всегда
        берутся заново, а статика — с проверкой изменений на сервере.
        """
        path = request.url.path
        # открытыми остаются: /api/health (мониторинг, healthcheck Docker)
        # и /static (скрипты/стили панели) — иначе браузер не покажет даже страницу входа
        protected = path == "/" or (path.startswith("/api") and path != "/api/health")
        if auth_pass and protected and _needs_auth(request.headers.get("authorization")):
            reason = "wrong" if request.headers.get("authorization") else "auth"
            return _auth_page(reason)
        response = await call_next(request)
        if path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        else:
            response.headers.setdefault("Cache-Control", "no-store, must-revalidate")
            response.headers.setdefault("Pragma", "no-cache")
        return response

    # ------------------------------------------------------------------ static
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index():
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return JSONResponse({"error": "web panel not found", "api": "/api/state"})
        return FileResponse(str(index_file))

    # -------------------------------------------------------------------- api
    @app.get("/api/health")
    async def health(request: Request):
        """Проверка живости. Для мониторинга — JSON, для браузера — подсказка.

        Частая путаница: этот служебный адрес открывают в браузере вместо панели.
        В этом случае вместо сухого JSON отдаём страницу с кнопкой «Открыть панель».
        """
        accept = (request.headers.get("accept") or "").lower()
        if "text/html" in accept:
            status = runtime.status
            labels = {"running": "бот работает", "stopped": "бот остановлен",
                      "paused": "пауза", "error": "ошибка",
                      "target_reached": "цель по профиту достигнута",
                      "stopped_by_stop_loss": "остановлен стоп-лоссом"}
            hint = ""
            if status == "stopped":
                hint = ("<p>Бот ещё не запущен. Откройте панель и нажмите <b>«Старт»</b> — "
                        "по умолчанию включён демо-режим, ключи биржи не нужны.</p>")
            html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PlanB — служебный адрес</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;
   font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
 .card{{max-width:560px;padding:28px 32px;background:#131a23;border:1px solid #243146;border-radius:12px}}
 h1{{font-size:18px;margin:0 0 10px}} p{{margin:0 0 12px;color:#c6d2df}}
 a.button{{display:inline-block;margin:6px 0 14px;padding:11px 18px;border-radius:8px;
   background:#1d7a45;color:#eafff2;text-decoration:none;font-weight:600}}
 code{{background:#0b1017;padding:2px 6px;border-radius:4px;font-size:13px;color:#ffd479}}
 .muted{{color:#8b98a9;font-size:13px}}
</style></head><body><div class="card">
 <h1>Это служебный адрес <code>/api/health</code></h1>
 <p>Здесь для систем мониторинга отдаётся JSON: <code>{{"ok":true,"status":"{status}"}}</code>
    (сейчас: {labels.get(status, status)}).</p>
 <p><b>Панель управления ботом находится по адресу без пути:</b></p>
 <p><a class="button" href="../">Открыть панель PlanB →</a></p>
 {hint}
 <p class="muted">Если браузер спрашивает логин и пароль — они заданы в файле
   <code>.env</code> рядом с ботом (<code>PLANB_AUTH_USER</code>, <code>PLANB_AUTH_PASSWORD</code>)
   либо посмотрите их командой <code>bash deploy/set-auth.sh --show</code>.</p>
</div></body></html>"""
            return HTMLResponse(html)
        return {"ok": True, "version": __version__, "status": runtime.status}

    @app.get("/api/state")
    async def state():
        return runtime.snapshot()

    @app.get("/api/settings")
    async def get_settings():
        return runtime.settings.public_dict()

    @app.put("/api/settings")
    async def put_settings(payload: Dict[str, Any]):
        try:
            merged = {**runtime.settings.model_dump(), **(payload or {})}
            new_settings = BotSettings(**merged)
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"ok": False, "error": exc.errors(
                include_url=False, include_context=False)})
        new_settings = new_settings.merge_secrets(runtime.settings)
        result = await runtime.apply_settings(new_settings)
        return {"ok": True, **result, "settings": new_settings.public_dict()}

    @app.post("/api/settings/test-connection")
    async def test_connection(payload: Optional[Dict[str, Any]] = None):
        try:
            merged = {**runtime.settings.model_dump(), **(payload or {})}
            candidate = BotSettings(**merged).merge_secrets(runtime.settings)
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"ok": False, "error": exc.errors(
                include_url=False, include_context=False)})
        if candidate.synthetic_data:
            return {"ok": True, "synthetic": True, "real_data": False,
                    "message": "Выбран синтетический рынок (выдуманные свечи) — биржа не нужна. "
                               "Чтобы работать на реальных данных, выберите источник «Gate.io»."}
        public = await check_gate_connection(candidate)
        if not candidate.api_key or not candidate.api_secret:
            public["keys_present"] = False
            public["note"] = ("Реальные свечи идут без API-ключей. Ключи нужны только для "
                              "реальной торговли (режим live) и для показа баланса.")
            return public
        private = await test_credentials(candidate)
        private["keys_present"] = True
        private["public"] = public
        if private.get("ok") and public.get("ok"):
            private["message"] = (f"ключи рабочие, реальные данные идут: "
                                  f"{public.get('symbol')} {public.get('timeframe')}, "
                                  f"цена {public.get('price')}")
        elif not private.get("ok"):
            private["error"] = f"ключи не сработали: {private.get('error')}"
        return private

    @app.post("/api/bot/start")
    async def bot_start():
        return await runtime.start(runtime.settings)

    @app.post("/api/bot/stop")
    async def bot_stop():
        return await runtime.stop()

    @app.post("/api/bot/pause")
    async def bot_pause():
        return await runtime.pause()

    @app.post("/api/bot/resume")
    async def bot_resume():
        return await runtime.resume()

    @app.post("/api/bot/close-all")
    async def bot_close_all():
        return await runtime.close_all()

    @app.post("/api/bot/reset-stats")
    async def bot_reset():
        return await runtime.reset_stats()

    @app.post("/api/bot/reset-cycle")
    async def bot_reset_cycle():
        return await runtime.reset_cycle()

    # ---------------------------------------------------------------- market
    @app.get("/api/candles")
    async def candles(limit: int = Query(default=300, ge=20, le=1500)):
        return runtime.chart_payload(limit)

    @app.get("/api/price")
    async def price():
        return {"price": runtime.engine.last_price, "ts": runtime.last_loop_ts}

    @app.get("/api/markets")
    async def markets(query: str = "", limit: int = Query(default=50, ge=1, le=500)):
        if runtime.settings.synthetic_data:
            base = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "TON/USDT",
                    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT"]
            return {"ok": True, "demo": True, "markets": [m for m in base if query.upper() in m]}
        try:
            import ccxt.async_support as ccxt
            options = {"defaultType": "swap" if runtime.settings.market_type == "futures" else "spot"}
            ex = ccxt.gate({"enableRateLimit": True, "options": options})
            try:
                await ex.load_markets()
                quote = runtime.settings.symbol.partition("/")[2].split(":")[0] or "USDT"
                symbols = [s for s, m in ex.markets.items()
                           if m.get("quote") == quote and (m.get("swap") if runtime.settings.market_type == "futures"
                                                           else m.get("spot"))
                           and query.upper() in s.upper()]
                return {"ok": True, "markets": symbols[:limit]}
            finally:
                await ex.close()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "markets": []}

    # --------------------------------------------------------------- history
    @app.get("/api/cycles")
    async def cycles(limit: int = Query(default=100, ge=1, le=1000)):
        return {"ok": True, "cycles": db.list_cycles(limit)}

    @app.get("/api/positions")
    async def positions(limit: int = Query(default=200, ge=1, le=2000)):
        return {"ok": True, "positions": db.all_positions(limit)}

    @app.get("/api/logs")
    async def logs(limit: int = Query(default=200, ge=1, le=2000)):
        return {"ok": True, "logs": db.tail_logs(limit)}

    # -------------------------------------------------------------- backtest
    @app.post("/api/backtest")
    async def backtest(payload: Optional[Dict[str, Any]] = None):
        """Бэктест синхронно (небольшие периоды, удобно для curl и тестов)."""
        try:
            return await runtime.run_backtest(payload or {})
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"ok": False, "error": exc.errors(
                include_url=False, include_context=False)})
        except HistoryError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    @app.post("/api/backtest/job")
    async def backtest_job_start(payload: Optional[Dict[str, Any]] = None):
        """Запустить бэктест в фоне (загрузка истории с биржи может идти минутами).

        Панель опрашивает ``/api/backtest/job/{id}`` и показывает ход загрузки.
        """
        return runtime.start_backtest_job(payload or {})

    @app.get("/api/backtest/jobs")
    async def backtest_jobs():
        return {"ok": True, "jobs": runtime.list_backtest_jobs()}

    @app.get("/api/backtest/job/{job_id}")
    async def backtest_job_get(job_id: str, result: int = Query(default=1, ge=0, le=1)):
        job = runtime.backtest_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="задание не найдено")
        payload = runtime.public_job(job)
        payload["ok"] = job["state"] != "error"
        if result and job["state"] == "done":
            payload["result"] = job["result"]
        return payload

    @app.post("/api/backtest/job/{job_id}/cancel")
    async def backtest_job_cancel(job_id: str):
        if not runtime.cancel_backtest_job(job_id):
            return {"ok": False, "error": "задание не выполняется"}
        return {"ok": True}

    # ------------------------------------------------- свечи: файлы и загрузка
    @app.get("/api/backtest/files")
    async def backtest_files():
        """Список CSV со свечами (data/candles) — свои файлы для бэктеста."""
        from .history import history_dir
        return {"ok": True, "files": list_files(), "dir": str(history_dir())}

    @app.post("/api/backtest/files")
    async def backtest_file_upload(request: Request,
                                   name: str = Query(default="candles.csv", max_length=160)):
        """Загрузить свои свечи: CSV, ZIP или GZIP — как есть, без распаковки руками.

        Понимает выгрузки бирж (Binance, Gate.io, TradingView, Excel): разделитель
        «,» или «;», время в секундах/миллисекундах/микросекундах/датой, лишние
        колонки игнорируются. Файл нормализуется и сохраняется в data/candles как CSV.
        """
        import json as _json
        import re as _re
        raw = await request.body()
        if not raw:
            return JSONResponse(status_code=400, content={"ok": False, "error": "пустой файл"})
        # прежняя версия панели отправляла CSV в JSON-строке — принимаем и такой формат
        if raw[:1] == b'"' and raw[-1:] == b'"':
            try:
                raw = _json.loads(raw.decode("utf-8")).encode("utf-8")
            except Exception:
                pass
        try:
            candles = read_candles(raw)
        except HistoryError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        truncated = 0
        if len(candles) > MAX_CANDLES:
            truncated = len(candles) - MAX_CANDLES
            candles = candles[-MAX_CANDLES:]      # берём последние свечи — они нужнее
        clean = _re.sub(r"\.(zip|gz|csv|txt)$", "", name, flags=_re.I).strip() or "candles"
        path = save_file(clean + ".csv", write_csv(candles))
        info = {"name": path.name, "label": f"файл {path.name}"}
        info.update(describe_candles(candles))
        message = f"загружено свечей: {len(candles)}"
        if truncated:
            message += f" (лимит {MAX_CANDLES}, отброшено старых свечей: {truncated})"
        return {"ok": True, "file": file_info(path.name), "candles": len(candles),
                "data": info, "message": message, "truncated": truncated}

    @app.get("/api/backtest/files/{name}")
    async def backtest_file_download(name: str):
        from .history import file_path
        path = file_path(name)
        if not path.exists():
            raise HTTPException(status_code=404, detail="файл не найден")
        return FileResponse(str(path), media_type="text/csv",
                            filename=path.name)

    @app.delete("/api/backtest/files/{name}")
    async def backtest_file_delete(name: str):
        return {"ok": delete_file(name)}

    @app.get("/api/backtests")
    async def backtests():
        return {"ok": True, "backtests": db.list_backtests()}

    @app.get("/api/backtest/{bt_id}")
    async def backtest_get(bt_id: int):
        item = db.get_backtest(bt_id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True, **item}

    @app.delete("/api/backtest/{bt_id}")
    async def backtest_delete(bt_id: int):
        with db._lock:  # noqa: SLF001 - tiny helper
            db.conn.execute("DELETE FROM backtests WHERE id=?", (bt_id,))
            db.conn.commit()
        return {"ok": True}

    # -------------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        # браузеры отправляют Basic-заголовок и при WebSocket-рукопожатии;
        # если пароль не проходит — панель работает на опросе /api/state
        if auth_pass and not _check_auth(websocket.headers.get("authorization")):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = runtime.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", "snapshot": runtime.snapshot()})
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    message = {"type": "snapshot", "snapshot": runtime.snapshot()}
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            runtime.unsubscribe(queue)

    @app.on_event("startup")
    async def _startup():
        """Автозапуск бота — ТОЛЬКО в фоне, панель не должна ждать биржу.

        uvicorn сначала выполняет startup-обработчики и лишь потом открывает порт.
        Если автозапуск выполнялся здесь синхронно, а биржа отвечала медленно (или
        запрос к ней «подвисал»), панель вообще не открывалась: служба в systemd
        «активна», а порт никто не слушает. Поэтому запуск бота уходит в отдельную
        задачу, а веб-сервер поднимается сразу.
        """
        app.state.autostart_task = asyncio.create_task(_autostart())
        return None

    async def _autostart() -> None:
        try:
            delay = float(os.environ.get("PLANB_AUTOSTART_DELAY", "1.0") or 0)
        except ValueError:
            delay = 1.0
        if delay > 0:
            await asyncio.sleep(delay)          # отдаём порт браузеру как можно раньше
        if not autostart_enabled():
            runtime.log("info", "автозапуск отключён (PLANB_AUTOSTART=0)", persist=False)
            return
        try:
            if runtime.should_autostart():
                result = await runtime.start(runtime.settings)
                if not result.get("ok"):
                    runtime.log("error", f"автозапуск не удался: {result.get('error')}", persist=False)
            elif runtime.engine.cycle is not None:
                runtime.log("warn", "после запуска обнаружен незакрытый цикл, но бот был остановлен "
                                    "пользователем — нажмите «Продолжить», чтобы взять его в работу",
                            persist=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # автозапуск не должен ронять сервер
            runtime.log("error", f"автозапуск не удался: {exc}", persist=False)

    @app.on_event("shutdown")
    async def _shutdown():
        task = getattr(app.state, "autostart_task", None)
        if task is not None and not task.done():
            task.cancel()
        try:
            await runtime._teardown()  # noqa: SLF001
        except Exception:
            pass

    app.state.runtime = runtime
    app.state.db = db
    return app


app = create_app()
