from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Direction(Enum):
    BUY = "BUY"
    SELL = "SELL"

    def opposite(self) -> Direction:
        return Direction.SELL if self is Direction.BUY else Direction.BUY


class ActionType(Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class ConnectionStatus(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class BridgeState(Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    open_price: float = 0.0
    open_time: int = 0    # Unix timestamp from MT5
    profit: float = 0.0   # Unrealized P&L from MT5

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "volume": self.volume,
            "open_price": self.open_price,
            "open_time": self.open_time,
            "profit": self.profit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Position:
        return cls(
            ticket=data["ticket"],
            symbol=data["symbol"],
            direction=Direction(data["direction"]),
            volume=data["volume"],
            open_price=data.get("open_price", 0.0),
            open_time=data.get("open_time", 0),
            profit=data.get("profit", 0.0),
        )


@dataclass
class TradeAction:
    action: ActionType
    symbol: str
    direction: Direction
    volume: float
    mt5_ticket: int
    xtb_order: int | None = None


@dataclass
class TicketMapping:
    """Maps MT5 ticket numbers to XTB order numbers for position tracking."""

    _map: dict[int, int] = field(default_factory=dict)

    def add(self, mt5_ticket: int, xtb_order: int) -> None:
        self._map[mt5_ticket] = xtb_order

    def remove(self, mt5_ticket: int) -> int | None:
        return self._map.pop(mt5_ticket, None)

    def get_xtb_order(self, mt5_ticket: int) -> int | None:
        return self._map.get(mt5_ticket)

    def has(self, mt5_ticket: int) -> bool:
        return mt5_ticket in self._map

    def tickets(self) -> list[int]:
        return list(self._map.keys())

    def to_dict(self) -> dict[str, int]:
        return {str(k): v for k, v in self._map.items()}

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> TicketMapping:
        mapping = cls()
        mapping._map = {int(k): v for k, v in data.items()}
        return mapping
