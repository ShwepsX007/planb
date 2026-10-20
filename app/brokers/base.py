"""Broker abstraction: everything the bot needs to actually trade."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from ..models import Fill, Side


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    """Minimal trading interface used by the live bot."""

    name: str = "broker"

    def __init__(self, settings) -> None:
        self.settings = settings

    # lifecycle ---------------------------------------------------------------
    @abstractmethod
    async def start(self) -> None:
        """Connect, configure the account (leverage etc.)."""

    async def close(self) -> None:  # pragma: no cover - optional
        return None

    # trading -----------------------------------------------------------------
    @abstractmethod
    async def open_position(self, side: Side, volume_usd: float, ref_price: float,
                            ts: Optional[int] = None) -> Fill:
        """Open a position sized ``volume_usd`` (notional) at the market."""

    @abstractmethod
    async def close_position(self, side: Side, qty: float, ref_price: float,
                             ts: Optional[int] = None, reason: str = "") -> Fill:
        """Close ``qty`` of an open position (reduce only) at the market."""

    # info --------------------------------------------------------------------
    @abstractmethod
    async def last_price(self) -> float:
        ...

    @abstractmethod
    async def balance(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def exchange_position(self, side: Side) -> float:
        """Current position size in base currency (0 if flat)."""

    async def place_pending_order(self, *, purpose: str, side: Side, price: float,
                                  volume_usd: float = 0.0, qty: float = 0.0,
                                  client_id: str = "") -> Dict[str, Any]:
        """Place a resting add limit or a reduce-only take-profit order."""
        raise BrokerError("этот брокер не поддерживает отложенные заявки")

    async def order_status(self, order_id: str, *, side: Side) -> Dict[str, Any]:
        raise BrokerError("этот брокер не поддерживает проверку отложенных заявок")

    async def cancel_pending_order(self, order_id: str, *, side: Side) -> Dict[str, Any]:
        raise BrokerError("этот брокер не поддерживает отмену отложенных заявок")

    async def update_market(self, price: float, candle=None) -> None:
        """Paper brokers may use the full candle range to simulate limit fills."""
        return None

    def normalize_price(self, price: float) -> float:
        return float(price)

    async def extra_status(self) -> Dict[str, Any]:
        return {}

    # helpers -----------------------------------------------------------------
    def apply_slippage(self, price: float, *, adverse: bool) -> float:
        slip = self.settings.slippage_pct / 100.0
        return price * (1 + slip if adverse else 1 - slip)
