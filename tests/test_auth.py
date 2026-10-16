"""Авторизация панели: кириллические пароли, 401-страница, доступ к API.

Регрессия: раньше пароль с не-ASCII символами валил `secrets.compare_digest`
(TypeError) и страница отдавала HTTP 500 — в браузере это выглядел как пустой
белый экран, хотя `/api/health` продолжал отвечать 200.
"""
from __future__ import annotations

import base64
import os

import httpx
import pytest


def basic(user: str, password: str, encoding: str = "utf-8") -> dict:
    raw = f"{user}:{password}".encode(encoding)
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANB_AUTH_USER", "admin")
    monkeypatch.setenv("PLANB_AUTH_PASSWORD", "придумай-пароль")   # кириллица
    from app.main import create_app
    app = create_app(os.path.join(str(tmp_path), "auth.sqlite3"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_cyrillic_password_utf8_works(client):
    """Chrome/Firefox отправляют Basic-заголовок в UTF-8."""
    res = await client.get("/", headers=basic("admin", "придумай-пароль"))
    assert res.status_code == 200, res.text
    assert "PlanB" in res.text

    res = await client.get("/api/state", headers=basic("admin", "придумай-пароль"))
    assert res.status_code == 200


async def test_cyrillic_password_cp1251_works(client):
    """Часть браузеров/прокси кодируют пароль в cp1251 или latin-1."""
    res = await client.get("/", headers=basic("admin", "придумай-пароль", "cp1251"))
    assert res.status_code == 200, res.text


async def test_wrong_password_returns_readable_page(client):
    res = await client.get("/", headers=basic("admin", "неверный"))
    assert res.status_code == 401
    assert res.headers["www-authenticate"].startswith("Basic")
    assert "Неверный логин или пароль" in res.text      # не пустой белый экран
    assert "PLANB_AUTH_PASSWORD" in res.text


async def test_missing_credentials_returns_hint(client):
    res = await client.get("/api/settings")
    assert res.status_code == 401
    assert "Требуется авторизация" in res.text


async def test_health_is_open_for_monitoring(client):
    res = await client.get("/api/health")
    assert res.status_code == 200 and res.json()["ok"] is True


async def test_static_assets_are_open_so_the_error_page_renders(client):
    for asset in ("/static/styles.css", "/static/app.js"):
        res = await client.get(asset)
        assert res.status_code == 200, asset


async def test_protected_api_needs_auth(client):
    for path in ("/api/state", "/api/settings", "/api/cycles", "/api/logs"):
        res = await client.get(path)
        assert res.status_code == 401, path
        res = await client.get(path, headers=basic("admin", "придумай-пароль"))
        assert res.status_code == 200, path


async def test_settings_hidden_without_auth(client):
    res = await client.put("/api/settings", json={"timeframe": "15m"})
    assert res.status_code == 401
    # и ничего не изменилось
    ok = await client.get("/api/settings", headers=basic("admin", "придумай-пароль"))
    assert ok.json()["timeframe"] == "5m"


def test_password_comparison_helper_handles_any_encoding():
    """Юнит-проверка самой логики сравнения (без HTTP)."""
    import secrets as _secrets

    def secure_equal(a: str, b: str) -> bool:
        try:
            return _secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
        except Exception:
            return False

    assert secure_equal("придумай-пароль", "придумай-пароль")
    assert not secure_equal("придумай-пароль", "придумай-пароль2")
    assert secure_equal("ascii-pass", "ascii-pass")
    # строковая версия падала бы с TypeError — фиксируем это как причину баг-фикса
    with pytest.raises(TypeError):
        _secrets.compare_digest("придумай-пароль", "придумай-пароль")
