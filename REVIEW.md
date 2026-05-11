# XTB Bridge — Code Review

**Reviewed:** 2026-04-30  
**Depth:** deep (full cross-file analysis)  
**Files Reviewed:** 8 source files  
**Status:** issues_found

---

## Files Reviewed

- `xtb_bridge/main.py`
- `xtb_bridge/bridge.py`
- `xtb_bridge/models.py`
- `xtb_bridge/config.py`
- `xtb_bridge/mt5_reader.py`
- `xtb_bridge/xtb_web.py`
- `xtb_bridge/gui/main_window.py`
- `xtb_bridge/gui/trade_table.py`
- `xtb_bridge/gui/log_widget.py`

---

## Summary

The bridge is a well-structured PyQt6 desktop application that polls MT5 and mirrors positions to XTB via Playwright browser automation. The core logic is solid — the diff engine, retry queues, and GUI-thread-safety design are correct in the large. However there are several issues ranging from a critical safety violation (SL/TP data being present in the `Position` model and potentially leaking into XTB via trade verification), a real security leak (credentials committed to disk), and a number of correctness bugs in the Playwright layer that could silently misfire trades.

---

## CRITICAL

---

### CR-01: SL/TP fields present in Position model — risk of future leakage into XTB trades

**File:** `xtb_bridge/models.py:39-41`, `xtb_bridge/mt5_reader.py:53-58`

**Issue:** The `Position` dataclass carries `sl` and `tp` fields populated directly from `p.sl` and `p.tp` on every MT5 position. The design requirement (noted in memory context) is that XTB trades must NEVER be opened with Stop Loss or Take Profit — only symbol/direction/volume.

Currently `open_trade` in `xtb_web.py` does not pass SL/TP, so the live code path is safe. But the data is live in every `Position` object and available to any code that handles positions. If a future developer touches `_handle_new_position` or `open_trade` and references `pos.sl / pos.tp` (an obvious thing to do since the fields are right there), they will silently start setting SL/TP on XTB trades, violating the rule.

The "no SL/TP on XTB" invariant is not enforced anywhere — it is just relying on omission.

**Fix:**
1. Remove `sl` and `tp` from `Position` entirely (they are not used by any XTB code path).
2. If SL/TP needs to be visible in the GUI table for monitoring purposes, create a separate read-only display model rather than embedding them in the trade execution model.
3. If removing them is undesirable, add a loud assertion in `open_trade`:

```python
# In xtb_web.py XTBWeb.open_trade — defensive guard
async def open_trade(self, symbol: str, direction: Direction, volume: float) -> bool:
    # INVARIANT: XTB trades are opened symbol/direction/volume ONLY.
    # No SL/TP is ever set. If you're adding SL/TP here, stop and re-read the design doc.
    ...
```

And remove `sl`/`tp` from `Position` in `models.py`:

```python
@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    open_price: float = 0.0
    # sl and tp intentionally omitted — XTB mirror trades must NEVER carry SL/TP
```

---

### CR-02: Real credentials committed to the git repository

**File:** `config.toml:6-7`

**Issue:** `config.toml` contains a plaintext email address (`ptr.szczepaniak@gmail.com`) and a masked (but present) password field. Even though the password value shows as `"************"`, this is the live `config.toml`, not the example. The `.gitignore` correctly excludes `config.toml`, but the file was committed in an earlier state or left on disk with a real email. More seriously, `xtb_bridge.log` and `mapping.json` are also present in the working tree and are gitignored — but if the gitignore was ever misconfigured or `git add -f` was used, real session artifacts could be pushed.

**Fix:**
- Rotate the XTB password immediately since the email is now visible in the repo history.
- Verify via `git log --all -- config.toml` that no commit contains the real password in history. If it does, use `git filter-repo` to purge it.
- Add a pre-commit hook that fails if `config.toml` (or any file containing `password =`) is staged:

```bash
# .git/hooks/pre-commit
if git diff --cached --name-only | grep -q 'config\.toml'; then
  echo "ERROR: config.toml must not be committed (contains credentials)"
  exit 1
fi
```

---

## HIGH

---

### HI-01: BUY button CSS selector is broken — trades will silently fail to open

**File:** `xtb_bridge/xtb_web.py:208`

**Issue:** The BUY button locator uses dot-notation that means "class='xui-btn' AND class='xui-btn-ct-buy' AND class='xs-btn-buy'", but the dots are un-spaced tokens treated as a compound class selector:

```python
btn = self._page.locator(".xui-btn xui-btn-ct-buy xs-btn-buy").first
```

The second and third tokens (`xui-btn-ct-buy`, `xs-btn-buy`) are missing the leading dot — CSS descendant selectors, not class selectors. This string means "an element with class `xui-btn` that contains a descendant element named `xui-btn-ct-buy`" (a tag name), which matches nothing. Playwright will return an empty locator, and the subsequent `btn.click()` will time out and throw, causing `open_trade` to return `False`. Every BUY trade silently fails.

The SELL selector on line 210 (`".xs-btn-sell"`) is correctly formed.

**Fix:**

```python
if direction == Direction.BUY:
    btn = self._page.locator(".xs-btn-buy").first
else:
    btn = self._page.locator(".xs-btn-sell").first
```

If the class name isn't confirmed, a safer combined selector:

```python
btn = self._page.locator(
    ".xs-btn-buy, [class*='btn-buy'], [class*='ct-buy']"
).first
```

---

### HI-02: open_trade verification logic can re-fire a trade spuriously

**File:** `xtb_bridge/xtb_web.py:242-249`

**Issue:** After clicking the trade button (step 6), `_has_no_open_positions()` is called to verify the trade opened. If it returns `True` (no positions visible), the button is clicked **again**:

```python
if no_positions:
    self._log("Trade not confirmed — retrying click...")
    await btn.click(timeout=ACTION_TIMEOUT_MS)
```

There are two bugs here:

1. `_has_no_open_positions()` checks for an empty-state marker (`xs6-open-positions-feature .empty-page`). If the positions panel is not currently visible or in a loading state, it returns `False` (the empty marker is not visible) — but that just means the panel isn't showing the empty state, NOT that the trade succeeded. The only reliable `True` from this check is when the panel is visible AND empty. If the trade DID open but the panel is hidden, the check returns `False` and incorrectly skips the retry — that's fine. But if the panel is loading (element exists but trade hasn't rendered yet), it could return `True` and fire a duplicate trade.

2. The function at line 267-270 returns `True` when `empty.count() > 0 AND empty.first.is_visible()`. After a fresh trade open, there's a 1500ms wait, but the positions panel may not be open (the code only opened the chart tab, not the positions panel). The empty-state element would not be present at all, so `count() > 0` returns `False`, making the function return `False` regardless — meaning the retry is never triggered even when the trade failed. The verification step effectively never catches a failed trade.

**Fix:** Remove the unreliable re-click logic. Return success once the button click completes without exception. If trade verification is needed, scrape the positions panel explicitly after navigating to it:

```python
# Step 6: Navigate to positions panel and confirm the open
# (remove the speculative re-click — it can double-open positions)
self._log(f"Trade submitted: {direction.value} {symbol} {volume} lots")
self._record_action_success()
return True
```

If verification is essential, gate the retry on actually checking the positions list for the specific symbol, not on the empty-state widget.

---

### HI-03: close_trade is direction-agnostic — wrong position can be closed when holding both BUY and SELL on same symbol

**File:** `xtb_bridge/xtb_web.py:297-318`

**Issue:** `close_trade` finds the close button by matching the symbol text in the ancestor row, then clicks the first match. It does not inspect the direction column of the row. If XTB has both a BUY and a SELL position open for the same symbol (hedge), the bridge will always close the row that appears first in the list — it may close the wrong direction.

This is a financial correctness bug: closing the wrong leg of a hedge, or closing a position that was manually opened, with no ability to distinguish.

**Fix:** After finding rows containing `symbol`, additionally check for the direction text in the row before selecting it:

```python
for i in range(total):
    btn = close_btns.nth(i)
    row_with_symbol = btn.locator(
        f"xpath=ancestor::*[contains(normalize-space(.), '{symbol}')][1]"
    )
    if await row_with_symbol.count() > 0:
        row_text = await row_with_symbol.first.inner_text()
        # Only select rows that also contain the direction text
        if direction.value in row_text.upper():
            target_btn = btn
            break
```

---

### HI-04: Thread-safety: `_close_all_requested` and `_force_sync_requested` are plain bools written from the GUI thread and read from the bridge thread

**File:** `xtb_bridge/bridge.py:70-72`, `bridge.py:637-648`

**Issue:** `_close_all_requested` and `_force_sync_requested` are Python `bool` attributes. They are written by `request_close_all()` and `request_force_sync()`, which are called from the GUI thread via Qt signal connections. They are read in `_poll_cycle()` which runs on the bridge thread.

Python's GIL makes simple bool reads/writes individually atomic on CPython, but:
- The pattern `if self._close_all_requested: self._close_all_requested = False` is a non-atomic check-then-clear that can theoretically see a stale state if the GIL switches between the check and the clear.
- More importantly, the code relies on CPython GIL behaviour that is explicitly not guaranteed by the Python language spec, and will break under free-threaded Python (PEP 703, available from 3.13+).

The other queues (`_manual_close_queue`, `_reopen_request_queue`, etc.) correctly use `collections.deque` which is thread-safe. These two booleans should follow the same pattern.

**Fix:** Replace both booleans with `deque` sentinels (consistent with existing pattern) or use `threading.Event`:

```python
# In __init__
from threading import Event
self._close_all_event = Event()
self._force_sync_event = Event()

# In request_close_all / request_force_sync
self._close_all_event.set()
self._force_sync_event.set()

# In _poll_cycle
if self._close_all_event.is_set():
    self._close_all_event.clear()
    self._drain_close_all()
if self._force_sync_event.is_set():
    self._force_sync_event.clear()
    self._reconcile_counter = self._reconcile_every_n_polls
```

---

## MEDIUM

---

### ME-01: Reconciliation symbol match is too broad — can prevent legitimate close

**File:** `xtb_bridge/bridge.py:303`

**Issue:** Reconciliation checks whether a symbol is open in XTB using substring matching against the full row text:

```python
if not any(xtb_symbol in row_text for row_text in xtb_rows):
```

Row text in xStation5 includes the symbol, P&L values, prices, and other numbers. A symbol like `"USD"` would match any row containing the string "USD" anywhere, including `"EURUSD"`, `"USDJPY"`, `"USDCAD"` rows. This means:

- A mapped EURUSD position would be kept in the mapping (not auto-reopened) even if the actual EURUSD position was closed, as long as any other USD-containing row exists.

For the current symbol list (EURUSD, GBPUSD, XAUUSD, USDCAD, GBPCHF) the risk is low because the exact symbol names are long enough. But if a short symbol like "US30" or "OIL" were added, false matches become likely.

**Fix:** Use word-boundary matching or look for the symbol followed by a space or non-alpha character:

```python
import re
pattern = re.compile(rf'\b{re.escape(xtb_symbol)}\b')
if not any(pattern.search(row_text) for row_text in xtb_rows):
    to_remove.append(mt5_ticket)
```

---

### ME-02: `_handle_new_position` uses `pos.ticket` as both MT5 and XTB ticket — XTB order ID is never retrieved

**File:** `xtb_bridge/bridge.py:520`

**Issue:** When a new position is mirrored, the mapping stores `pos.ticket` as both the MT5 ticket and the XTB order:

```python
self._mapping.add(pos.ticket, pos.ticket)
```

The comment acknowledges this: `"(using ticket as placeholder since we can't get XTB order from UI)"`. This means `TicketMapping.get_xtb_order()` always returns the MT5 ticket number, not a real XTB order ID. Any code that later calls `get_xtb_order()` for XTB-specific purposes (e.g., to close by XTB order ID) will receive a wrong value.

Currently no code path calls `get_xtb_order()` for trading — close operations look up by symbol/direction — so this is not causing live bugs. But it makes `TicketMapping` misleading and represents technical debt that is one refactor away from causing a real issue.

**Fix:** Either rename the field to make the limitation explicit:

```python
self._mapping.add(pos.ticket, xtb_order=0)  # 0 = unknown
```

Or add a comment in `TicketMapping` making the invariant clear, and mark `get_xtb_order()` as deprecated/unused.

---

### ME-03: `mt5_reader.connect()` crashes if `terminal_info()` returns None

**File:** `xtb_bridge/mt5_reader.py:20-21`

**Issue:**

```python
info = mt5.terminal_info()
log.info("MT5 connected: %s (build %s)", info.name, info.build)
```

`mt5.initialize()` can succeed but `mt5.terminal_info()` can return `None` in certain edge cases (terminal not fully loaded, IPC not ready). If `info` is `None`, the `.name` attribute access raises `AttributeError`, crashing `connect()` before it returns `True`, which means the caller's `if not connect():` branch fires — correct behaviour, but the error message ("Cannot connect to MT5") is misleading because `initialize()` did succeed.

**Fix:**

```python
info = mt5.terminal_info()
if info is None:
    log.warning("MT5 terminal_info() returned None after initialize")
else:
    log.info("MT5 connected: %s (build %s)", info.name, info.build)
return True
```

---

### ME-04: `_on_start_stop` optimistically sets `_bridge_running = True` before `on_start` validates credentials

**File:** `xtb_bridge/gui/main_window.py:234-247`, `xtb_bridge/main.py:157-168`

**Issue:** When the user clicks START:

1. `_on_start_stop()` sets `self._bridge_running = True` and calls `self.start_requested.emit()`.
2. `on_start()` (connected to that signal) then validates credentials.
3. If credentials are missing, it resets `window._bridge_running = False` and fixes the button state.

This works, but the direct field access `window._bridge_running = False` from outside the class (in `main.py`) breaks encapsulation and is fragile — if the field is renamed, it silently stops working. More subtly, between steps 1 and 3, the bridge state label briefly shows "Running" before reverting to "Stopped".

**Fix:** Move credential validation into `_on_start_stop()` before emitting `start_requested`, or add a proper `cancel_start()` slot on `MainWindow` that resets state cleanly:

```python
# In MainWindow
def cancel_start(self) -> None:
    """Called when a start attempt is aborted (e.g. missing credentials)."""
    self._bridge_running = False
    self._bridge_paused = False
    self._update_start_stop_style()
    self._update_bridge_state_label()
    self._set_operational_buttons_enabled(False)
```

And in `main.py`, replace the three direct field accesses with `window.cancel_start()`.

---

### ME-05: Log file path is relative to CWD — behaviour depends on where the app is launched from

**File:** `xtb_bridge/main.py:22`

**Issue:**

```python
LOG_FILE = Path("xtb_bridge.log")
```

`Path("xtb_bridge.log")` resolves relative to the process's current working directory at launch time. If the app is launched from a different directory (e.g. via a shortcut, from a different shell directory, or as a packaged `.exe`), the log file appears somewhere unexpected. Same issue applies to `MAPPING_FILE = Path("mapping.json")` and `BROWSER_DATA_DIR = Path("xtb_browser_data")` in `config.py` and `xtb_web.py`.

**Fix:** Anchor all persistent file paths to the script's own directory:

```python
# In main.py
from pathlib import Path
BASE_DIR = Path(__file__).parent.parent  # project root
LOG_FILE = BASE_DIR / "xtb_bridge.log"
```

Apply the same pattern to `MAPPING_FILE` in `config.py` and `BROWSER_DATA_DIR` in `xtb_web.py`.

---

### ME-06: `_drain_close_all` adds tickets to `_user_closed_tickets` but the underlying MT5 positions remain open — bridge will auto-reopen them

**File:** `xtb_bridge/bridge.py:371-392`

**Issue:** After `_drain_close_all` closes a position in XTB, it:
1. Removes the mapping entry.
2. Adds the ticket to `_user_closed_tickets`.

This is correct — the ticket stays in `_user_closed_tickets` so step 5 won't auto-reopen. But `_user_closed_tickets` is cleaned up in `_poll_cycle` step 7:

```python
self._user_closed_tickets.intersection_update(current.keys())
```

This removes tickets from `_user_closed_tickets` as soon as the MT5 position closes. If the MT5 position is still open (which it will be — "Close All" only closes XTB, not MT5), the ticket remains in `_user_closed_tickets` indefinitely — that's correct.

However, if reconciliation runs before the user re-opens manually, reconciliation will remove the mapping (step 4 in `_poll_cycle`), but the ticket is still in `_user_closed_tickets`, so step 5 won't re-open. This is intentional. The flow is correct.

The actual bug is subtler: after "Close All", the bridge log says "Closed EURUSD (ticket 12345)" but the XTB panel still shows no positions. If the user then clicks **Reopen** on a ticket that was in "Close All", `_drain_reopen_requests` removes it from `_user_closed_tickets`, and `_handle_new_position` is called on the next poll — which is correct. **No bug here on further analysis; this note is left for clarity.**

---

### ME-07: `config.py` duplicate symbol-map / load functions — `save_mapping` / `load_mapping` are unused

**File:** `xtb_bridge/config.py:97-107`

**Issue:** `save_mapping()` and `load_mapping()` module-level functions are defined in `config.py` but never called. The bridge uses `bridge.py`'s `_load_mapping()` / `_save_mapping()` methods instead, which open the file directly. These orphan functions are dead code.

**Fix:** Remove `save_mapping` and `load_mapping` from `config.py`, or consolidate the file I/O into one place.

---

## LOW

---

### LO-01: Log line trim in LogWidget miscounts lines after bulk inserts

**File:** `xtb_bridge/gui/log_widget.py:44-46`, `63-69`

**Issue:** `_line_count` is incremented by 1 per `append_log` call regardless of whether the message itself contains newlines. If any log message contains embedded `\n` characters, `_line_count` will be lower than the actual line count in the widget. The `_trim_lines` loop attempts to delete exactly `(_line_count - MAX_LINES)` lines but will undershoot if some messages are multi-line, leaving the widget with more than `MAX_LINES` lines.

This is a cosmetic issue — the widget just uses a bit more memory than intended.

**Fix:** Count actual lines when incrementing, or use a character-based document limit instead of a line counter.

---

### LO-02: Screenshot filenames can collide within the same second

**File:** `xtb_bridge/xtb_web.py:582-584`

**Issue:**

```python
path = f"screenshots/{name}_{int(time.time())}.png"
```

`time.time()` has 1-second resolution when truncated with `int()`. If multiple screenshots are taken within the same second (e.g., `before_click_...` and `after_click_...`), the second one silently overwrites the first.

**Fix:** Use millisecond precision or a counter suffix:

```python
path = f"screenshots/{name}_{int(time.time() * 1000)}.png"
```

---

### LO-03: `_trim_lines` cursor selection approach is fragile with QTextEdit

**File:** `xtb_bridge/gui/log_widget.py:63-69`

**Issue:** The trimming loop moves the cursor down one line at a time from the start, keeping the selection anchor, then calls `removeSelectedText()`. This is O(n) in the number of lines to trim and operates inside the Qt event loop on each `append_log` call. For normal usage with MAX_LINES=1000 this is fine, but if logging is very rapid (hundreds of messages per second), this could cause GUI stuttering.

Additionally, `cursor.MoveOperation.Down` moves by visual lines in a word-wrapped widget. If the `QTextEdit` wraps long messages, one "line" in the document might render as two visual lines, causing incorrect trim counts.

**Fix:** Use `QTextDocument` block counting or trim by block count rather than visual lines:

```python
def _trim_lines(self) -> None:
    doc = self.document()
    while doc.blockCount() > MAX_LINES + 1:  # +1 for trailing empty block
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        cursor.select(cursor.SelectionType.BlockUnderCursor)
        cursor.movePosition(cursor.MoveOperation.EndOfBlock,
                            cursor.MoveMode.KeepAnchor)
        cursor.movePosition(cursor.MoveOperation.NextCharacter,
                            cursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
```

---

### LO-04: `close_trade` does not validate that the direction in the row actually matches before closing

**File:** `xtb_bridge/xtb_web.py:308-318`

**Issue:** (Lesser form of HI-03.) Even outside the hedge scenario, `close_trade` accepts a `direction` parameter but ignores it entirely during row selection. The `direction` argument is only used in the log message. If the first row containing the symbol belongs to a different direction than intended, the wrong position is closed silently.

This is the same root cause as HI-03 — addressed by that fix. Listed separately to ensure the `direction` parameter is not confused for validated input.

---

### LO-05: `open_trade` volume input strategy is fragile — triple-click + Backspace may leave stale digits

**File:** `xtb_bridge/xtb_web.py:200-203`

**Issue:**

```python
await volume_input.click(click_count=3)
await volume_input.press("Backspace")
await volume_input.type(str(round(volume, 2)), delay=50)
```

Triple-click selects all text in most inputs, but `Backspace` then deletes only one character if the selection is lost (e.g., due to focus change or slow rendering). If the field previously contained "0.10" and the bridge types "0.05", the result could be "0.10.05" or "0.105". The `Backspace` is redundant when text is selected — selected text is replaced by the first keypress.

**Fix:** Use `fill()` which clears and sets the value atomically:

```python
await volume_input.fill(str(round(volume, 2)))
```

This is Playwright's idiomatic approach for setting input values.

---

### LO-06: `get_open_positions()` in XTBWeb is never called — dead code

**File:** `xtb_bridge/xtb_web.py:356-390`

**Issue:** `XTBWeb.get_open_positions()` and `XTBWebSync.get_open_positions()` are defined but never called from `bridge.py` or anywhere else in the codebase. Reconciliation uses `scrape_open_position_texts()` instead. This is dead code.

**Fix:** Remove both methods, or mark them clearly as internal utilities with a `# currently unused` comment if they are intended for future use.

---

### LO-07: `xtb_bridge.log` and `mapping.json` are present on disk despite being gitignored

**File:** `xtb_bridge.log`, `mapping.json`

**Issue:** Both files exist in the working tree. While gitignored, `mapping.json` contains real MT5 ticket numbers (live trade IDs), and `xtb_bridge.log` may contain trading activity logs, login messages, and symbol names. Neither file contains credentials directly, but together they confirm live trading activity and specific account structure. If the directory is ever zipped and shared for debugging, these files would be included.

**Fix:** Add these to a `.bundleignore` / archive exclusion, or note in the README that these files contain live trading data and must not be shared.

---

## Findings Summary

| ID    | Severity | File                      | Issue |
|-------|----------|---------------------------|-------|
| CR-01 | CRITICAL | models.py, mt5_reader.py  | SL/TP fields in Position model — invariant "no SL/TP on XTB" not enforced |
| CR-02 | CRITICAL | config.toml               | Real email committed; credentials in plaintext file |
| HI-01 | HIGH     | xtb_web.py:208            | BUY button CSS selector is malformed — all BUY trades silently fail |
| HI-02 | HIGH     | xtb_web.py:242-249        | Trade verification can spuriously re-fire a trade (double open) |
| HI-03 | HIGH     | xtb_web.py:297-318        | close_trade ignores direction — wrong leg closed when hedging |
| HI-04 | HIGH     | bridge.py:70-72           | Plain bool flags written from GUI thread, read from bridge thread — not thread-safe |
| ME-01 | MEDIUM   | bridge.py:303             | Reconcile symbol match too broad — substring collision on short symbols |
| ME-02 | MEDIUM   | bridge.py:520             | TicketMapping stores MT5 ticket as XTB order ID — misleading |
| ME-03 | MEDIUM   | mt5_reader.py:20          | `terminal_info()` result not null-checked before attribute access |
| ME-04 | MEDIUM   | main_window.py:234, main.py:157 | `_bridge_running` mutated across module boundary; breaks encapsulation |
| ME-05 | MEDIUM   | main.py:22, config.py:8   | All persistent paths are CWD-relative — wrong location on non-standard launch |
| ME-07 | MEDIUM   | config.py:97-107          | `save_mapping` / `load_mapping` are dead code |
| LO-01 | LOW      | log_widget.py:44          | Line count wrong when messages contain embedded newlines |
| LO-02 | LOW      | xtb_web.py:582            | Screenshot filenames collide within the same second |
| LO-03 | LOW      | log_widget.py:63          | Trim loop uses visual lines — breaks with word-wrap |
| LO-04 | LOW      | xtb_web.py:308            | `direction` param to close_trade is ignored in row selection |
| LO-05 | LOW      | xtb_web.py:200-203        | Volume input clear strategy fragile — use `fill()` instead |
| LO-06 | LOW      | xtb_web.py:356-390        | `get_open_positions()` is dead code |
| LO-07 | LOW      | xtb_bridge.log, mapping.json | Live data files present on disk; would be included in archive shares |

---

_Reviewed: 2026-04-30_  
_Reviewer: Claude (deep review, all source files)_
