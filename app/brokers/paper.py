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

    async def start(self) -> None:
        return None

    async def set_price(self, price: float) -> None:
        if price > 0:
            self.last_price_value = price

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
