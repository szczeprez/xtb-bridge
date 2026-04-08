"""XTB Bridge — entry point.

Wires together config, bridge worker, and GUI.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QMessageBox

from .bridge import BridgeWorker
from .config import load_config
from .gui.main_window import MainWindow

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FILE = Path("xtb_bridge.log")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (rotating would be nicer, but keep it simple for MVP)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)
    log.info("XTB Bridge starting...")

    # Load config
    config = load_config()
    errors = config.validate()
    if errors:
        log.warning("Config validation warnings: %s", errors)

    # Qt application
    app = QApplication(sys.argv)
    app.setApplicationName("XTB Bridge")

    # Main window
    window = MainWindow()
    window.set_lot_ratio(config.lot_ratio)

    # Bridge worker + thread
    bridge_thread = QThread()
    worker = BridgeWorker(config)
    worker.moveToThread(bridge_thread)

    # --- Wire signals ---

    # Start: GUI → thread start → worker.run_loop
    def on_start():
        if not config.xtb_email or not config.xtb_password:
            QMessageBox.warning(
                window,
                "Missing Credentials",
                "Please set XTB email and password in config.toml before starting.",
            )
            window._bridge_running = False
            window._update_start_stop_style()
            return
        bridge_thread.start()

    window.start_requested.connect(on_start)
    bridge_thread.started.connect(worker.run_loop)

    # Stop: GUI → worker.stop → thread quit
    window.stop_requested.connect(worker.stop)
    window.stop_requested.connect(bridge_thread.quit)

    # Worker → GUI updates
    worker.log_message.connect(window.log_widget.append_log)
    worker.mt5_status.connect(window.set_mt5_status)
    worker.xtb_status.connect(window.set_xtb_status)
    worker.positions_updated.connect(window.trade_table.update_positions)
    worker.bridge_error.connect(window.on_bridge_error)

    # Lot ratio changes: GUI → worker
    window.lot_ratio_changed.connect(lambda v: setattr(worker, "lot_ratio", v))

    # Show window
    window.show()
    window.log_widget.append_log("XTB Bridge ready. Press START to begin.")

    if errors:
        for e in errors:
            window.log_widget.append_log(f"Config warning: {e}")

    # Run event loop
    exit_code = app.exec()

    # Cleanup
    if bridge_thread.isRunning():
        worker.stop()
        bridge_thread.quit()
        bridge_thread.wait(5000)

    log.info("XTB Bridge exited (code=%d)", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
