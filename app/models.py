"""Domain models shared by the strategy engine, brokers, backtester and API."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Optional

Side = Literal["long", "short"]


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Candle:
    """A closed OHLCV candle. ``ts`` is the candle open time in ms."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_ccxt(cls, row: list) -> "Candle":
        return cls(ts=int(row[0]), open=float(row[1]), high=float(row[2]),
                   low=float(row[3]), close=float(row[4]), volume=float(row[5] or 0.0))

    def to_list(self) -> list:
        return [self.ts, self.open, self.high, self.low, self.close, self.volume]

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    """A single fill inside a cycle."""

    idx: int                      # 1-based position number inside the cycle
    ts: int
    price: float                  # fill price
    qty: float                    # base-currency amount
    volume_usd: float             # notional in quote currency (USDT)
    fee: float = 0.0              # entry fee paid (quote currency)
    multiplier: float = 1.0       # volume multiplier used for this position
    close_ts: Optional[int] = None
    close_price: Optional[float] = None
    close_fee: float = 0.0
    pnl: float = 0.0              # net pnl of this position (fees included)

    @property
    def is_open(self) -> bool:
        return self.close_ts is None

    def dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_open"] = self.is_open
        return d


@dataclass
class Cycle:
    """One trading cycle: a signal, its fibonacci grid and all averaged positions."""

    id: str
    side: Side
    signal_ts: int
    signal_price: float           # close of the impulse's last candle
    opened_ts: int
    swing_high: float             # максимум движения
    swing_low: float              # минимум движения
    goal_pct: float               # fib correction target (e.g. 50)
    target: float                 # price of the target level
    #: направление самого движения: "down" (0% внизу, лонг) или "up" (0% вверху, шорт).
    #: от него зависит, с какой стороны отсчитываются уровни сетки.
    move_side: str = "down"
    positions: List[Position] = field(default_factory=list)
    status: str = "open"          # open | closed
    closed_ts: Optional[int] = None
    close_price: Optional[float] = None
    close_reason: str = ""
    pnl: float = 0.0              # net realized pnl of the cycle
    pnl_pct: float = 0.0          # net pnl / invested volume
    fees: float = 0.0
    max_positions_used: int = 1
    extreme_at_last_entry: float = 0.0
    note: str = ""

    # ------------------------------------------------------------------ math
    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.is_open]

    @property
    def total_qty(self) -> float:
        return sum(p.qty for p in self.open_positions)

    @property
    def total_volume_usd(self) -> float:
        return sum(p.qty * p.price for p in self.open_positions)

    @property
    def invested_volume_usd(self) -> float:
        return sum(p.qty * p.price for p in self.positions)

    @property
    def avg_entry(self) -> float:
        qty = self.total_qty
        if qty <= 0:
            return 0.0
        return sum(p.price * p.qty for p in self.open_positions) / qty

    @property
    def last_entry_price(self) -> float:
        return self.positions[-1].price if self.positions else 0.0

    def profit_pct_to(self, price: float) -> float:
        """Profit (in %) between the volume weighted average entry and ``price``."""
        avg = self.avg_entry
        if avg <= 0:
            return 0.0
        if self.side == "long":
            return (price / avg - 1.0) * 100.0
        return (1.0 - price / avg) * 100.0

    def unrealized_pnl(self, price: float) -> float:
        if price <= 0:
            return 0.0
        pnl = 0.0
        for p in self.open_positions:
            if self.side == "long":
                gross = (price - p.price) * p.qty
            else:
                gross = (p.price - price) * p.qty
            pnl += gross - p.fee
        return pnl

    def expected_pnl_at_target(self, fee_pct: float = 0.0) -> float:
        """Estimated net profit if the whole cycle is closed at ``self.target``."""
        if self.total_qty <= 0:
            return 0.0
        pnl = 0.0
        for p in self.open_positions:
            if self.side == "long":
                gross = (self.target - p.price) * p.qty
            else:
                gross = (p.price - self.target) * p.qty
            pnl += gross - p.fee - self.target * p.qty * fee_pct / 100.0
        return pnl

    def dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.update({
            "avg_entry": self.avg_entry,
            "total_qty": self.total_qty,
            "total_volume_usd": self.total_volume_usd,
            "invested_volume_usd": self.invested_volume_usd,
            "open_positions": len(self.open_positions),
            "positions": [p.dict() for p in self.positions],
            "profit_pct_to_target": self.profit_pct_to(self.target),
        })
        return d


@dataclass
class Action:
    """Something the strategy asks the executor to do."""

    kind: str                     # entry | add | close_all
    side: Side
    ref_price: float              # ideal price (candle close / target level)
    volume_usd: float = 0.0       # for entry/add
    qty: float = 0.0              # for close_all (0 -> close everything)
    position_idx: int = 0
    target: float = 0.0
    reason: str = ""
    ts: int = 0

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    """Result of an order."""

    price: float
    qty: float
    fee: float = 0.0
    ts: int = 0
    order_id: str = ""
    info: Dict[str, Any] = field(default_factory=dict)
