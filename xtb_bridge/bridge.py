from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from . import mt5_reader
from .config import Config, MAPPING_FILE
from .models import Direction, Position, TicketMapping
from .xtb_web import XTBWebSync

log = logging.getLogger(__name__)

RETRY_DELAYS = [1, 2, 4, 8, 16, 30]  # exponential backoff (seconds)


class BridgeWorker(QObject):
    """Runs the MT5→XTB sync loop on a dedicated QThread."""

    # Signals for GUI updates
    log_message = pyqtSignal(str)
    mt5_status = pyqtSignal(bool)
    xtb_status = pyqtSignal(bool)
    positions_updated = pyqtSignal(list)  # list of dicts for the trade table
    bridge_error = pyqtSignal(str)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self._running = False
        self._mapping = TicketMapping()
        self._prev_positions: dict[int, Position] = {}
        self._xtb: XTBWebSync | None = None
        self._consecutive_errors = 0

    @property
    def lot_ratio(self) -> float:
        return self._config.lot_ratio

    @lot_ratio.setter
    def lot_ratio(self, value: float) -> None:
        self._config.lot_ratio = value
        self._emit_log(f"Lot ratio changed to {value:.2f}")

    @property
    def reverse_mode(self) -> bool:
        return self._config.reverse_mode

    @reverse_mode.setter
    def reverse_mode(self, value: bool) -> None:
        self._config.reverse_mode = value
        self._emit_log(f"Reverse mode {'ON' if value else 'OFF'}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    @pyqtSlot()
    def run_loop(self) -> None:
        self._running = True
        self._emit_log("Bridge starting...")

        # --- Connect MT5 ---
        if not mt5_reader.connect(self._config.mt5_terminal_path):
            self._emit_log("ERROR: Cannot connect to MT5. Is the terminal running?")
            self.mt5_status.emit(False)
            self.bridge_error.emit("MT5 connection failed")
            self._running = False
            return
        self.mt5_status.emit(True)
        self._emit_log("MT5 connected")

        # --- Launch & login XTB browser ---
        self._xtb = XTBWebSync(
            email=self._config.xtb_email,
            password=self._config.xtb_password,
            account_type=self._config.xtb_account_type,
            on_log=self._emit_log,
        )
        try:
            self._xtb.launch()
            if not self._xtb.login():
                self._emit_log("ERROR: XTB login failed")
                self.xtb_status.emit(False)
                self.bridge_error.emit("XTB login failed")
                self._cleanup()
                return
            self.xtb_status.emit(True)
        except Exception as e:
            self._emit_log(f"ERROR: XTB browser launch failed: {e}")
            self.xtb_status.emit(False)
            self.bridge_error.emit(str(e))
            self._cleanup()
            return

        # --- Load saved mapping ---
        self._load_mapping()

        # --- Main poll loop ---
        self._emit_log("Bridge running — monitoring positions...")
        while self._running:
            try:
                self._poll_cycle()
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                delay = RETRY_DELAYS[min(self._consecutive_errors - 1, len(RETRY_DELAYS) - 1)]
                self._emit_log(f"ERROR in poll cycle: {e} (retry in {delay}s)")
                log.exception("Poll cycle error")

                if not mt5_reader.is_connected():
                    self.mt5_status.emit(False)
                if self._xtb and not self._xtb.is_logged_in():
                    self.xtb_status.emit(False)

                self._sleep(delay)
                continue

            self._sleep(self._config.poll_interval_ms / 1000.0)

        self._emit_log("Bridge stopped")
        self._cleanup()

    def _poll_cycle(self) -> None:
        # 1. Read current MT5 positions
        current = mt5_reader.get_open_positions(self._config.pairs)

        # 2. Detect new positions (opened in MT5)
        for ticket, pos in current.items():
            if ticket not in self._prev_positions and not self._mapping.has(ticket):
                self._handle_new_position(pos)

        # 3. Detect closed positions (gone from MT5)
        for ticket, pos in self._prev_positions.items():
            if ticket not in current and self._mapping.has(ticket):
                self._handle_closed_position(ticket, pos)

        # 4. Update state
        self._prev_positions = current

        # 5. Emit positions for GUI table
        self._emit_positions(current)

    def _handle_new_position(self, pos: Position) -> None:
        xtb_symbol = self._config.map_symbol(pos.symbol)
        if not xtb_symbol:
            self._emit_log(f"SKIP: No symbol mapping for {pos.symbol}")
            return

        volume = round(pos.volume * self._config.lot_ratio, 2)
        if volume < 0.01:
            self._emit_log(f"SKIP: Volume too small ({volume}) for {pos.symbol}")
            return

        direction = pos.direction
        if self._config.reverse_mode:
            direction = direction.opposite()

        self._emit_log(
            f"OPEN {direction.value} {xtb_symbol} {volume} lots "
            f"(MT5: {pos.direction.value} {pos.symbol} {pos.volume} lots, ticket={pos.ticket})"
        )

        if self._xtb and self._xtb.open_trade(xtb_symbol, direction, volume):
            # Store mapping (using ticket as placeholder since we can't get XTB order from UI)
            self._mapping.add(pos.ticket, pos.ticket)
            self._save_mapping()
        else:
            self._emit_log(f"FAILED to open {xtb_symbol} in XTB")

    def _handle_closed_position(self, ticket: int, pos: Position) -> None:
        xtb_symbol = self._config.map_symbol(pos.symbol)
        if not xtb_symbol:
            self._mapping.remove(ticket)
            return

        direction = pos.direction
        if self._config.reverse_mode:
            direction = direction.opposite()

        self._emit_log(
            f"CLOSE {direction.value} {xtb_symbol} "
            f"(MT5 ticket={ticket})"
        )

        if self._xtb:
            self._xtb.close_trade(xtb_symbol, direction)
        self._mapping.remove(ticket)
        self._save_mapping()

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    @pyqtSlot()
    def stop(self) -> None:
        self._emit_log("Stopping bridge...")
        self._running = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_log(self, msg: str) -> None:
        log.info(msg)
        self.log_message.emit(msg)

    def _emit_positions(self, mt5_positions: dict[int, Position]) -> None:
        rows = []
        for ticket, pos in mt5_positions.items():
            xtb_symbol = self._config.map_symbol(pos.symbol) or "?"
            xtb_volume = round(pos.volume * self._config.lot_ratio, 2)
            direction = pos.direction
            if self._config.reverse_mode:
                direction = direction.opposite()
            rows.append({
                "mt5_ticket": ticket,
                "symbol_mt5": pos.symbol,
                "symbol_xtb": xtb_symbol,
                "direction_mt5": pos.direction.value,
                "direction_xtb": direction.value,
                "volume_mt5": pos.volume,
                "volume_xtb": xtb_volume,
                "mirrored": self._mapping.has(ticket),
            })
        self.positions_updated.emit(rows)

    def _load_mapping(self) -> None:
        if MAPPING_FILE.exists():
            try:
                with open(MAPPING_FILE) as f:
                    data = json.load(f)
                self._mapping = TicketMapping.from_dict(data)
                self._emit_log(f"Loaded {len(data)} saved position mappings")
            except Exception as e:
                self._emit_log(f"Could not load mapping: {e}")
                self._mapping = TicketMapping()

    def _save_mapping(self) -> None:
        try:
            with open(MAPPING_FILE, "w") as f:
                json.dump(self._mapping.to_dict(), f, indent=2)
        except Exception as e:
            log.warning("Failed to save mapping: %s", e)

    def _cleanup(self) -> None:
        self._running = False
        try:
            mt5_reader.disconnect()
        except Exception:
            pass
        self.mt5_status.emit(False)
        if self._xtb:
            try:
                self._xtb.close()
            except Exception:
                pass
            self._xtb = None
        self.xtb_status.emit(False)

    def _sleep(self, seconds: float) -> None:
        """Sleep in small increments so we can respond to stop() quickly."""
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            QThread.msleep(100)
