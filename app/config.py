"""Bot settings (persisted as JSON in SQLite, edited from the web panel)."""
from __future__ import annotations

from typing import Any, Dict, Literal, Sequence, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

TIMEFRAMES: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}


class BotSettings(BaseModel):
    """All tunable parameters of the bot."""

    model_config = {"extra": "ignore"}

    # ------------------------------------------------------------------ exchange
    exchange_id: str = "gate"
    market_type: Literal["spot", "futures"] = "futures"
    symbol: str = "BTC/USDT"
    api_key: str = ""
    api_secret: str = ""
    sandbox: bool = False
    leverage: int = Field(default=5, ge=1, le=100)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    data_source: Literal["gate", "synthetic"] = "gate"
    demo_speed: float = Field(default=1.0, ge=1, le=600,
                              description="ускорение синтетического рынка (1 = реальное время)")

    # --------------------------------------------------------------- market data
    timeframe: str = "5m"

    # ------------------------------------------------------------------ strategy
    entry_mode: Literal["swing", "impulse"] = "swing"
    candles_count: int = Field(default=3, ge=1, le=50,
                               description="число свечей только для legacy impulse-метода")
    swing_lookback: int = Field(default=120, ge=10, le=500,
                                description="сколько свечей просматривать при поиске движения")
    swing_fresh_candles: int = Field(default=24, ge=2, le=200,
                                     description="насколько свежим должен быть экстремум движения")
    min_correction_pct: float = Field(default=5.0, ge=0, le=95,
                                      description="минимальная коррекция для входа, % сетки")
    min_move_pct: float = Field(default=1.2, gt=0, le=1000, description="минимальный % движения")
    fib_goal_pct: float = Field(default=50.0, gt=0, le=618, description="цель, уровень коррекции по фибо, %")
    min_profit_pct: float = Field(default=0.5, gt=0, le=100, description="минимальный % профита")
    direction: Literal["both", "long", "short"] = "both"
    move_measure: Literal["range", "close"] = "range"
    candle_direction_mode: Literal["body", "close"] = "body"
    update_swing_in_cycle: bool = False   # тянуть сетку также по новым максимумам

    # ------------------------------------------------------------------- volumes
    base_volume_usd: float = Field(default=10.0, gt=0, description="объём первой сделки, $")
    volume_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    multiplier_start_position: int = Field(default=3, ge=1, le=20,
                                           description="с какой позиции удваивать объём")
    max_positions: int = Field(default=6, ge=1, le=20)

    # ---------------------------------------------------------------- risk rules
    require_new_extreme_for_add: bool = True   # новую позицию только после нового экстремума
    tp_requires_min_profit: bool = True        # фиксировать цель только если профит >= минимума
    close_at_target_when_maxed: bool = True    # при достижении лимита позиций закрывать по цели
    cycle_stop_loss_pct: float = Field(default=0.0, ge=0, le=100,
                                       description="аварийный стоп цикла, % (0 = выключен)")

    # ------------------------------------------------------------ profit targets
    total_profit_target_usd: float = Field(default=50.0, ge=0,
                                           description="общий профит, $ (0 = без ограничения)")
    stop_after_total_target: bool = True

    # ----------------------------------------------------------------- execution
    trade_mode: Literal["paper", "live"] = "paper"
    paper_balance_usd: float = Field(default=1000.0, gt=0)
    fee_pct: float = Field(default=0.05, ge=0, le=2, description="комиссия за сделку, %")
    slippage_pct: float = Field(default=0.02, ge=0, le=2)
    auto_min_volume: bool = True
    poll_interval_sec: float = Field(default=3.0, ge=1, le=60)
    candle_lookback: int = Field(default=300, ge=60, le=1500)
    notify_telegram_token: str = ""
    notify_telegram_chat: str = ""

    # ------------------------------------------------------------------ helpers
    @field_validator("data_source", mode="before")
    @classmethod
    def _norm_data_source(cls, v: Any) -> Any:
        """Приводим названия источника данных к двум понятным вариантам.

        * ``gate``      — реальные свечи с биржи Gate.io (нужен только интернет,
          API-ключи не требуются). Торговать можно виртуально (paper) или реально (live).
        * ``synthetic`` — выдуманные свечи, чтобы посмотреть панель без интернета.

        Старое значение ``demo`` означало именно синтетику, поэтому оно сохраняется
        как ``synthetic`` (понятные синонимы принимаются, чтобы ничего не сломать).
        """
        if not isinstance(v, str):
            return v
        aliases = {
            "demo": "synthetic", "demos": "synthetic", "synthetic": "synthetic",
            "synth": "synthetic", "fake": "synthetic", "offline": "synthetic",
            "gate": "gate", "gateio": "gate", "real": "gate", "exchange": "gate",
            "live": "gate", "market": "gate", "": "gate",
        }
        return aliases.get(v.strip().lower(), v.strip().lower())

    @field_validator("timeframe")
    @classmethod
    def _check_timeframe(cls, v: str) -> str:
        if v not in TIMEFRAMES:
            raise ValueError(f"неизвестный таймфрейм: {v}")
        return v

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("символ не может быть пустым")
        return v

    @field_validator("entry_mode", mode="before")
    @classmethod
    def _norm_entry_mode(cls, v: Any) -> Any:
        """Старое значение (N свечей) принимаем как impulse, всё остальное — swing."""
        if not isinstance(v, str):
            return v
        alias = {"impulse": "impulse", "candles": "impulse", "n": "impulse",
                 "swing": "swing", "leg": "swing", "movement": "swing",
                 "swing_leg": "swing", "": "swing"}
        return alias.get(v.strip().lower(), "swing")

    @model_validator(mode="after")
    def _check_positions(self) -> "BotSettings":
        if self.multiplier_start_position > self.max_positions:
            self.multiplier_start_position = self.max_positions
        if self.entry_mode == "impulse" and self.swing_lookback < self.candles_count * 2:
            self.swing_lookback = self.candles_count * 2
        if self.market_type == "spot" and self.direction == "short":
            self.direction = "long"
        if self.trade_mode == "live" and self.synthetic_data:
            raise ValueError(
                "нельзя торговать реальными деньгами (live) на выдуманных свечах: выберите "
                "источник данных Gate.io или режим paper")
        return self

    @property
    def synthetic_data(self) -> bool:
        """True, если свечи выдуманы (нет реального рынка)."""
        return self.data_source == "synthetic"

    def data_source_label(self) -> str:
        if self.synthetic_data:
            return "синтетические свечи (выдуманный рынок)"
        return f"реальные свечи с биржи ({self.exchange_id})"

    def tf_seconds(self) -> int:
        return TIMEFRAMES[self.timeframe]

    def allowed_sides(self) -> Tuple[str, ...]:
        if self.market_type == "spot":
            return ("long",)
        if self.direction == "both":
            return ("long", "short")
        return (self.direction,)

    def volume_for_position(self, index: int) -> float:
        """Volume in $ of the ``index``-th (1-based) position of a cycle.

        Default schedule for base=10$, multiplier=2, start=3:
        10, 10, 20, 40, 80, 160 ...
        """
        if index < self.multiplier_start_position:
            return float(self.base_volume_usd)
        return float(self.base_volume_usd) * (self.volume_multiplier ** (index - self.multiplier_start_position + 1))

    def volume_schedule(self, n: int) -> list:
        return [round(self.volume_for_position(i), 4) for i in range(1, n + 1)]

    def public_dict(self) -> Dict[str, Any]:
        """Settings for the UI - secrets are masked."""
        d = self.model_dump()
        key = d.get("api_key") or ""
        d["api_key"] = ("*" * max(0, len(key) - 4) + key[-4:]) if key else ""
        d["api_secret_set"] = bool(self.api_secret)
        d["api_secret"] = ""
        d["telegram_token_set"] = bool(self.notify_telegram_token)
        d["notify_telegram_token"] = ""
        d["timeframes"] = list(TIMEFRAMES.keys())
        d["volume_schedule"] = self.volume_schedule(self.max_positions)
        return d

    def merge_secrets(self, old: "BotSettings") -> "BotSettings":
        """Keep stored secrets when the UI posts masked/empty values."""
        if not self.api_key or "*" in self.api_key:
            self.api_key = old.api_key
        if not self.api_secret or "*" in self.api_secret:
            self.api_secret = old.api_secret
        if not self.notify_telegram_token or "*" in self.notify_telegram_token:
            self.notify_telegram_token = old.notify_telegram_token
        return self


DEFAULT_SETTINGS = BotSettings()
