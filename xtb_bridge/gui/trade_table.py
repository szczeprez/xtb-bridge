from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

COLUMNS = [
    "MT5 Ticket",
    "Symbol (MT5)",
    "Symbol (XTB)",
    "Dir MT5",
    "Dir XTB",
    "Lots MT5",
    "Lots XTB",
    "Open Price",
    "Opened",
    "P&L",
    "Status",
    "Actions",
]

BTN_STYLE_CLOSE = (
    "QPushButton {"
    "  background-color: #dc2626;"
    "  color: white;"
    "  font-weight: bold;"
    "  font-size: 11px;"
    "  border: none;"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "}"
    "QPushButton:hover { background-color: #ef4444; }"
    "QPushButton:disabled { background-color: #475569; color: #94a3b8; }"
)

BTN_STYLE_REOPEN = (
    "QPushButton {"
    "  background-color: #2563eb;"
    "  color: white;"
    "  font-weight: bold;"
    "  font-size: 11px;"
    "  border: none;"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "}"
    "QPushButton:hover { background-color: #3b82f6; }"
)

BTN_STYLE_IGNORE = (
    "QPushButton {"
    "  background-color: #d97706;"
    "  color: white;"
    "  font-weight: bold;"
    "  font-size: 11px;"
    "  border: none;"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "}"
    "QPushButton:hover { background-color: #f59e0b; }"
)

BTN_STYLE_UNIGNORE = (
    "QPushButton {"
    "  background-color: #059669;"
    "  color: white;"
    "  font-weight: bold;"
    "  font-size: 11px;"
    "  border: none;"
    "  border-radius: 4px;"
    "  padding: 3px 8px;"
    "}"
    "QPushButton:hover { background-color: #10b981; }"
)

COLOR_BUY = QColor("#4ade80")
COLOR_SELL = QColor("#f87171")
COLOR_PROFIT = QColor("#4ade80")
COLOR_LOSS = QColor("#f87171")
COLOR_ZERO = QColor("#94a3b8")

STATUS_COLORS = {
    "SYNCED": QColor("#4ade80"),
    "ASSUMED": QColor("#a3e635"),
    "PENDING": QColor("#60a5fa"),
    "CLOSED_XTB": QColor("#f87171"),
    "IGNORED": QColor("#fbbf24"),
}

_COL = {name: i for i, name in enumerate(COLUMNS)}


class TradeTable(QTableWidget):
    """Table displaying mirrored positions with status and action buttons."""

    close_requested = pyqtSignal(str, str, int)
    reopen_requested = pyqtSignal(int)
    ignore_requested = pyqtSignal(int)
    unignore_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setSortingEnabled(True)

        header = self.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for i in range(1, len(COLUMNS) - 1):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(len(COLUMNS) - 1, 200)

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
        self.setSortingEnabled(False)
        self.setRowCount(len(positions))

        for row, pos in enumerate(positions):
            mt5_ticket = int(pos["mt5_ticket"])
            symbol_xtb = pos["symbol_xtb"]
            direction_xtb = pos["direction_xtb"]
            status = pos.get("status", "PENDING")
            is_ignored = pos.get("ignored", False)
            profit = pos.get("profit", 0.0)
            open_price = pos.get("open_price", 0.0)
            open_time = pos.get("open_time", 0)

            self._set_cell(row, _COL["MT5 Ticket"], str(mt5_ticket))
            self._set_cell(row, _COL["Symbol (MT5)"], pos["symbol_mt5"])
            self._set_cell(row, _COL["Symbol (XTB)"], symbol_xtb)
            self._set_cell(row, _COL["Dir MT5"], pos["direction_mt5"],
                           COLOR_BUY if pos["direction_mt5"] == "BUY" else COLOR_SELL)
            self._set_cell(row, _COL["Dir XTB"], direction_xtb,
                           COLOR_BUY if direction_xtb == "BUY" else COLOR_SELL)
            self._set_cell(row, _COL["Lots MT5"], f"{pos['volume_mt5']:.2f}")
            self._set_cell(row, _COL["Lots XTB"], f"{pos['volume_xtb']:.2f}")

            # Open price
            price_str = f"{open_price:.5f}" if open_price else "—"
            self._set_cell(row, _COL["Open Price"], price_str)

            # Opened time
            if open_time:
                time_str = datetime.fromtimestamp(open_time).strftime("%H:%M:%S")
            else:
                time_str = "—"
            self._set_cell(row, _COL["Opened"], time_str)

            # P&L
            if profit > 0.001:
                pnl_str = f"+{profit:.2f}"
                pnl_color = COLOR_PROFIT
            elif profit < -0.001:
                pnl_str = f"{profit:.2f}"
                pnl_color = COLOR_LOSS
            else:
                pnl_str = "0.00"
                pnl_color = COLOR_ZERO
            self._set_cell(row, _COL["P&L"], pnl_str, pnl_color)

            # Status column
            status_color = STATUS_COLORS.get(status, QColor("#94a3b8"))
            status_labels = {
                "SYNCED": "Synced",
                "ASSUMED": "Assumed",
                "PENDING": "Pending",
                "CLOSED_XTB": "Closed (XTB)",
                "IGNORED": "Ignored",
            }
            self._set_cell(row, _COL["Status"], status_labels.get(status, status), status_color)

            # Actions column
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(4, 2, 4, 2)
            actions_layout.setSpacing(4)

            if is_ignored:
                btn_unignore = QPushButton("Unignore")
                btn_unignore.setStyleSheet(BTN_STYLE_UNIGNORE)
                btn_unignore.clicked.connect(
                    lambda _=False, t=mt5_ticket: self.unignore_requested.emit(t)
                )
                actions_layout.addWidget(btn_unignore)
            elif status == "CLOSED_XTB":
                btn_reopen = QPushButton("Reopen")
                btn_reopen.setStyleSheet(BTN_STYLE_REOPEN)
                btn_reopen.clicked.connect(
                    lambda _=False, t=mt5_ticket: self.reopen_requested.emit(t)
                )
                actions_layout.addWidget(btn_reopen)

                btn_ignore = QPushButton("Ignore")
                btn_ignore.setStyleSheet(BTN_STYLE_IGNORE)
                btn_ignore.clicked.connect(
                    lambda _=False, t=mt5_ticket: self.ignore_requested.emit(t)
                )
                actions_layout.addWidget(btn_ignore)
            elif status in ("SYNCED", "ASSUMED"):
                btn_close = QPushButton("Close XTB")
                btn_close.setStyleSheet(BTN_STYLE_CLOSE)
                btn_close.clicked.connect(
                    lambda _=False, s=symbol_xtb, d=direction_xtb, t=mt5_ticket:
                    self.close_requested.emit(s, d, t)
                )
                actions_layout.addWidget(btn_close)

                btn_ignore = QPushButton("Ignore")
                btn_ignore.setStyleSheet(BTN_STYLE_IGNORE)
                btn_ignore.clicked.connect(
                    lambda _=False, t=mt5_ticket: self.ignore_requested.emit(t)
                )
                actions_layout.addWidget(btn_ignore)
            else:
                btn_ignore = QPushButton("Ignore")
                btn_ignore.setStyleSheet(BTN_STYLE_IGNORE)
                btn_ignore.clicked.connect(
                    lambda _=False, t=mt5_ticket: self.ignore_requested.emit(t)
                )
                actions_layout.addWidget(btn_ignore)

            actions_layout.addStretch()
            self.setCellWidget(row, _COL["Actions"], actions)

        self.setSortingEnabled(True)
        self.resizeColumnsToContents()
        self.setColumnWidth(_COL["Actions"], 200)

    def _set_cell(self, row: int, col: int, text: str,
                  color: QColor | None = None) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if color:
            item.setForeground(color)
        self.setItem(row, col, item)
