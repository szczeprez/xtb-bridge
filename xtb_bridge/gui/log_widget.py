from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import QColor, QTextCharFormat
from PyQt6.QtWidgets import QTextEdit

MAX_LINES = 1000


class LogWidget(QTextEdit):
    """Scrolling event log with color-coded entries."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setStyleSheet(
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
        self._line_count = 0

    @pyqtSlot(str)
    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        color = self._color_for_message(message)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))

        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(f"[{timestamp}] ", QTextCharFormat())
        cursor.insertText(f"{message}\n", fmt)

        self._line_count += 1
        if self._line_count > MAX_LINES:
            self._trim_lines()

        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _color_for_message(self, message: str) -> str:
        upper = message.upper()
        if "OPEN" in upper and "FAIL" not in upper:
            return "#4ade80"  # green
        if "CLOSE" in upper and "FAIL" not in upper:
            return "#f87171"  # red
        if "ERROR" in upper or "FAIL" in upper:
            return "#fbbf24"  # amber
        if "SKIP" in upper or "WARN" in upper:
            return "#fbbf24"  # amber
        return "#94a3b8"  # gray

    def _trim_lines(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        for _ in range(self._line_count - MAX_LINES):
            cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._line_count = MAX_LINES
