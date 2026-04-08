from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem

COLUMNS = [
    "Symbol (MT5)",
    "Symbol (XTB)",
    "Dir MT5",
    "Dir XTB",
    "Lots MT5",
    "Lots XTB",
    "Mirrored",
]

COLOR_BUY = QColor("#4ade80")
COLOR_SELL = QColor("#f87171")
COLOR_YES = QColor("#4ade80")
COLOR_NO = QColor("#64748b")


class TradeTable(QTableWidget):
    """Table displaying mirrored positions."""

    def __init__(self, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)

        header = self.horizontalHeader()
        header.setStretchLastSection(True)
        for i in range(len(COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)

        self.setStyleSheet(
            "QTableWidget {"
            "  background-color: #1e293b;"
            "  color: #cbd5e1;"
            "  gridline-color: #334155;"
            "  border: 1px solid #334155;"
            "  border-radius: 6px;"
            "  font-size: 13px;"
            "}"
            "QTableWidget::item { padding: 6px; }"
            "QTableWidget::item:alternate { background-color: #0f172a; }"
            "QHeaderView::section {"
            "  background-color: #0f172a;"
            "  color: #64748b;"
            "  font-weight: bold;"
            "  font-size: 11px;"
            "  text-transform: uppercase;"
            "  padding: 8px;"
            "  border: none;"
            "  border-bottom: 1px solid #334155;"
            "}"
        )

    @pyqtSlot(list)
    def update_positions(self, positions: list[dict]) -> None:
        self.setRowCount(len(positions))

        for row, pos in enumerate(positions):
            self._set_cell(row, 0, pos["symbol_mt5"])
            self._set_cell(row, 1, pos["symbol_xtb"])
            self._set_cell(row, 2, pos["direction_mt5"],
                           COLOR_BUY if pos["direction_mt5"] == "BUY" else COLOR_SELL)
            self._set_cell(row, 3, pos["direction_xtb"],
                           COLOR_BUY if pos["direction_xtb"] == "BUY" else COLOR_SELL)
            self._set_cell(row, 4, f"{pos['volume_mt5']:.2f}")
            self._set_cell(row, 5, f"{pos['volume_xtb']:.2f}")
            mirrored = pos["mirrored"]
            self._set_cell(row, 6, "YES" if mirrored else "NO",
                           COLOR_YES if mirrored else COLOR_NO)

    def _set_cell(self, row: int, col: int, text: str,
                  color: QColor | None = None) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if color:
            item.setForeground(color)
        self.setItem(row, col, item)
