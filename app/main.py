"""FastAPI application: REST API + WebSocket + web panel."""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
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

    def _check_auth(header: Optional[str]) -> bool:
        if not auth_pass:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        user, _, pwd = raw.partition(":")
        expect_user = auth_user or "admin"
        return secrets.compare_digest(user, expect_user) and secrets.compare_digest(pwd, auth_pass)

    app = FastAPI(title="PlanB — Fibonacci bot for Gate.io", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        # /api/health остаётся открытым для мониторинга и healthcheck Docker
        protected = path == "/" or (path.startswith("/api") and path != "/api/health")
        if auth_pass and protected:
            if not _check_auth(request.headers.get("authorization")):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="PlanB"'},
                                content="Требуется авторизация")
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
    async def health():
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
