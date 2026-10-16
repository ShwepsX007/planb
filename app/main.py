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

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

#: preset applied on the very first start (handy for local demo)
PRESETS: Dict[str, Dict[str, Any]] = {
    "demo": {
        "data_source": "demo",
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
    }
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

    runtime = BotRuntime(db, settings)

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
                        headers={"WWW-Authenticate": 'Basic realm="PlanB", charset="UTF-8"'},
                        content=html)

    app = FastAPI(title="PlanB — Fibonacci bot for Gate.io", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        # открытыми остаются: /api/health (мониторинг, healthcheck Docker)
        # и /static (скрипты/стили панели) — иначе браузер не покажет даже страницу входа
        protected = path == "/" or (path.startswith("/api") and path != "/api/health")
        if auth_pass and protected and _needs_auth(request.headers.get("authorization")):
            reason = "wrong" if request.headers.get("authorization") else "auth"
            return _auth_page(reason)
        return await call_next(request)

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
        if candidate.data_source == "demo":
            return {"ok": True, "demo": True,
                    "message": "Демо-данные: подключение к бирже не требуется"}
        if not candidate.api_key or not candidate.api_secret:
            return {"ok": False, "error": "не заданы API-ключи"}
        return await test_credentials(candidate)

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
        if runtime.settings.data_source == "demo":
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
        try:
            return await runtime.run_backtest(payload or {})
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"ok": False, "error": exc.errors(
                include_url=False, include_context=False)})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

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
        """Автозапуск бота при старте процесса (systemd/Docker)."""
        if not autostart_enabled():
            runtime.log("info", "автозапуск отключён (PLANB_AUTOSTART=0)", persist=False)
            return
        if runtime.should_autostart():
            result = await runtime.start(runtime.settings)
            if not result.get("ok"):
                runtime.log("error", f"автозапуск не удался: {result.get('error')}", persist=False)
        elif runtime.engine.cycle is not None:
            runtime.log("warn", "после запуска обнаружен незакрытый цикл, но бот был остановлен "
                                "пользователем — нажмите «Продолжить», чтобы взять его в работу",
                        persist=False)

    @app.on_event("shutdown")
    async def _shutdown():
        try:
            await runtime._teardown()  # noqa: SLF001
        except Exception:
            pass

    app.state.runtime = runtime
    app.state.db = db
    return app


app = create_app()
