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

    async def extra_status(self) -> Dict[str, Any]:
        return {}

    # helpers -----------------------------------------------------------------
    def apply_slippage(self, price: float, *, adverse: bool) -> float:
        slip = self.settings.slippage_pct / 100.0
        return price * (1 + slip if adverse else 1 - slip)
