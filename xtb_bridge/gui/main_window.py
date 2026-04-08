from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("XTB Bridge — MT5 Mirror Trading")
        self.setMinimumSize(900, 650)
        self._bridge_running = False
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
        status_row.addWidget(self._mt5_indicator)
        status_row.addWidget(self._xtb_indicator)
        status_row.addStretch()
        layout.addLayout(status_row)

        # --- Controls ---
        controls = QGroupBox("Controls")
        controls.setStyleSheet(
            "QGroupBox { color: #94a3b8; font-weight: bold; font-size: 13px;"
            "  border: 1px solid #334155; border-radius: 8px; padding: 16px; padding-top: 24px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; }"
        )
        controls_layout = QHBoxLayout(controls)

        # START/STOP button
        self._start_stop_btn = QPushButton("START")
        self._start_stop_btn.setFixedSize(120, 40)
        self._start_stop_btn.clicked.connect(self._on_start_stop)
        self._update_start_stop_style()
        controls_layout.addWidget(self._start_stop_btn)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #334155;")
        controls_layout.addWidget(sep)

        # Lot multiplier slider
        lot_label = QLabel("Lot Multiplier:")
        lot_label.setStyleSheet("color: #94a3b8; font-size: 13px;")
        controls_layout.addWidget(lot_label)

        self._lot_slider = QSlider(Qt.Orientation.Horizontal)
        self._lot_slider.setRange(1, 30)  # 0.1x to 3.0x
        self._lot_slider.setValue(5)  # default 0.5x
        self._lot_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._lot_slider.setTickInterval(5)
        self._lot_slider.setFixedWidth(200)
        self._lot_slider.valueChanged.connect(self._on_lot_changed)
        controls_layout.addWidget(self._lot_slider)

        self._lot_value_label = QLabel("0.50x")
        self._lot_value_label.setFixedWidth(50)
        self._lot_value_label.setStyleSheet(
            "color: #60a5fa; font-weight: bold; font-size: 14px;"
        )
        controls_layout.addWidget(self._lot_value_label)

        controls_layout.addStretch()
        layout.addWidget(controls)

        # --- Splitter: trade table (top) + log (bottom) ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.trade_table = TradeTable()
        splitter.addWidget(self.trade_table)

        self.log_widget = LogWidget()
        splitter.addWidget(self.log_widget)

        splitter.setSizes([300, 250])
        layout.addWidget(splitter)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            "QMainWindow { background-color: #0f172a; }"
            "QWidget { background-color: #0f172a; color: #e2e8f0; }"
            "QPushButton { border-radius: 8px; font-weight: bold; font-size: 14px; }"
            "QSlider::groove:horizontal {"
            "  background: #334155; height: 6px; border-radius: 3px; }"
            "QSlider::handle:horizontal {"
            "  background: #60a5fa; width: 16px; height: 16px;"
            "  margin: -5px 0; border-radius: 8px; }"
            "QSlider::sub-page:horizontal { background: #1d4ed8; border-radius: 3px; }"
            "QSplitter::handle { background: #334155; height: 2px; }"
        )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_start_stop(self) -> None:
        if self._bridge_running:
            self.stop_requested.emit()
            self._bridge_running = False
        else:
            self.start_requested.emit()
            self._bridge_running = True
        self._update_start_stop_style()

    def _on_lot_changed(self, value: int) -> None:
        ratio = value / 10.0
        self._lot_value_label.setText(f"{ratio:.1f}x")
        self.lot_ratio_changed.emit(ratio)

    @pyqtSlot(bool)
    def set_mt5_status(self, connected: bool) -> None:
        self._mt5_indicator.set_status(connected)

    @pyqtSlot(bool)
    def set_xtb_status(self, connected: bool) -> None:
        self._xtb_indicator.set_status(connected)

    @pyqtSlot(str)
    def on_bridge_error(self, error: str) -> None:
        self._bridge_running = False
        self._update_start_stop_style()

    def set_lot_ratio(self, ratio: float) -> None:
        self._lot_slider.setValue(int(ratio * 10))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._bridge_running:
            self.stop_requested.emit()
        event.accept()
