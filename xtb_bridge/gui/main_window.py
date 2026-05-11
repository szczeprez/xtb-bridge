from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .log_widget import LogWidget
from .trade_table import TradeTable


class StatusIndicator(QLabel):
    """Small colored circle indicating connection status."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self.set_status(False)

    def set_status(self, connected: bool) -> None:
        color = "#22c55e" if connected else "#ef4444"
        text_color = "#4ade80" if connected else "#f87171"
        status_text = "Connected" if connected else "Disconnected"
        self.setText(f"  {self._label}: {status_text}")
        self.setStyleSheet(
            f"QLabel {{"
            f"  color: {text_color};"
            f"  font-weight: bold;"
            f"  font-size: 13px;"
            f"  padding: 4px 12px;"
            f"  background: {color}22;"
            f"  border: 1px solid {color}44;"
            f"  border-radius: 6px;"
            f"}}"
        )


class MainWindow(QMainWindow):
    """Main application window for XTB Bridge."""

    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    lot_ratio_changed = pyqtSignal(float)
    reverse_mode_changed = pyqtSignal(bool)
    pause_requested = pyqtSignal()
    close_all_requested = pyqtSignal()
    force_sync_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("XTB Bridge — MT5 Mirror Trading")
        self.setMinimumSize(1200, 750)
        self._bridge_running = False
        self._bridge_paused = False
        self._active_positions: list[dict] = []
        self._setup_ui()
        self._apply_dark_theme()

    # ------------------------------------------------------------------
    # UI Setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # --- Status bar ---
        status_row = QHBoxLayout()
        self._mt5_indicator = StatusIndicator("MT5")
        self._xtb_indicator = StatusIndicator("XTB")
        self._bridge_state_label = QLabel("  Bridge: Stopped")
        self._bridge_state_label.setStyleSheet(
            "QLabel { color: #94a3b8; font-weight: bold; font-size: 13px;"
            "  padding: 4px 12px; background: #47556922;"
            "  border: 1px solid #47556944; border-radius: 6px; }"
        )
        status_row.addWidget(self._mt5_indicator)
        status_row.addWidget(self._xtb_indicator)
        status_row.addWidget(self._bridge_state_label)
        status_row.addStretch()
        layout.addLayout(status_row)

        # --- Controls row 1: START/STOP, PAUSE, lot ratio ---
        controls = QGroupBox("Controls")
        controls.setStyleSheet(
            "QGroupBox { color: #94a3b8; font-weight: bold; font-size: 13px;"
            "  border: 1px solid #334155; border-radius: 8px; padding: 16px; padding-top: 24px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; }"
        )
        controls_layout = QVBoxLayout(controls)

        row1 = QHBoxLayout()

        # START/STOP button
        self._start_stop_btn = QPushButton("START")
        self._start_stop_btn.setFixedSize(120, 40)
        self._start_stop_btn.clicked.connect(self._on_start_stop)
        self._update_start_stop_style()
        row1.addWidget(self._start_stop_btn)

        # PAUSE button
        self._pause_btn = QPushButton("PAUSE")
        self._pause_btn.setFixedSize(100, 40)
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self._on_pause)
        self._update_pause_style()
        row1.addWidget(self._pause_btn)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet("color: #334155;")
        row1.addWidget(sep1)

        # Lot size input
        lot_label = QLabel("Lot size:")
        lot_label.setStyleSheet("color: #94a3b8; font-size: 13px;")
        row1.addWidget(lot_label)

        self._lot_spinbox = QDoubleSpinBox()
        self._lot_spinbox.setRange(0.01, 99.99)
        self._lot_spinbox.setSingleStep(0.01)
        self._lot_spinbox.setDecimals(2)
        self._lot_spinbox.setValue(0.50)
        self._lot_spinbox.setFixedWidth(90)
        self._lot_spinbox.setStyleSheet(
            "QDoubleSpinBox { background: #1e293b; color: #60a5fa;"
            "  font-weight: bold; font-size: 14px;"
            "  border: 1px solid #334155; border-radius: 6px; padding: 4px 8px; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button"
            "  { width: 18px; background: #334155; border-radius: 3px; }"
        )
        self._lot_spinbox.valueChanged.connect(self._on_lot_changed)
        row1.addWidget(self._lot_spinbox)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color: #334155;")
        row1.addWidget(sep2)

        # Reverse mode checkbox
        self._reverse_checkbox = QCheckBox("Reverse Mode")
        self._reverse_checkbox.setStyleSheet(
            "QCheckBox { color: #94a3b8; font-size: 13px; spacing: 6px; }"
            "QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px;"
            "  border: 2px solid #475569; background: #1e293b; }"
            "QCheckBox::indicator:checked { background: #dc2626; border-color: #ef4444; }"
        )
        self._reverse_checkbox.toggled.connect(
            lambda checked: self.reverse_mode_changed.emit(checked)
        )
        row1.addWidget(self._reverse_checkbox)

        row1.addStretch()
        controls_layout.addLayout(row1)

        # --- Controls row 2: Close All, Sync Now ---
        row2 = QHBoxLayout()

        self._close_all_btn = QPushButton("Close All XTB")
        self._close_all_btn.setFixedSize(140, 34)
        self._close_all_btn.setEnabled(False)
        self._close_all_btn.setStyleSheet(
            "QPushButton { background-color: #991b1b; color: white;"
            "  font-weight: bold; font-size: 12px; border-radius: 6px; }"
            "QPushButton:hover { background-color: #dc2626; }"
            "QPushButton:disabled { background-color: #475569; color: #94a3b8; }"
        )
        self._close_all_btn.clicked.connect(self._on_close_all)
        row2.addWidget(self._close_all_btn)

        self._sync_btn = QPushButton("Sync Now")
        self._sync_btn.setFixedSize(100, 34)
        self._sync_btn.setEnabled(False)
        self._sync_btn.setStyleSheet(
            "QPushButton { background-color: #1d4ed8; color: white;"
            "  font-weight: bold; font-size: 12px; border-radius: 6px; }"
            "QPushButton:hover { background-color: #2563eb; }"
            "QPushButton:disabled { background-color: #475569; color: #94a3b8; }"
        )
        self._sync_btn.clicked.connect(self._on_sync_now)
        row2.addWidget(self._sync_btn)

        row2.addStretch()
        controls_layout.addLayout(row2)

        layout.addWidget(controls)

        # --- Splitter: trade table (top) + log (bottom) ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.trade_table = TradeTable()
        splitter.addWidget(self.trade_table)

        self.log_widget = LogWidget()
        splitter.addWidget(self.log_widget)

        splitter.setSizes([350, 250])
        layout.addWidget(splitter)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            "QMainWindow { background-color: #0f172a; }"
            "QWidget { background-color: #0f172a; color: #e2e8f0; }"
            "QPushButton { border-radius: 8px; font-weight: bold; font-size: 14px; }"
            "QSplitter::handle { background: #334155; height: 2px; }"
        )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_start_stop(self) -> None:
        if self._bridge_running:
            self.stop_requested.emit()
            self._bridge_running = False
            self._bridge_paused = False
            self._set_operational_buttons_enabled(False)
        else:
            self.start_requested.emit()
            self._bridge_running = True
            self._bridge_paused = False
            self._set_operational_buttons_enabled(True)
        self._update_start_stop_style()
        self._update_pause_style()
        self._update_bridge_state_label()

    def _on_pause(self) -> None:
        self.pause_requested.emit()
        # Actual state will be updated via set_paused() signal from worker

    def _on_lot_changed(self, value: float) -> None:
        self.lot_ratio_changed.emit(value)

    def _on_close_all(self) -> None:
        reply = QMessageBox.question(
            self,
            "Close All XTB Positions",
            "Are you sure you want to close ALL mirrored positions in XTB?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.close_all_requested.emit()

    def _on_sync_now(self) -> None:
        self.force_sync_requested.emit()

    @pyqtSlot(bool)
    def set_mt5_status(self, connected: bool) -> None:
        self._mt5_indicator.set_status(connected)

    @pyqtSlot(bool)
    def set_xtb_status(self, connected: bool) -> None:
        self._xtb_indicator.set_status(connected)

    @pyqtSlot(bool)
    def set_paused(self, paused: bool) -> None:
        self._bridge_paused = paused
        self._update_pause_style()
        self._update_bridge_state_label()

    @pyqtSlot(str)
    def on_bridge_error(self, error: str) -> None:
        self._bridge_running = False
        self._bridge_paused = False
        self._update_start_stop_style()
        self._update_pause_style()
        self._set_operational_buttons_enabled(False)
        self._update_bridge_state_label()

    def set_lot_ratio(self, ratio: float) -> None:
        self._lot_spinbox.setValue(ratio)

    def set_reverse_mode(self, enabled: bool) -> None:
        self._reverse_checkbox.setChecked(enabled)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_operational_buttons_enabled(self, enabled: bool) -> None:
        """Enable/disable buttons that only make sense when bridge is running."""
        self._pause_btn.setEnabled(enabled)
        self._close_all_btn.setEnabled(enabled)
        self._sync_btn.setEnabled(enabled)

    def _update_start_stop_style(self) -> None:
        if self._bridge_running:
            self._start_stop_btn.setText("STOP")
            self._start_stop_btn.setStyleSheet(
                "QPushButton { background-color: #dc2626; color: white; }"
                "QPushButton:hover { background-color: #ef4444; }"
            )
        else:
            self._start_stop_btn.setText("START")
            self._start_stop_btn.setStyleSheet(
                "QPushButton { background-color: #16a34a; color: white; }"
                "QPushButton:hover { background-color: #22c55e; }"
            )

    def _update_pause_style(self) -> None:
        if self._bridge_paused:
            self._pause_btn.setText("RESUME")
            self._pause_btn.setStyleSheet(
                "QPushButton { background-color: #2563eb; color: white; }"
                "QPushButton:hover { background-color: #3b82f6; }"
            )
        else:
            self._pause_btn.setText("PAUSE")
            self._pause_btn.setStyleSheet(
                "QPushButton { background-color: #d97706; color: white; }"
                "QPushButton:hover { background-color: #f59e0b; }"
                "QPushButton:disabled { background-color: #475569; color: #94a3b8; }"
            )

    def _update_bridge_state_label(self) -> None:
        if not self._bridge_running:
            text = "  Bridge: Stopped"
            style = (
                "QLabel { color: #94a3b8; font-weight: bold; font-size: 13px;"
                "  padding: 4px 12px; background: #47556922;"
                "  border: 1px solid #47556944; border-radius: 6px; }"
            )
        elif self._bridge_paused:
            text = "  Bridge: Paused"
            style = (
                "QLabel { color: #fbbf24; font-weight: bold; font-size: 13px;"
                "  padding: 4px 12px; background: #d9770622;"
                "  border: 1px solid #d9770644; border-radius: 6px; }"
            )
        else:
            text = "  Bridge: Running"
            style = (
                "QLabel { color: #4ade80; font-weight: bold; font-size: 13px;"
                "  padding: 4px 12px; background: #16a34a22;"
                "  border: 1px solid #16a34a44; border-radius: 6px; }"
            )
        self._bridge_state_label.setText(text)
        self._bridge_state_label.setStyleSheet(style)

    @pyqtSlot(list)
    def _on_positions_updated(self, positions: list[dict]) -> None:
        self._active_positions = positions
        mirrored = sum(
            1 for p in positions
            if p.get("status") in ("SYNCED", "ASSUMED") and not p.get("ignored")
        )
        state = "RUNNING" if self._bridge_running and not self._bridge_paused else \
                "PAUSED" if self._bridge_paused else "STOPPED"
        suffix = f" ({mirrored} mirrored)" if mirrored and self._bridge_running else ""
        self.setWindowTitle(f"XTB Bridge — {state}{suffix}")

    def closeEvent(self, event: QCloseEvent) -> None:
        mirrored = [
            p for p in self._active_positions
            if p.get("status") in ("SYNCED", "ASSUMED") and not p.get("ignored")
        ]
        if self._bridge_running and mirrored:
            symbols = ", ".join(sorted({p["symbol_xtb"] for p in mirrored}))
            reply = QMessageBox.question(
                self,
                "Active positions — confirm exit",
                f"There are {len(mirrored)} mirrored position(s) open in XTB:\n{symbols}\n\n"
                "Closing the bridge will NOT close them in XTB.\nExit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        if self._bridge_running:
            self.stop_requested.emit()
        event.accept()
