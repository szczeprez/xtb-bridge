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
| `bridge.lot_ratio` | `0.5` | Default lot size for all symbols |
| `bridge.poll_interval_ms` | `500` | How often to check MT5 (milliseconds) |
| `bridge.reverse_mode` | `false` | Flip direction: BUY becomes SELL |
| `symbols.*` | — | Maps MT5 symbol names to XTB symbol names |
| `lots.*` | *(optional)* | Per-symbol lot size override (see below) |

### Per-symbol lot sizes

Add a `[lots]` section to override `lot_ratio` for specific instruments:

```toml
[bridge]
lot_ratio = 0.04   # default for all symbols

[lots]
EURUSD = 0.02      # override for EURUSD only
GBPUSD = 0.01      # override for GBPUSD only
# symbols not listed here use the lot_ratio default
```

Changes to `lot_ratio` made in the GUI are saved back to `config.toml` automatically.

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
├── run.py                   # Launcher used by the packaged .exe
├── xtb_bridge.spec          # PyInstaller build spec
├── build.py                 # Build script — produces dist/xtb_bridge/
├── config.toml              # Your config with credentials (gitignored)
├── requirements.txt         # Python dependencies
├── mapping.json             # Auto-generated: MT5↔XTB position mapping
├── position_ids.json        # Auto-generated: exact XTB position IDs per ticket
├── xtb_bridge.log           # Auto-generated: rotating log (max 5 MB × 3 backups)
└── .gitignore
```

## Building a Standalone Windows Executable

The `build.py` script packages the application into a self-contained folder that can be copied to any Windows machine — no Python installation required on the target.

### Prerequisites (build machine only)

- Python 3.11+ with the project venv active
- Playwright Chromium already installed (`playwright install chromium`)
- Internet access to download PyInstaller on first run

### Run the build

```bash
# activate the venv first
.venv\Scripts\activate

python build.py
```

The script will:

1. Install **PyInstaller ≥ 6.12** if not present
2. Bundle Python, all dependencies, and the Playwright Node.js driver via `xtb_bridge.spec`
3. Copy the local **Chromium** browser (~393 MB) into `dist/xtb_bridge/_playwright_browsers/`
4. Copy `config.toml` and write a `start.bat` launcher
5. Produce a zip archive at `dist/xtb_bridge_windows.zip`

To skip the zip step:

```bash
python build.py --no-zip
```

### Distribution layout

```
dist/xtb_bridge/
├── xtb_bridge.exe              ← double-click to launch (or use start.bat)
├── start.bat                   ← alternative launcher
├── config.toml                 ← edit with XTB credentials before running
├── README.txt                  ← end-user instructions
├── _internal/                  ← Python runtime + all libraries (~249 MB)
│   └── playwright/driver/      ← Node.js Playwright driver
└── _playwright_browsers/       ← Chromium browser (~393 MB)
    └── chromium-XXXX/
```

Total size: **~650 MB** uncompressed, **~400 MB** zipped.

### Installing on the target machine

1. Copy and unzip `xtb_bridge_windows.zip` to any folder
2. Edit `config.toml` with the XTB account credentials
3. Start MetaTrader 5 and log in
4. Double-click `xtb_bridge.exe`

**Windows SmartScreen** will show a warning on the first launch because the exe is unsigned. Click **"More info" → "Run anyway"** to proceed.

> The target machine must have **MetaTrader 5 terminal** installed — the bridge communicates with it via a local socket (`MetaTrader5` Python package requirement).

### Rebuilding after code changes

```bash
python build.py
```

PyInstaller detects changed files automatically. The Chromium copy step is skipped if `_playwright_browsers/` already exists in `dist/xtb_bridge/`.

### Updating the Chromium version

If you run `playwright install chromium` and get a newer version, delete the old browsers folder before rebuilding:

```bash
Remove-Item -Recurse -Force dist\xtb_bridge\_playwright_browsers
python build.py
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
