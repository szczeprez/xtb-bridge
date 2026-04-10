# XTB Bridge

Mirror trading bridge that automatically copies trades from **MetaTrader 5** (Stratus Bora) to **xStation5 Web** (XTB) in real-time.

When a position opens or closes in MT5, the same action is executed in XTB with a configurable lot multiplier.

## How It Works

```
MT5 Terminal              Bridge App (PyQt6)           xStation5 Web (Browser)
+-----------------+  poll  +------------------+  click  +------------------+
| positions_get() |------->| Diff engine      |-------->| BUY / SELL       |
| EURUSD BUY 0.10 |       | Lot ratio x0.5   |        | EURUSD BUY 0.05  |
+-----------------+       +------------------+        +------------------+
```

- **Layer 1 (MT5 Reader):** Polls MT5 every 500ms via the official `MetaTrader5` Python package
- **Layer 2 (Bridge Logic):** Detects new/closed positions, applies lot multiplier, maps symbols
- **Layer 3 (XTB Execution):** Automates xStation5 Web via Playwright (visible browser window)

## Prerequisites

- **Windows 10/11** (required by MetaTrader5 Python package)
- **Python 3.11+** (tested with 3.14)
- **MetaTrader 5 terminal** installed and running on the same PC
- **XTB account** (demo or real) with xStation5 Web access

## Installation

### 1. Clone or download the project

```bash
cd /c/Projekty/xtb-bridge
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/Scripts/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browser

```bash
playwright install chromium
```

### 5. Create your config file

Copy the example config and fill in your credentials:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` with your settings:

```toml
[mt5]
# Optional — leave commented to auto-detect
# terminal_path = "C:\\Program Files\\MetaTrader 5\\terminal64.exe"

[xtb]
email = "your_xtb_email@example.com"
password = "your_xtb_password"
account_type = "demo"   # use "demo" for testing, "real" for live

[bridge]
pairs = ["EURUSD", "GBPUSD", "GOLD"]
lot_ratio = 0.5          # MT5 0.10 lots -> XTB 0.05 lots
poll_interval_ms = 500   # check MT5 every 500ms
reverse_mode = false     # true = BUY in MT5 becomes SELL in XTB

[symbols]
# MT5 symbol name = XTB symbol name
EURUSD = "EURUSD"
GBPUSD = "GBPUSD"
GOLD = "XAUUSD"
```

## Running the Application

### 1. Start MetaTrader 5

Open the MT5 terminal and log in to your Stratus Bora account. The terminal must be running before starting the bridge.

### 2. Activate the virtual environment

```bash
cd /c/Projekty/xtb-bridge
source .venv/Scripts/activate
```

### 3. Launch XTB Bridge

```bash
python -m xtb_bridge.main
```

### 4. Using the GUI

1. The application window opens with connection status indicators (MT5 / XTB)
2. Click **START** to begin the bridge
3. A Chromium browser window opens and navigates to xStation5 Web
4. **Log in manually** if CAPTCHA or 2FA is prompted (the app fills credentials automatically, but you may need to handle security challenges in the browser window)
5. Once logged in, the bridge begins monitoring MT5 positions
6. Open a trade in MT5 — it will be mirrored to XTB within 1-3 seconds
7. Close a trade in MT5 — the corresponding XTB position closes automatically
8. Use the **lot multiplier slider** (0.1x - 3.0x) to scale position sizes
9. Click **STOP** to pause the bridge

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `mt5.terminal_path` | auto-detect | Path to MT5 terminal executable |
| `xtb.email` | *(required)* | Your XTB account email |
| `xtb.password` | *(required)* | Your XTB account password |
| `xtb.account_type` | `demo` | `"demo"` or `"real"` |
| `bridge.pairs` | `["EURUSD", "GBPUSD", "GOLD"]` | MT5 symbols to monitor |
| `bridge.lot_ratio` | `0.5` | Multiplier: MT5 lots x ratio = XTB lots |
| `bridge.poll_interval_ms` | `500` | How often to check MT5 (milliseconds) |
| `bridge.reverse_mode` | `false` | Flip direction: BUY becomes SELL |
| `symbols.*` | — | Maps MT5 symbol names to XTB symbol names |

## Project Structure

```
xtb-bridge/
├── xtb_bridge/
│   ├── __init__.py          # Package version
│   ├── main.py              # Entry point — wires config, GUI, and bridge thread
│   ├── models.py            # Shared data structures (Position, Direction, etc.)
│   ├── config.py            # Loads config.toml, validates settings
│   ├── mt5_reader.py        # MT5 connection and position polling
│   ├── xtb_web.py           # Playwright automation of xStation5 Web
│   ├── bridge.py            # Diff engine — detects changes, mirrors trades
│   └── gui/
│       ├── __init__.py
│       ├── main_window.py   # Main application window
│       ├── trade_table.py   # Positions table widget
│       └── log_widget.py    # Color-coded event log widget
├── config.example.toml      # Template config (safe to commit)
├── config.toml              # Your config with credentials (gitignored)
├── requirements.txt         # Python dependencies
├── mapping.json             # Auto-generated: tracks MT5↔XTB position mapping
├── xtb_bridge.log           # Auto-generated: application log file
└── .gitignore
```

## Troubleshooting

### "MT5 connection failed"
- Make sure MetaTrader 5 terminal is open and logged in
- If you have multiple MT5 installations, set `mt5.terminal_path` in config.toml

### "XTB login failed"
- Check your email and password in config.toml
- Look at the browser window — you may need to solve a CAPTCHA or approve 2FA
- The login timeout is 2 minutes, giving you time to handle manual steps

### Selectors not working (trade not executing in XTB)
- xStation5 Web UI may change over time, breaking CSS selectors
- Check `screenshots/` folder for error screenshots
- Open xStation5 in DevTools (F12) to inspect current selectors
- Update selectors in `xtb_bridge/xtb_web.py`

### Bridge stops unexpectedly
- Check `xtb_bridge.log` for error details
- The bridge auto-retries with exponential backoff (1s, 2s, 4s... up to 30s)
- If MT5 terminal was closed, reopen it and restart the bridge

### Position not mirrored
- Verify the symbol is in `bridge.pairs` config
- Verify the symbol mapping exists in `[symbols]` section
- Check that `lot_ratio * mt5_lots >= 0.01` (XTB minimum lot size)

## Important Warnings

- **Test on demo accounts first.** Do not use real money until the bridge runs stable for at least 5 business days without errors.
- **Latency:** Browser automation adds 1-3 seconds per trade. This is acceptable for swing/copy trading, not for scalping.
- **Do not close positions manually in XTB** while the bridge is running — the bridge tracks position mappings and manual intervention can cause state desync.
- **Keep the browser window open** — the bridge needs it to execute trades.
- `config.toml` contains your password in plaintext. Do not share it or commit it to git.

## Tech Stack

- **Python 3.11+** — application language
- **PyQt6** — desktop GUI framework
- **MetaTrader5** — official Python package for MT5 terminal communication
- **Playwright** — browser automation for xStation5 Web
- **tomllib** — config file parsing (Python stdlib)
