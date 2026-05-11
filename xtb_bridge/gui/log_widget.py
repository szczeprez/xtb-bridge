from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import QColor, QTextCharFormat
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

MAX_LINES = 2000

_FILTER_ALL = "All"
_FILTER_ERRORS = "Errors / Warnings"
_FILTER_ACTIONS = "Opens / Closes"


class LogWidget(QWidget):
    """Scrolling event log with color-coding, filter, and clear."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages: list[tuple[str, str, str]] = []  # (timestamp, text, color)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        filter_label = QLabel("Filter:")
        filter_label.setStyleSheet("color: #64748b; font-size: 12px;")
        toolbar.addWidget(filter_label)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems([_FILTER_ALL, _FILTER_ERRORS, _FILTER_ACTIONS])
        self._filter_combo.setFixedWidth(160)
        self._filter_combo.setStyleSheet(
            "QComboBox { background: #1e293b; color: #94a3b8; border: 1px solid #334155;"
            "  border-radius: 4px; padding: 2px 6px; font-size: 12px; }"
            "QComboBox::drop-down { border: none; }"
            "QComboBox QAbstractItemView { background: #1e293b; color: #94a3b8;"
            "  selection-background-color: #334155; }"
        )
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._filter_combo)

        toolbar.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedSize(60, 24)
        clear_btn.setStyleSheet(
            "QPushButton { background: #334155; color: #94a3b8; border: none;"
            "  border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background: #475569; color: #e2e8f0; }"
        )
        clear_btn.clicked.connect(self._on_clear)
        toolbar.addWidget(clear_btn)

        layout.addLayout(toolbar)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet(
            "QTextEdit {"
            "  background-color: #0a0f1a;"
            "  color: #cbd5e1;"
            "  font-family: 'Cascadia Code', 'Consolas', monospace;"
            "  font-size: 12px;"
            "  border: 1px solid #334155;"
            "  border-radius: 6px;"
            "  padding: 8px;"
            "}"
        )
        layout.addWidget(self._text)

    @pyqtSlot(str)
    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        color = self._color_for_message(message)
        self._messages.append((timestamp, message, color))

        if len(self._messages) > MAX_LINES:
            self._messages = self._messages[-MAX_LINES:]

        if self._passes_filter(message):
            self._append_to_display(timestamp, message, color)

    def _passes_filter(self, message: str) -> bool:
        f = self._filter_combo.currentText()
        if f == _FILTER_ALL:
            return True
        upper = message.upper()
        if f == _FILTER_ERRORS:
            return "ERROR" in upper or "FAIL" in upper or "SKIP" in upper or "WARN" in upper
        if f == _FILTER_ACTIONS:
            return "OPEN" in upper or "CLOSE" in upper or "RETRY" in upper
        return True

    def _append_to_display(self, timestamp: str, message: str, color: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self._text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(f"[{timestamp}] ", QTextCharFormat())
        cursor.insertText(f"{message}\n", fmt)
        self._text.verticalScrollBar().setValue(self._text.verticalScrollBar().maximum())

    def _on_filter_changed(self, _: str) -> None:
        self._text.clear()
        for timestamp, message, color in self._messages:
            if self._passes_filter(message):
                self._append_to_display(timestamp, message, color)

    def _on_clear(self) -> None:
        self._messages.clear()
        self._text.clear()

    def _color_for_message(self, message: str) -> str:
        upper = message.upper()
        if "OPEN" in upper and "FAIL" not in upper:
            return "#4ade80"
        if "CLOSE" in upper and "FAIL" not in upper:
            return "#f87171"
        if "ERROR" in upper or "FAIL" in upper:
            return "#fbbf24"
        if "SKIP" in upper or "WARN" in upper:
            return "#fbbf24"
        return "#94a3b8"
