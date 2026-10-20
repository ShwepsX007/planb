"""Live broker for Gate.io (spot + USDT-margined perpetual futures) via ccxt."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import ccxt.async_support as ccxt  # type: ignore

from ..models import Fill, Side, now_ms
from .base import Broker, BrokerError


class GateBroker(Broker):
    name = "gate"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.ex: Optional[ccxt.Exchange] = None
        self.market = None
        self.unified = settings.symbol
        self._leverage_set = False

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        if not self.settings.api_key or not self.settings.api_secret:
            raise BrokerError("не заданы API-ключи биржи")
        params: Dict[str, Any] = {
            "apiKey": self.settings.api_key,
            "secret": self.settings.api_secret,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": "swap" if self.settings.market_type == "futures" else "spot"},
        }
        self.ex = ccxt.gate(params)
        if self.settings.sandbox and self.ex.has.get("sandbox", True):
            try:
                self.ex.set_sandbox_mode(True)
            except Exception:  # pragma: no cover - depends on exchange
                pass
        await self.ex.load_markets()
        self.unified = self._unified_symbol(self.settings.symbol)
        self.market = self.ex.market(self.unified)
        if self.settings.market_type == "futures":
            await self._configure_futures()

    def _unified_symbol(self, symbol: str) -> str:
        if self.settings.market_type == "futures" and ":" not in symbol:
            base, _, quote = symbol.partition("/")
            return f"{base}/{quote}:{quote}"
        return symbol

    async def _configure_futures(self) -> None:
        assert self.ex is not None
        try:
            await self.ex.set_margin_mode(self.settings.margin_mode, self.unified)
        except Exception as exc:
            message = str(exc).lower()
            if not any(word in message for word in ("already", "same", "unchanged", "not modified", "not changed")):
                raise BrokerError(f"не удалось проверить режим маржи {self.settings.margin_mode}: {exc}") from exc
        try:
            await self.ex.set_leverage(self.settings.leverage, self.unified)
            self._leverage_set = True
        except Exception as exc:
            raise BrokerError(f"не удалось установить плечо {self.settings.leverage}x: {exc}") from exc

    async def close(self) -> None:
        if self.ex is not None:
            await self.ex.close()

    # --------------------------------------------------------------- trading
    def _contract_size(self) -> float:
        if self.settings.market_type != "futures" or not self.market:
            return 1.0
        return float(self.market.get("contractSize") or 1.0)

    def _amount(self, qty: float, price: float) -> float:
        """Convert base units to Gate's amount units (contracts on futures)."""
        assert self.ex is not None
        amount = qty / self._contract_size()
        amount = float(self.ex.amount_to_precision(self.unified, amount))
        limits = (self.market.get("limits", {}) or {}) if self.market else {}
        min_amount = ((limits.get("amount") or {}).get("min")) or 0
        min_cost = ((limits.get("cost") or {}).get("min")) or 0
        if amount <= 0:
            if self.settings.auto_min_volume and min_amount:
                amount = float(self.ex.amount_to_precision(self.unified, float(min_amount)))
            else:
                raise BrokerError("объём меньше минимального шага биржи")
        if self.settings.auto_min_volume:
            base_amount = amount * self._contract_size()
            if min_amount and amount < min_amount:
                amount = float(self.ex.amount_to_precision(self.unified, float(min_amount)))
                base_amount = amount * self._contract_size()
            if min_cost and base_amount * price < min_cost:
                amount = float(self.ex.amount_to_precision(
                    self.unified, float(min_cost) / price / self._contract_size()))
        return amount

    async def open_position(self, side: Side, volume_usd: float, ref_price: float,
                            ts: Optional[int] = None) -> Fill:
        assert self.ex is not None, "broker is not started"
        if self.settings.market_type == "spot" and side == "short":
            raise BrokerError("на споте шорт недоступен")
        order_side = "buy" if side == "long" else "sell"
        qty = self._amount(volume_usd / ref_price, ref_price)
        if qty <= 0:
            raise BrokerError("объём позиции получился нулевым")
        params: Dict[str, Any] = {}
        if self.settings.market_type == "futures":
            params["reduceOnly"] = False
        else:
            # спот: рыночный buy у Gate задаётся суммой в котируемой валюте
            params["cost"] = round(float(volume_usd), 8)
        try:
            order = await self.ex.create_order(self.unified, "market", order_side, qty, None, params)
        except Exception as exc:  # pragma: no cover - network
            raise BrokerError(f"ошибка открытия позиции: {exc}") from exc
        fill = self._fill_from_order(order, side, ref_price)
        return fill

    async def close_position(self, side: Side, qty: float, ref_price: float,
                             ts: Optional[int] = None, reason: str = "") -> Fill:
        assert self.ex is not None, "broker is not started"
        order_side = "sell" if side == "long" else "buy"
        amount = self._amount(qty, ref_price)
        if amount <= 0:
            raise BrokerError("нечего закрывать")
        params: Dict[str, Any] = {}
        if self.settings.market_type == "futures":
            params["reduceOnly"] = True
        try:
            order = await self.ex.create_order(self.unified, "market", order_side, amount, None, params)
        except Exception as exc:  # pragma: no cover - network
            raise BrokerError(f"ошибка закрытия позиции: {exc}") from exc
        return self._fill_from_order(order, side, ref_price)

    async def place_pending_order(self, *, purpose: str, side: Side, price: float,
                                  volume_usd: float = 0.0, qty: float = 0.0,
                                  client_id: str = "") -> Dict[str, Any]:
        """Place a resting add limit or a reduce-only TP limit on Gate.io."""
        assert self.ex is not None, "broker is not started"
        if price <= 0:
            raise BrokerError("цена отложенного ордера должна быть больше нуля")
        base_qty = qty if qty > 0 else volume_usd / price
        amount = self._amount(base_qty, price)
        order_side = ("sell" if side == "long" else "buy") if purpose == "tp" else (
            "buy" if side == "long" else "sell")
        params: Dict[str, Any] = {}
        if self.settings.market_type == "futures" and purpose == "tp":
            params["reduceOnly"] = True
        if client_id:
            # Gate text/clientOrderId максимум 28 символов; CCXT добавит префикс t-.
            params["clientOrderId"] = client_id[:26]
        try:
            order = await self.ex.create_order(self.unified, "limit", order_side,
                                               amount, price, params)
        except Exception as exc:
            raise BrokerError(f"ошибка установки отложенного ордера {purpose}: {exc}") from exc
        order_id = str(order.get("id") or "")
        if not order_id:
            raise BrokerError("биржа не вернула ID отложенного ордера")
        base_qty_actual = amount * self._contract_size()
        actual_price = float(order.get("price") or price)
        return {"id": order_id, "status": str(order.get("status") or "open"),
                "price": actual_price, "amount": amount, "base_qty": base_qty_actual,
                "volume_usd": base_qty_actual * actual_price,
                "clientOrderId": order.get("clientOrderId") or client_id,
                "raw": order}

    def _normalize_managed_order(self, order: Dict[str, Any], side: Side) -> Dict[str, Any]:
        amount = float(order.get("amount") or 0.0)
        filled = order.get("filled")
        if filled is None:
            remaining = order.get("remaining")
            filled = max(0.0, amount - float(remaining or 0.0))
        contract_size = self._contract_size()
        filled_base = float(filled or 0.0) * contract_size
        price = float(order.get("average") or order.get("price") or 0.0)
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or 0.0)
        if not fee:
            fee = sum(float(x.get("cost") or 0.0) for x in (order.get("fees") or []))
        if not fee and price and filled_base:
            fee = price * filled_base * self.settings.fee_pct / 100.0
        return {"id": str(order.get("id") or ""), "status": str(order.get("status") or "open"),
                "filled_qty": filled_base, "average": price,
                "filled_cost": filled_base * price, "fee": fee,
                "timestamp": int(order.get("lastTradeTimestamp") or order.get("timestamp") or now_ms()),
                "raw": order}

    async def order_status(self, order_id: str, *, side: Side) -> Dict[str, Any]:
        assert self.ex is not None
        try:
            order = await self.ex.fetch_order(order_id, self.unified)
        except Exception as exc:
            raise BrokerError(f"не удалось проверить ордер {order_id}: {exc}") from exc
        return self._normalize_managed_order(order, side)

    async def cancel_pending_order(self, order_id: str, *, side: Side) -> Dict[str, Any]:
        assert self.ex is not None
        try:
            order = await self.ex.cancel_order(order_id, self.unified)
        except Exception as exc:
            # В гонке биржа могла уже исполнить заявку; в таком случае читаем её итог.
            try:
                return await self.order_status(order_id, side=side)
            except Exception:
                raise BrokerError(f"не удалось отменить ордер {order_id}: {exc}") from exc
        if not order.get("filled") and order.get("id"):
            try:
                order = await self.ex.fetch_order(order_id, self.unified)
            except Exception:
                pass
        return self._normalize_managed_order(order, side)

    def normalize_price(self, price: float) -> float:
        assert self.ex is not None
        return float(self.ex.price_to_precision(self.unified, price))

    def _fill_from_order(self, order: dict, side: Side, ref_price: float) -> Fill:
        price = float(order.get("average") or order.get("price") or ref_price)
        filled_value = order.get("filled")
        if filled_value is None:
            amount = float(order.get("amount") or 0.0)
            remaining = order.get("remaining")
            filled_value = max(0.0, amount - float(remaining or 0.0)) if remaining is not None else amount
        filled = float(filled_value or 0.0)
        qty = filled * self._contract_size()
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or 0.0)
        if not fee and price and qty:
            fee = price * qty * self.settings.fee_pct / 100.0
        return Fill(price=price, qty=qty, fee=fee,
                    ts=int(order.get("timestamp") or now_ms()),
                    order_id=str(order.get("id", "")), info=order)

    # ------------------------------------------------------------------ info
    async def last_price(self) -> float:
        assert self.ex is not None
        ticker = await self.ex.fetch_ticker(self.unified)
        return float(ticker.get("last") or ticker.get("close") or 0.0)

    async def balance(self) -> Dict[str, Any]:
        assert self.ex is not None
        bal = await self.ex.fetch_balance()
        quote = (self.market or {}).get("quote", "USDT") if self.market else "USDT"
        if self.settings.market_type == "futures":
            total = bal.get("total") or {}
            free = bal.get("free") or {}
            return {
                "mode": "live",
                "quote": quote,
                "total": float(total.get(quote) or 0.0),
                "free": float(free.get(quote) or 0.0),
                "used": float((bal.get("used") or {}).get(quote) or 0.0),
                "info": {"type": "futures"},
            }
        total = bal.get("total") or {}
        free = bal.get("free") or {}
        return {
            "mode": "live",
            "quote": quote,
            "total": float(total.get(quote) or 0.0),
            "free": float(free.get(quote) or 0.0),
            "used": float((bal.get("used") or {}).get(quote) or 0.0),
            "info": {"type": "spot"},
        }

    async def exchange_position(self, side: Side) -> float:
        assert self.ex is not None
        if self.settings.market_type == "spot":
            if side == "short":
                return 0.0
            balance = await self.ex.fetch_balance()
            base = (self.market or {}).get("base") or self.settings.symbol.split("/")[0]
            return float((balance.get("total") or {}).get(base) or 0.0)
        try:
            positions = await self.ex.fetch_positions([self.unified])
        except Exception:
            return 0.0
        for p in positions or []:
            contracts = float(p.get("contracts") or 0.0)
            p_side = str(p.get("side") or "")
            if contracts and p_side == side:
                return contracts * float(p.get("contractSize") or self._contract_size())
        return 0.0

    async def extra_status(self) -> Dict[str, Any]:
        return {"symbol": self.unified, "leverage": self.settings.leverage,
                "market_type": self.settings.market_type}


async def test_credentials(settings) -> Dict[str, Any]:
    """Used by the "проверить API" button in the UI."""
    broker = GateBroker(settings)
    try:
        await broker.start()
        bal = await broker.balance()
        price = await broker.last_price()
        return {"ok": True, "balance": bal, "price": price, "symbol": broker.unified}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            await broker.close()
        except Exception:
            pass
