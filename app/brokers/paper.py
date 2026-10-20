"""Paper trading broker: real market data, simulated fills."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..models import Fill, Side, now_ms
from .base import Broker, BrokerError


class PaperBroker(Broker):
    """Simulates market orders with a configurable slippage and fee."""

    name = "paper"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.start_balance = float(settings.paper_balance_usd)
        self.cash = float(settings.paper_balance_usd)   # free quote currency
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.open_qty = 0.0
        self.open_volume_usd = 0.0
        self.open_side: Optional[Side] = None
        self.last_price_value = 0.0
        self.orders: list = []
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self.finished_orders: Dict[str, Dict[str, Any]] = {}
        self._next_pending_id = 0
        # OHLC from a forming candle includes price action that may have happened
        # before a resting order was submitted. Keep the latest observed range so
        # newly placed orders cannot be back-filled by an earlier wick.
        self._market_candle_ts: Optional[int] = None
        self._market_candle_high = 0.0
        self._market_candle_low = 0.0

    async def start(self) -> None:
        return None

    def restore_cycle(self, cycle) -> None:
        """Rebuild the simulated open exposure after a process restart."""
        if cycle is None:
            return
        self.open_qty = cycle.total_qty
        self.open_volume_usd = cycle.total_volume_usd
        self.open_side = cycle.side if self.open_qty > 0 else None

    async def set_price(self, price: float) -> None:
        if price > 0:
            self.last_price_value = price

    async def update_market(self, price: float, candle=None) -> None:
        """Fill resting paper orders on price action observed after submission.

        A forming candle's OHLC contains its entire range, including wicks from
        before a limit order existed. For an order submitted during that candle,
        only a new post-submission extreme (or the current traded price) can fill
        it; once the next candle starts, its full range is eligible.
        """
        if price <= 0:
            return
        self.last_price_value = price
        if candle is None and self._market_candle_ts is not None:
            # Price-only post-submission checks must not erase the active candle's
            # watermark; the next poll may still report that same candle's OHLC.
            candle_ts = self._market_candle_ts
            high = max(price, self._market_candle_high)
            low = min(price, self._market_candle_low)
        else:
            candle_ts = int(candle.ts) if candle is not None else None
            high = max(price, float(getattr(candle, "high", price)))
            low = min(price, float(getattr(candle, "low", price)))
        # If both target and averaging levels are crossed in one OHLC candle, use
        # the conservative event ordering for the cycle: take profit first.
        pending = sorted(list(self.pending_orders.values()),
                         key=lambda item: 0 if item["purpose"] == "tp" else 1)
        for order in pending:
            same_candle = (candle_ts is not None
                           and order.get("_observed_candle_ts") == candle_ts)
            observed_high = float(order.get("_observed_high", high))
            observed_low = float(order.get("_observed_low", low))
            level = float(order["price"])
            upward = ((order["purpose"] == "tp" and order["side"] == "long")
                      or (order["purpose"] != "tp" and order["side"] == "short"))
            if upward:
                range_touch = high >= level and (not same_candle or high > observed_high)
                touched = price >= level or range_touch
            else:
                range_touch = low <= level and (not same_candle or low < observed_low)
                touched = price <= level or range_touch

            if not touched:
                # Advance the order's watermark so a later wick in this candle
                # is distinguishable from a pre-submission wick.
                order["_observed_candle_ts"] = candle_ts
                order["_observed_high"] = high
                order["_observed_low"] = low
                continue
            if order["purpose"] == "tp":
                fill_price = (max(level, price) if order["side"] == "long"
                              else min(level, price))
                fill = await self._fill_limit_close(order, fill_price)
                self._finish_pending(order, fill)
                # Any add resting for this cycle must not survive its take-profit.
                for other in list(self.pending_orders.values()):
                    if other.get("cycle_id") == order.get("cycle_id"):
                        self.pending_orders.pop(other["id"], None)
                        self.finished_orders[other["id"]] = {**other, "status": "canceled",
                                                              "filled_qty": 0.0, "average": 0.0,
                                                              "filled_cost": 0.0, "fee": 0.0}
                break
            fill_price = min(level, price) if order["side"] == "long" else max(level, price)
            fill = self._fill_limit_add(order, fill_price)
            self._finish_pending(order, fill)

        self._market_candle_ts = candle_ts
        self._market_candle_high = high
        self._market_candle_low = low

    def _fill_limit_add(self, order: Dict[str, Any], price: float) -> Fill:
        qty = float(order["qty"])
        fee = qty * price * self.settings.fee_pct / 100.0
        self.open_qty += qty
        self.open_volume_usd += qty * price
        self.open_side = order["side"]
        self.cash -= fee
        self.fees_paid += fee
        return Fill(price=price, qty=qty, fee=fee, ts=now_ms(), order_id=order["id"],
                    info={"side": order["side"], "purpose": "add"})

    async def _fill_limit_close(self, order: Dict[str, Any], price: float) -> Fill:
        qty = min(float(order["qty"]), self.open_qty)
        fee = qty * price * self.settings.fee_pct / 100.0
        basis = self.open_volume_usd * (qty / self.open_qty) if self.open_qty else 0.0
        proceeds = price * qty
        self.realized_pnl += (proceeds - basis) if order["side"] == "long" else (basis - proceeds)
        self.cash -= fee
        self.fees_paid += fee
        self.open_volume_usd = max(0.0, self.open_volume_usd - basis)
        self.open_qty = max(0.0, self.open_qty - qty)
        if self.open_qty <= 1e-12:
            self.open_qty = 0.0
            self.open_volume_usd = 0.0
            self.open_side = None
        return Fill(price=price, qty=qty, fee=fee, ts=now_ms(), order_id=order["id"],
                    info={"side": order["side"], "purpose": "tp"})

    def _finish_pending(self, order: Dict[str, Any], fill: Fill) -> None:
        self.pending_orders.pop(order["id"], None)
        self.finished_orders[order["id"]] = {
            **order, "status": "closed", "filled_qty": fill.qty,
            "average": fill.price, "filled_cost": fill.qty * fill.price, "fee": fill.fee,
            "timestamp": fill.ts, "fill": fill,
        }

    async def place_pending_order(self, *, purpose: str, side: Side, price: float,
                                  volume_usd: float = 0.0, qty: float = 0.0,
                                  client_id: str = "") -> Dict[str, Any]:
        if price <= 0:
            raise BrokerError("цена отложенного ордера должна быть больше нуля")
        self._next_pending_id += 1
        order_id = f"paper-pending-{self._next_pending_id}"
        amount = qty if qty > 0 else volume_usd / price
        observed_price = self.last_price_value or price
        order = {"id": order_id, "purpose": purpose, "side": side, "price": price,
                 "qty": amount, "volume_usd": amount * price, "clientOrderId": client_id,
                 "cycle_id": client_id.split("-")[0] if client_id else "",
                 "status": "open", "filled_qty": 0.0, "average": 0.0,
                 "filled_cost": 0.0, "fee": 0.0,
                 "_observed_candle_ts": self._market_candle_ts,
                 "_observed_high": self._market_candle_high or observed_price,
                 "_observed_low": self._market_candle_low or observed_price}
        self.pending_orders[order_id] = order
        return {"id": order_id, "status": "open", "price": price, "qty": amount,
                "base_qty": amount, "volume_usd": order["volume_usd"],
                "clientOrderId": client_id}

    async def order_status(self, order_id: str, *, side: Side) -> Dict[str, Any]:
        order = self.pending_orders.get(order_id) or self.finished_orders.get(order_id)
        if order is None:
            raise BrokerError(f"ордер {order_id} не найден в paper-брокере")
        return dict(order)

    async def cancel_pending_order(self, order_id: str, *, side: Side) -> Dict[str, Any]:
        order = self.pending_orders.pop(order_id, None)
        if order is None:
            return await self.order_status(order_id, side=side)
        result = {**order, "status": "canceled"}
        self.finished_orders[order_id] = result
        return dict(result)

    def normalize_price(self, price: float) -> float:
        return round(float(price), 8)

    async def open_position(self, side: Side, volume_usd: float, ref_price: float,
                            ts: Optional[int] = None) -> Fill:
        if ref_price <= 0:
            raise BrokerError("нет цены для открытия позиции")
        price = self.apply_slippage(ref_price, adverse=True)
        qty = volume_usd / price
        fee = qty * price * self.settings.fee_pct / 100.0
        self.open_qty += qty
        self.open_volume_usd += qty * price
        self.open_side = side
        self.cash -= fee
        self.fees_paid += fee
        fill = Fill(price=price, qty=qty, fee=fee, ts=ts or now_ms(),
                    order_id=f"paper-{len(self.orders)}", info={"side": side})
        self.orders.append(fill)
        return fill

    async def close_position(self, side: Side, qty: float, ref_price: float,
                            ts: Optional[int] = None, reason: str = "") -> Fill:
        if ref_price <= 0:
            raise BrokerError("нет цены для закрытия позиции")
        qty = min(qty, self.open_qty) if self.open_qty else qty
        price = self.apply_slippage(ref_price, adverse=False)
        fee = qty * price * self.settings.fee_pct / 100.0
        # book the closed part: cost basis -> proceeds
        basis = self.open_volume_usd * (qty / self.open_qty) if self.open_qty else 0.0
        proceeds = price * qty
        self.realized_pnl += (proceeds - basis) if side == "long" else (basis - proceeds)
        self.cash -= fee
        self.fees_paid += fee
        self.open_volume_usd = max(0.0, self.open_volume_usd - basis)
        self.open_qty = max(0.0, self.open_qty - qty)
        if self.open_qty <= 1e-12:
            self.open_qty = 0.0
            self.open_volume_usd = 0.0
            self.open_side = None
        fill = Fill(price=price, qty=qty, fee=fee, ts=ts or now_ms(),
                    order_id=f"paper-{len(self.orders)}", info={"side": side, "reason": reason})
        self.orders.append(fill)
        return fill

    async def last_price(self) -> float:
        return self.last_price_value

    async def balance(self) -> Dict[str, Any]:
        # cash уже учитывает комиссии, realized_pnl — стоимость закрытых позиций
        equity = self.cash + self.realized_pnl
        return {
            "mode": "paper",
            "start_balance": self.start_balance,
            "free": round(equity, 4),
            "total": round(equity, 4),
            "used": round(self.open_volume_usd, 4),
            "realized_pnl": round(self.realized_pnl - self.fees_paid, 6),
            "fees": round(self.fees_paid, 6),
        }

    async def exchange_position(self, side: Side) -> float:
        return self.open_qty if self.open_side == side else 0.0
