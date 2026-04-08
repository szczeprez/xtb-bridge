from __future__ import annotations

import logging

import MetaTrader5 as mt5

from .models import Direction, Position

log = logging.getLogger(__name__)


def connect(terminal_path: str | None = None) -> bool:
    kwargs = {}
    if terminal_path:
        kwargs["path"] = terminal_path
    if not mt5.initialize(**kwargs):
        err = mt5.last_error()
        log.error("MT5 initialize failed: %s", err)
        return False
    info = mt5.terminal_info()
    log.info("MT5 connected: %s (build %s)", info.name, info.build)
    return True


def disconnect() -> None:
    mt5.shutdown()
    log.info("MT5 disconnected")


def is_connected() -> bool:
    try:
        info = mt5.terminal_info()
        return info is not None
    except Exception:
        return False


def get_open_positions(symbols: list[str]) -> dict[int, Position]:
    positions: dict[int, Position] = {}

    all_pos = mt5.positions_get()
    if all_pos is None:
        log.warning("MT5 positions_get() returned None: %s", mt5.last_error())
        return positions

    for p in all_pos:
        if p.symbol not in symbols:
            continue
        direction = Direction.BUY if p.type == mt5.ORDER_TYPE_BUY else Direction.SELL
        positions[p.ticket] = Position(
            ticket=p.ticket,
            symbol=p.symbol,
            direction=direction,
            volume=p.volume,
            open_price=p.price_open,
            sl=p.sl,
            tp=p.tp,
        )

    return positions
