"""XTB Bridge — entry point."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtCore import QThread
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .bridge import BridgeWorker
from .config import load_config, save_user_settings
from .gui.main_window import MainWindow

LOG_FILE = Path("xtb_bridge.log")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)
    log.info("XTB Bridge starting...")

    config = load_config()
    errors = config.validate()
    if errors:
        log.warning("Config validation warnings: %s", errors)

    app = QApplication(sys.argv)
    app.setApplicationName("XTB Bridge")
    app.setQuitOnLastWindowClosed(False)  # keep alive when minimized to tray

    window = MainWindow()
    window.set_lot_ratio(config.lot_ratio)
    window.set_reverse_mode(config.reverse_mode)

    # -----------------------------------------------------------------------
    # System tray
    # -----------------------------------------------------------------------
    tray = QSystemTrayIcon(app)
    # Use a built-in Qt icon as fallback (no external icon file needed)
    tray.setIcon(app.style().standardIcon(
        app.style().StandardPixmap.SP_ComputerIcon  # type: ignore[attr-defined]
    ))
    tray.setToolTip("XTB Bridge")

    tray_menu = QMenu()
    tray_open_action = tray_menu.addAction("Open")
    tray_menu.addSeparator()
    tray_pause_action = tray_menu.addAction("Pause / Resume")
    tray_menu.addSeparator()
    tray_quit_action = tray_menu.addAction("Quit")

    tray_open_action.triggered.connect(lambda: (window.show(), window.raise_()))
    tray_pause_action.triggered.connect(lambda: window.pause_requested.emit())
    tray_quit_action.triggered.connect(lambda: (window.stop_requested.emit(), app.quit()))

    tray.setContextMenu(tray_menu)
    tray.activated.connect(
        lambda reason: (window.show(), window.raise_())
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
    )
    tray.show()

    # -----------------------------------------------------------------------
    # Bridge lifecycle
    # -----------------------------------------------------------------------
    bridge_state: dict = {"thread": None, "worker": None}

    def _wire_worker(worker: BridgeWorker, thread: QThread) -> None:
        thread.started.connect(worker.run_loop)

        worker.log_message.connect(window.log_widget.append_log)
        worker.mt5_status.connect(window.set_mt5_status)
        worker.xtb_status.connect(window.set_xtb_status)
        worker.positions_updated.connect(window.trade_table.update_positions)
        worker.positions_updated.connect(window._on_positions_updated)
        worker.bridge_error.connect(window.on_bridge_error)
        worker.paused_changed.connect(window.set_paused)

        def _on_lot_changed(v: float) -> None:
            setattr(worker, "lot_ratio", v)
            config.lot_ratio = v
            save_user_settings(config)

        def _on_reverse_changed(v: bool) -> None:
            setattr(worker, "reverse_mode", v)
            config.reverse_mode = v
            save_user_settings(config)

        window.lot_ratio_changed.connect(_on_lot_changed)
        window.reverse_mode_changed.connect(_on_reverse_changed)
        window.pause_requested.connect(worker.toggle_pause)
        window.trade_table.close_requested.connect(worker.request_manual_close)
        window.trade_table.reopen_requested.connect(worker.request_reopen)
        window.trade_table.ignore_requested.connect(worker.request_ignore)
        window.trade_table.unignore_requested.connect(worker.request_unignore)
        window.close_all_requested.connect(worker.request_close_all)
        window.force_sync_requested.connect(worker.request_force_sync)

    def _cleanup_bridge() -> None:
        worker = bridge_state.get("worker")
        thread = bridge_state.get("thread")
        if worker:
            worker.stop()
        if thread and thread.isRunning():
            thread.quit()
            thread.wait(5000)
        _disconnect_gui_signals()
        bridge_state["worker"] = None
        bridge_state["thread"] = None

    def _disconnect_gui_signals() -> None:
        for sig in [
            window.lot_ratio_changed,
            window.reverse_mode_changed,
            window.pause_requested,
            window.close_all_requested,
            window.force_sync_requested,
        ]:
            try:
                sig.disconnect()
            except TypeError:
                pass
        for sig in [
            window.trade_table.close_requested,
            window.trade_table.reopen_requested,
            window.trade_table.ignore_requested,
            window.trade_table.unignore_requested,
        ]:
            try:
                sig.disconnect()
            except TypeError:
                pass

    def on_start() -> None:
        if not config.xtb_email or not config.xtb_password:
            QMessageBox.warning(
                window,
                "Missing Credentials",
                "Please set XTB email and password in config.toml before starting.",
            )
            window._bridge_running = False
            window._update_start_stop_style()
            window._set_operational_buttons_enabled(False)
            window._update_bridge_state_label()
            return

        _cleanup_bridge()
        thread = QThread()
        worker = BridgeWorker(config)
        worker.moveToThread(thread)
        _wire_worker(worker, thread)
        bridge_state["thread"] = thread
        bridge_state["worker"] = worker
        thread.start()

    def on_stop() -> None:
        _cleanup_bridge()

    window.start_requested.connect(on_start)
    window.stop_requested.connect(on_stop)

    # Show window + errors
    window.show()
    window.log_widget.append_log("XTB Bridge ready. Press START to begin.")
    if errors:
        for e in errors:
            window.log_widget.append_log(f"Config warning: {e}")
        QMessageBox.warning(
            window,
            "Configuration warnings",
            "Config validation issues:\n" + "\n".join(f"• {e}" for e in errors),
        )

    exit_code = app.exec()
    _cleanup_bridge()
    log.info("XTB Bridge exited (code=%d)", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
