from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from . import mt5_reader
from .config import Config, MAPPING_FILE, POSITION_IDS_FILE
from .models import Direction, Position, TicketMapping
from .xtb_web import XTBWebSync

log = logging.getLogger(__name__)

RETRY_DELAYS = [1, 2, 4, 8, 16, 30]  # exponential backoff (seconds)
MAX_CLOSE_RETRIES = 10  # max retries for pending XTB closes before giving up


class BridgeWorker(QObject):
    """Runs the MT5→XTB sync loop on a dedicated QThread."""

    # Signals for GUI updates
    log_message = pyqtSignal(str)
    mt5_status = pyqtSignal(bool)
    xtb_status = pyqtSignal(bool)
    positions_updated = pyqtSignal(list)  # list of dicts for the trade table
    bridge_error = pyqtSignal(str)
    paused_changed = pyqtSignal(bool)  # emitted when pause state changes

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self._running = False
        self._paused = False
        self._mapping = TicketMapping()
        self._prev_positions: dict[int, Position] = {}
        self._xtb: XTBWebSync | None = None
        self._consecutive_errors = 0
        self._first_poll = True
        self._reconcile_counter = 0
        # Run XTB state reconciliation every N poll cycles. At 500ms poll
        # interval this is ~5s. Scraping XTB via Playwright is not free
        # (~0.5-1s per call) so we don't do it on every poll.
        self._reconcile_every_n_polls = 10
        # Track consecutive reconcile misses per ticket.
        # A ticket is removed from mapping only after RECONCILE_MISS_THRESHOLD
        # consecutive misses, preventing a single bad scrape from triggering a
        # re-open loop.
        self._reconcile_misses: dict[int, int] = {}
        self._reconcile_miss_threshold = 3
        # Tickets where auto-open is blocked. Set ONLY by:
        #   1. User clicking "Close XTB" button (explicit decision)
        #   2. Failed open_trade (to prevent 500ms retry spam)
        # Cleared by: user clicking "Reopen", or MT5 closing the ticket.
        # NOT set by reconcile — if XTB lost a position, bridge auto-reopens.
        self._user_closed_tickets: set[int] = set()
        # Pending manual-close requests from GUI (thread-safe append/popleft).
        # Entries: (xtb_symbol, xtb_direction_str, mt5_ticket)
        self._manual_close_queue: deque[tuple[str, str, int]] = deque()
        # Pending reopen requests from GUI — just a set of tickets to take
        # out of _user_closed_tickets on next poll.
        self._reopen_request_queue: deque[int] = deque()
        # Persistent queue of XTB closes triggered by MT5-side closes that
        # have NOT yet been confirmed. Entries: (mt5_ticket, xtb_symbol,
        # direction_str). Retried each poll until close_trade returns True,
        # so we never orphan an XTB position after MT5 closes its ticket.
        self._pending_xtb_closes: deque[tuple[int, str, str]] = deque()
        # Tickets added to mapping at startup (assumed mirrored — not confirmed).
        # Cleared per-ticket when reconcile confirms the symbol is in XTB, or
        # when open_trade succeeds for a new open.
        self._unconfirmed_tickets: set[int] = set()
        # Tickets the user wants to ignore (don't mirror to XTB at all).
        self._ignored_tickets: set[int] = set()
        # Queues for ignore/unignore from GUI (thread-safe)
        self._ignore_request_queue: deque[int] = deque()
        self._unignore_request_queue: deque[int] = deque()
        # Map MT5 ticket → XTB position ID captured after open_trade succeeds.
        # Used to close the exact XTB position (not just any matching direction).
        self._xtb_position_ids: dict[int, str] = {}
        # Events for close-all / force-sync requests from GUI thread
        self._close_all_requested = threading.Event()
        self._force_sync_requested = threading.Event()

    @property
    def lot_ratio(self) -> float:
        return self._config.lot_ratio

    @lot_ratio.setter
    def lot_ratio(self, value: float) -> None:
        self._config.lot_ratio = value
        self._emit_log(f"Lot size set to {value:.2f}")

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
        self._health_check_counter = 0
        # Run XTB health check every N polls (~60s at 500ms interval).
        # Keep it gentle — we don't want to disrupt trading by false-positive
        # session detection. The per-action _ensure_healthy() in xtb_web.py
        # handles the fast path.
        self._health_check_every_n_polls = 120
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
                    # Attempt auto-recovery
                    self._try_xtb_recovery()

                self._sleep(delay)
                continue

            # Periodic XTB health check
            self._health_check_counter += 1
            if self._health_check_counter >= self._health_check_every_n_polls:
                self._health_check_counter = 0
                self._periodic_health_check()

            self._sleep(self._config.poll_interval_ms / 1000.0)

        self._emit_log("Bridge stopped")
        self._cleanup()

    def _poll_cycle(self) -> None:
        # 0a. Apply queued reopen requests (just un-mark tickets so step 5 opens them)
        self._drain_reopen_requests()
        # 0b. Apply queued ignore/unignore requests
        self._drain_ignore_requests()
        # 0c. Process manual-close requests queued from the GUI (even when paused)
        self._drain_manual_closes()
        # 0d. Process close-all request
        if self._close_all_requested.is_set():
            self._close_all_requested.clear()
            self._drain_close_all()
        # 0e. Force sync if requested
        if self._force_sync_requested.is_set():
            self._force_sync_requested.clear()
            self._reconcile_counter = self._reconcile_every_n_polls
        # 0f. Retry any MT5-triggered XTB closes that didn't confirm last time
        if not self._paused:
            self._drain_pending_xtb_closes()

        # 1. Read current MT5 positions
        current = mt5_reader.get_open_positions(self._config.pairs)

        # 2. Emit positions for GUI table FIRST, before any blocking XTB calls.
        #    Otherwise the table stays empty whenever an open/close hangs in
        #    Playwright, and the user has no visibility into MT5 state.
        self._emit_positions(current)

        # 3. On the first poll after startup, optimistically mark all existing
        #    MT5 positions as already-mirrored (add to mapping) so the bridge
        #    doesn't try to open 10+ trades simultaneously on start. The next
        #    poll's reconciliation will prune entries whose symbols are NOT
        #    actually present in XTB, and the normal open-flow in step 4 will
        #    then (re)open them one-by-one.
        if self._first_poll:
            self._first_poll = False
            self._prev_positions = current

            # Prune stale mapping entries — tickets saved from previous sessions
            # that are no longer open in MT5. Keeping them causes reconcile to
            # constantly look for positions in XTB that will never exist.
            stale = [t for t in self._mapping.tickets() if t not in current]
            for t in stale:
                self._mapping.remove(t)
            if stale:
                self._emit_log(f"Pruned {len(stale)} stale mapping entries from previous sessions")
                self._save_mapping()

            for ticket in current.keys():
                if not self._mapping.has(ticket):
                    self._mapping.add(ticket, ticket)
                self._unconfirmed_tickets.add(ticket)  # all startup positions are unconfirmed
            if current:
                self._save_mapping()
                symbols = ", ".join(sorted({p.symbol for p in current.values()}))
                self._emit_log(
                    f"Initial sync: {len(current)} pre-existing MT5 position(s) "
                    f"[{symbols}] — assumed mirrored; will reconcile with XTB next cycle."
                )
            # Force reconcile on the very next poll so we detect mismatches
            # ASAP instead of waiting 10 polls.
            self._reconcile_counter = self._reconcile_every_n_polls
            return

        # When paused: still track MT5 state but skip XTB actions
        if self._paused:
            self._prev_positions = current
            return

        # 4. Reconcile FIRST (before opening), so that any positions the user
        #    closed manually in the XTB browser are pruned from mapping, then
        #    immediately re-opened by the loop below — matching the rule
        #    "if MT5 has the position, keep it mirrored in XTB".
        self._reconcile_counter += 1
        if (self._reconcile_counter >= self._reconcile_every_n_polls
                and self._mapping.tickets()):
            self._reconcile_counter = 0
            self._reconcile_xtb_state(current)

        # 5. Open missing positions: any MT5 ticket NOT in mapping AND NOT
        #    flagged as user-closed or ignored gets (re)opened.
        for ticket, pos in current.items():
            if self._mapping.has(ticket):
                continue
            if ticket in self._user_closed_tickets:
                continue
            if ticket in self._ignored_tickets:
                continue
            self._handle_new_position(pos)

        # 6. Detect closed positions (gone from MT5)
        for ticket, pos in self._prev_positions.items():
            if ticket not in current and self._mapping.has(ticket):
                self._handle_closed_position(ticket, pos)

        # 7. Cleanup: once MT5 no longer has a ticket, clear its flags
        self._user_closed_tickets.intersection_update(current.keys())
        self._ignored_tickets.intersection_update(current.keys())
        self._unconfirmed_tickets.intersection_update(current.keys())

        # 8. Update state
        self._prev_positions = current

        # 9. Re-emit positions so the GUI reflects mapping changes.
        self._emit_positions(current)

    def _reconcile_xtb_state(self, mt5_current: dict[int, Position]) -> None:
        """Prune mapping entries for positions no longer open in XTB.

        If MT5 has a position but XTB doesn't, the mapping entry is removed
        so step 5 of the next poll will auto-reopen it. This ensures the
        mirror stays in sync: any position in MT5 is always mirrored in XTB.
        """
        if not self._xtb:
            return

        try:
            xtb_rows = self._xtb.scrape_open_position_texts()
        except Exception as e:
            self._emit_log(f"Reconcile: scrape failed ({e}) — skipping")
            return

        if xtb_rows is None:
            # Couldn't reliably determine XTB state — don't prune on ambiguous
            # data, or we'd falsely clear the mapping during page transitions.
            return

        to_remove: list[int] = []
        for mt5_ticket in self._mapping.tickets():
            # Skip if MT5 position is gone too — normal closed-position flow
            # will handle it via _handle_closed_position.
            mt5_pos = mt5_current.get(mt5_ticket)
            if mt5_pos is None:
                continue

            xtb_symbol = self._config.map_symbol(mt5_pos.symbol)
            if not xtb_symbol:
                continue

            # Substring match against row text. XTB may display "EUR/USD"
            # instead of "EURUSD" — check both forms.
            _slashed = xtb_symbol[:3] + "/" + xtb_symbol[3:] if len(xtb_symbol) == 6 else xtb_symbol
            if any(xtb_symbol in row or _slashed in row for row in xtb_rows):
                self._reconcile_misses.pop(mt5_ticket, None)
                self._unconfirmed_tickets.discard(mt5_ticket)
            else:
                misses = self._reconcile_misses.get(mt5_ticket, 0) + 1
                self._reconcile_misses[mt5_ticket] = misses
                if misses >= self._reconcile_miss_threshold:
                    to_remove.append(mt5_ticket)
                else:
                    self._emit_log(
                        f"Reconcile: MT5 ticket {mt5_ticket} not found in XTB "
                        f"({misses}/{self._reconcile_miss_threshold}) — monitoring"
                    )

        for mt5_ticket in to_remove:
            self._mapping.remove(mt5_ticket)
            self._reconcile_misses.pop(mt5_ticket, None)
            self._emit_log(
                f"Reconcile: MT5 ticket {mt5_ticket} missing from XTB for "
                f"{self._reconcile_miss_threshold} consecutive checks "
                f"— will auto-open on next poll"
            )

        if to_remove:
            self._save_mapping()

    def _drain_ignore_requests(self) -> None:
        """Apply ignore/unignore clicks from the GUI."""
        while self._ignore_request_queue:
            try:
                ticket = self._ignore_request_queue.popleft()
            except IndexError:
                break
            self._ignored_tickets.add(ticket)
            # If this ticket is currently mirrored, close it in XTB first
            if self._mapping.has(ticket):
                pos = self._prev_positions.get(ticket)
                if pos and self._xtb:
                    xtb_symbol = self._config.map_symbol(pos.symbol)
                    if xtb_symbol:
                        direction = pos.direction
                        if self._config.reverse_mode:
                            direction = direction.opposite()
                        self._emit_log(
                            f"Ignoring ticket {ticket} — closing {direction.value} "
                            f"{xtb_symbol} in XTB"
                        )
                        try:
                            self._xtb.close_trade(xtb_symbol, direction)
                        except Exception as e:
                            self._emit_log(f"Close on ignore failed: {e}")
                self._mapping.remove(ticket)
                self._save_mapping()
            self._emit_log(f"Ticket {ticket} added to ignore list")

        while self._unignore_request_queue:
            try:
                ticket = self._unignore_request_queue.popleft()
            except IndexError:
                break
            self._ignored_tickets.discard(ticket)
            self._emit_log(
                f"Ticket {ticket} removed from ignore list — will mirror on next poll"
            )

    def _drain_close_all(self) -> None:
        """Close all currently mirrored positions in XTB."""
        if not self._xtb:
            self._emit_log("Cannot close all — XTB not connected")
            return

        tickets_to_close = list(self._mapping.tickets())
        if not tickets_to_close:
            self._emit_log("Close all: no mirrored positions to close")
            return

        self._emit_log(f"CLOSE ALL: closing {len(tickets_to_close)} position(s)...")
        for ticket in tickets_to_close:
            pos = self._prev_positions.get(ticket)
            if not pos:
                continue
            xtb_symbol = self._config.map_symbol(pos.symbol)
            if not xtb_symbol:
                continue
            direction = pos.direction
            if self._config.reverse_mode:
                direction = direction.opposite()
            try:
                ok = self._xtb.close_trade(xtb_symbol, direction)
            except Exception as e:
                self._emit_log(f"Close all error for {xtb_symbol}: {e}")
                ok = False
            if ok:
                self._mapping.remove(ticket)
                self._user_closed_tickets.add(ticket)
                self._emit_log(f"Closed {xtb_symbol} (ticket {ticket})")
            else:
                self._emit_log(f"Failed to close {xtb_symbol} (ticket {ticket})")
        self._save_mapping()

    def _drain_reopen_requests(self) -> None:
        """Apply Reopen clicks from the GUI: take tickets out of the
        user-closed set so the next poll's step 5 will open them again."""
        while self._reopen_request_queue:
            try:
                ticket = self._reopen_request_queue.popleft()
            except IndexError:
                break
            if ticket in self._user_closed_tickets:
                self._user_closed_tickets.discard(ticket)
                self._emit_log(
                    f"Reopen ticket {ticket}: bridge will retry opening in XTB"
                )

    def _drain_pending_xtb_closes(self) -> None:
        """Retry queued XTB closes triggered earlier by MT5-side closes.

        Successful entries drop out of the queue and clear the mapping.
        Failed entries are requeued for the next poll so we keep trying
        until the XTB side is actually closed.
        """
        if not self._pending_xtb_closes or not self._xtb:
            return

        # Snapshot-and-clear so newly-added entries from this poll are handled
        # on the NEXT poll (not this one) — avoids infinite loops if an entry
        # keeps failing.
        pending = list(self._pending_xtb_closes)
        self._pending_xtb_closes.clear()

        for entry in pending:
            # Support 3-tuple (legacy), 4-tuple (retry count), 5-tuple (+ position ID)
            if len(entry) == 5:
                ticket, xtb_symbol, direction_str, retry_count, pos_id = entry
            elif len(entry) == 4:
                ticket, xtb_symbol, direction_str, retry_count = entry
                pos_id = ""
            else:
                ticket, xtb_symbol, direction_str = entry
                retry_count = 0
                pos_id = ""

            try:
                direction = Direction(direction_str)
            except ValueError:
                self._emit_log(
                    f"Dropping pending close with invalid direction "
                    f"'{direction_str}' (ticket={ticket})"
                )
                if self._mapping.has(ticket):
                    self._mapping.remove(ticket)
                    self._save_mapping()
                continue

            self._emit_log(
                f"Retry XTB close: MT5 ticket {ticket} "
                f"({direction.value} {xtb_symbol}) attempt {retry_count + 1}/{MAX_CLOSE_RETRIES}"
            )
            try:
                ok = self._xtb.close_trade(xtb_symbol, direction, xtb_position_id=pos_id or None)
            except Exception as e:
                self._emit_log(f"Retry close error for {xtb_symbol}: {e}")
                ok = False

            if ok:
                if self._mapping.has(ticket):
                    self._mapping.remove(ticket)
                    self._save_mapping()
                self._emit_log(f"Retry close succeeded for {xtb_symbol}")
            elif retry_count + 1 < MAX_CLOSE_RETRIES:
                # Requeue for next poll
                self._pending_xtb_closes.append(
                    (ticket, xtb_symbol, direction_str, retry_count + 1, pos_id)
                )
            else:
                self._emit_log(
                    f"Giving up on close for {xtb_symbol} (ticket={ticket}) "
                    f"after {MAX_CLOSE_RETRIES} attempts — removing from mapping"
                )
                if self._mapping.has(ticket):
                    self._mapping.remove(ticket)
                    self._save_mapping()

    def _drain_manual_closes(self) -> None:
        """Process any manual-close requests enqueued from the GUI thread."""
        while self._manual_close_queue:
            try:
                xtb_symbol, direction_str, mt5_ticket = self._manual_close_queue.popleft()
            except IndexError:
                break

            if not self._xtb:
                self._emit_log(f"Cannot close {xtb_symbol} manually — XTB not connected")
                continue

            self._emit_log(
                f"MANUAL CLOSE {direction_str} {xtb_symbol} "
                f"(MT5 ticket={mt5_ticket})"
            )
            try:
                direction = Direction(direction_str)
            except ValueError:
                self._emit_log(f"Invalid direction '{direction_str}' — skipping")
                continue

            try:
                ok = self._xtb.close_trade(xtb_symbol, direction)
            except Exception as e:
                self._emit_log(f"Manual close failed for {xtb_symbol}: {e}")
                continue

            if ok:
                # Remove the mapping so the bridge won't try to close it
                # again when MT5 eventually closes the position, and flag
                # the ticket as user-closed so step 5 doesn't auto-reopen.
                if self._mapping.has(mt5_ticket):
                    self._mapping.remove(mt5_ticket)
                    self._save_mapping()
                self._user_closed_tickets.add(mt5_ticket)
                self._emit_log(f"Manually closed {xtb_symbol} in XTB")
            else:
                self._emit_log(f"XTB did not confirm manual close for {xtb_symbol}")

    def _handle_new_position(self, pos: Position) -> None:
        xtb_symbol = self._config.map_symbol(pos.symbol)
        if not xtb_symbol:
            self._emit_log(f"SKIP: No symbol mapping for {pos.symbol}")
            return

        volume = round(self._config.lot_for_symbol(xtb_symbol), 2)
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
            self._mapping.add(pos.ticket, pos.ticket)
            self._save_mapping()
            self._user_closed_tickets.discard(pos.ticket)
            self._unconfirmed_tickets.discard(pos.ticket)  # confirmed by successful open
            pos_id = self._xtb.last_opened_position_id
            if pos_id:
                self._xtb_position_ids[pos.ticket] = pos_id
                self._save_position_ids()
                self._emit_log(f"XTB position ID {pos_id} stored for MT5 ticket {pos.ticket}")
        else:
            # Failed opens are flagged as user-closed so the bridge doesn't
            # retry every 500ms. User must click Reopen to try again.
            self._user_closed_tickets.add(pos.ticket)
            self._emit_log(
                f"FAILED to open {xtb_symbol} in XTB — click Reopen to retry"
            )

    def _handle_closed_position(self, ticket: int, pos: Position) -> None:
        """Triggered when MT5 closes a ticket that was mirrored in XTB.

        Tries to close the XTB side immediately; if it fails, enqueues for
        retry on subsequent polls so the XTB position is never orphaned.
        """
        xtb_symbol = self._config.map_symbol(pos.symbol)
        if not xtb_symbol:
            # Can't close without a symbol mapping — just forget the ticket
            self._mapping.remove(ticket)
            self._save_mapping()
            return

        direction = pos.direction
        if self._config.reverse_mode:
            direction = direction.opposite()

        self._emit_log(
            f"MT5 closed ticket {ticket} — closing {direction.value} "
            f"{xtb_symbol} in XTB"
        )

        pos_id = self._xtb_position_ids.pop(ticket, None)
        if pos_id is not None:
            self._save_position_ids()

        ok = False
        if self._xtb:
            try:
                ok = self._xtb.close_trade(xtb_symbol, direction, xtb_position_id=pos_id)
            except Exception as e:
                self._emit_log(f"XTB close error for {xtb_symbol}: {e}")
                ok = False

        if ok:
            self._mapping.remove(ticket)
            self._save_mapping()
            self._emit_log(f"XTB closed for MT5 ticket {ticket}")
        else:
            # Keep mapping entry intact and enqueue retry. Otherwise the
            # XTB position would be orphaned and we'd have no way to
            # rediscover it (step 6 needs ticket in _prev_positions).
            self._pending_xtb_closes.append((ticket, xtb_symbol, direction.value, 0, pos_id or ""))
            self._emit_log(
                f"XTB close not confirmed for {xtb_symbol} "
                f"— queued for retry next poll"
            )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    @pyqtSlot()
    def stop(self) -> None:
        self._emit_log("Stopping bridge...")
        self._running = False

    def request_manual_close(self, xtb_symbol: str, direction: str,
                             mt5_ticket: int) -> None:
        """Queue a manual XTB close request. Safe to call from the GUI thread.

        The actual close runs in the bridge thread on the next poll cycle.
        """
        if not self._running:
            self._emit_log(
                f"Ignoring manual close of {xtb_symbol} — bridge is not running"
            )
            return
        self._manual_close_queue.append((xtb_symbol, direction, mt5_ticket))
        self._emit_log(
            f"Queued manual close: {direction} {xtb_symbol} (ticket={mt5_ticket})"
        )

    def request_reopen(self, mt5_ticket: int) -> None:
        """Queue a reopen request for a user-closed ticket. GUI-thread safe.

        On the next poll, the ticket is removed from _user_closed_tickets and
        step 5 of _poll_cycle will try to (re)open it in XTB.
        """
        if not self._running:
            self._emit_log(
                f"Ignoring reopen of ticket {mt5_ticket} — bridge is not running"
            )
            return
        self._reopen_request_queue.append(mt5_ticket)
        self._emit_log(f"Queued reopen for MT5 ticket {mt5_ticket}")

    @pyqtSlot()
    def toggle_pause(self) -> None:
        """Toggle pause state. GUI-thread safe."""
        self._paused = not self._paused
        state = "PAUSED" if self._paused else "RESUMED"
        self._emit_log(f"Bridge {state}")
        self.paused_changed.emit(self._paused)

    @property
    def paused(self) -> bool:
        return self._paused

    def request_ignore(self, mt5_ticket: int) -> None:
        """Queue an ignore request. GUI-thread safe."""
        self._ignore_request_queue.append(mt5_ticket)
        self._emit_log(f"Queued ignore for MT5 ticket {mt5_ticket}")

    def request_unignore(self, mt5_ticket: int) -> None:
        """Queue an unignore request. GUI-thread safe."""
        self._unignore_request_queue.append(mt5_ticket)
        self._emit_log(f"Queued unignore for MT5 ticket {mt5_ticket}")

    def request_close_all(self) -> None:
        """Queue a close-all request. GUI-thread safe."""
        if not self._running:
            self._emit_log("Ignoring close-all — bridge is not running")
            return
        self._close_all_requested.set()
        self._emit_log("Queued CLOSE ALL XTB positions")

    def request_force_sync(self) -> None:
        """Force immediate reconciliation on next poll. GUI-thread safe."""
        self._force_sync_requested.set()
        self._emit_log("Queued force sync")

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
            xtb_volume = round(self._config.lot_for_symbol(xtb_symbol), 2)
            direction = pos.direction
            if self._config.reverse_mode:
                direction = direction.opposite()

            # Determine status string for the GUI
            if ticket in self._ignored_tickets:
                status = "IGNORED"
            elif ticket in self._user_closed_tickets:
                status = "CLOSED_XTB"
            elif self._mapping.has(ticket):
                # ASSUMED = added at startup (not yet confirmed by reconcile or open_trade)
                # SYNCED  = confirmed by successful open_trade or reconcile check
                status = "ASSUMED" if ticket in self._unconfirmed_tickets else "SYNCED"
            else:
                status = "PENDING"

            rows.append({
                "mt5_ticket": ticket,
                "symbol_mt5": pos.symbol,
                "symbol_xtb": xtb_symbol,
                "direction_mt5": pos.direction.value,
                "direction_xtb": direction.value,
                "volume_mt5": pos.volume,
                "volume_xtb": xtb_volume,
                "open_price": pos.open_price,
                "open_time": pos.open_time,
                "profit": pos.profit,
                "mirrored": self._mapping.has(ticket),
                "closed_in_xtb": ticket in self._user_closed_tickets,
                "ignored": ticket in self._ignored_tickets,
                "status": status,
                "paused": self._paused,
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
        self._load_position_ids()

    def _load_position_ids(self) -> None:
        if POSITION_IDS_FILE.exists():
            try:
                with open(POSITION_IDS_FILE) as f:
                    data = json.load(f)
                self._xtb_position_ids = {int(k): v for k, v in data.items()}
                if self._xtb_position_ids:
                    self._emit_log(
                        f"Loaded {len(self._xtb_position_ids)} saved XTB position IDs"
                    )
            except Exception as e:
                self._emit_log(f"Could not load position IDs: {e}")

    def _save_position_ids(self) -> None:
        try:
            with open(POSITION_IDS_FILE, "w") as f:
                json.dump({str(k): v for k, v in self._xtb_position_ids.items()}, f, indent=2)
        except Exception as e:
            log.warning("Failed to save position IDs: %s", e)

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

    def _periodic_health_check(self) -> None:
        """Run a periodic XTB session health check from the poll loop."""
        if not self._xtb:
            return
        try:
            if not self._xtb.check_session_health():
                self._emit_log("Periodic health check: XTB session unhealthy")
                self.xtb_status.emit(False)
                self._try_xtb_recovery()
        except Exception as e:
            self._emit_log(f"Health check error: {e}")

    def _try_xtb_recovery(self) -> None:
        """Attempt to recover XTB session."""
        if not self._xtb:
            return
        try:
            if self._xtb.recover_session():
                self._emit_log("XTB session recovered")
                self.xtb_status.emit(True)
            else:
                self._emit_log("XTB session recovery failed — manual intervention needed")
                self.xtb_status.emit(False)
        except Exception as e:
            self._emit_log(f"XTB recovery error: {e}")
            self.xtb_status.emit(False)

    def _sleep(self, seconds: float) -> None:
        """Sleep in small increments so we can respond to stop() quickly."""
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            QThread.msleep(100)
