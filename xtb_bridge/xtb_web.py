from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .models import Direction, Position

log = logging.getLogger(__name__)

# Persistent browser profile directory (keeps cookies/session between restarts)
BROWSER_DATA_DIR = Path("xtb_browser_data")

# Timeouts
LOGIN_TIMEOUT_MS = 120_000  # 2 min — user may need to handle CAPTCHA/2FA
ACTION_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 30_000

# How many consecutive action failures before triggering a full page recovery
MAX_ACTION_FAILURES = 3


class XTBWeb:
    """Controls xStation5 Web via Playwright browser automation."""

    def __init__(
        self,
        email: str,
        password: str,
        account_type: str = "demo",
        on_log: Callable[[str], None] | None = None,
    ):
        self._email = email
        self._password = password
        self._account_type = account_type
        self._on_log = on_log or (lambda msg: None)

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._logged_in = False
        self._consecutive_action_failures = 0
        self._last_opened_position_id: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def launch(self) -> None:
        self._playwright = await async_playwright().start()
        BROWSER_DATA_DIR.mkdir(exist_ok=True)

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._log("Browser launched")

    async def login(self) -> bool:
        if not self._page:
            raise RuntimeError("Browser not launched. Call launch() first.")

        self._log("Navigating to xStation5...")
        await self._page.goto("https://xstation5.xtb.com", timeout=NAV_TIMEOUT_MS)

        try:
            # Detect login page by looking for a submit/login button
            # (the trading UI never has these — most reliable indicator)
            submit_btn = self._page.locator(
                "button[type='submit'], button:has-text('Log in'), "
                "button:has-text('Zaloguj'), button:has-text('Sign in'), "
                "input[type='submit']"
            ).first

            try:
                await submit_btn.wait_for(state="visible", timeout=15000)
            except Exception:
                # No login button found — already logged in
                self._logged_in = True
                self._log("Already logged in (session restored)")
                return True

            self._log("Login form detected, filling credentials...")

            # Fill email
            email_input = self._page.locator(
                "input[type='email'], input[name='email'], "
                "input[type='text'][name*='login'], input[type='text'][name*='user'], "
                "input[placeholder*='email' i], input[placeholder*='login' i], "
                "input[placeholder*='ID' i]"
            ).first
            await email_input.fill(self._email)

            # Fill password
            password_input = self._page.locator("input[type='password']").first
            await password_input.fill(self._password)

            # Click login/submit button
            await submit_btn.click()

            self._log("Credentials submitted. Waiting for main view...")
            self._log("(Handle CAPTCHA/2FA in the browser window if prompted)")

            # Wait for login button to disappear (trading view loaded)
            await submit_btn.wait_for(state="hidden", timeout=LOGIN_TIMEOUT_MS)
            self._logged_in = True
            self._log("Login successful")
            return True

        except Exception as e:
            self._log(f"Login failed: {e}")
            await self._screenshot("login_error")
            return False

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._playwright = None
        self._logged_in = False
        self._log("Browser closed")

    # ------------------------------------------------------------------
    # Trading actions
    # ------------------------------------------------------------------

    async def open_trade(self, symbol: str, direction: Direction, volume: float) -> bool:
        if not self._page:
            self._log("Cannot open trade — browser not launched")
            return False

        if not await self._ensure_healthy():
            self._log("Cannot open trade — session recovery failed")
            return False

        try:
            self._log(f"Opening {direction.value} {symbol} {volume} lots...")

            # Step 1: Navigate to the instrument
            # Take debug screenshot to see current state
            await self._screenshot(f"before_trade_{symbol}")

            # Clamp volume to xStation5 allowed range
            volume = max(0.01, min(350.0, round(volume, 2)))

            # Click on the instrument tab if already open, or find it in market watch.
            # xStation5 chart tab structure:
            #   <div class="chart-symbols-tab ...">
            #     <span class="chart-symbol-label">USDCAD (M15)</span>
            #     <div class="chart-panel-close-section">
            #        <div class="chart-close-btn">  <-- DO NOT CLICK (closes chart!)
            #     </div>
            #   </div>
            # We click the label span (left side of tab) to avoid the close button
            # on the right.  The click bubbles up to the outer div and activates the tab.
            tab = self._page.locator(
                f".chart-symbols-tab span.chart-symbol-label:has-text('{symbol}')"
            ).first
            try:
                await tab.click(timeout=ACTION_TIMEOUT_MS)
                self._log(f"Switched to existing tab for {symbol}")
            except Exception:
                # No tab open — search for instrument in market watch
                self._log(f"No tab for {symbol}, searching in market watch...")
                # Look for search input — exclude indicator search ("Dodaj wskaźnik")
                search_input = self._page.locator(
                    "input[placeholder*='search' i], input[placeholder*='szukaj' i], "
                    "input[placeholder*='instrument' i]"
                ).first
                await search_input.click(timeout=ACTION_TIMEOUT_MS)
                await search_input.fill("")
                await search_input.fill(symbol)
                await self._page.wait_for_timeout(1000)

                # Click on the instrument from search results
                result = self._page.locator(
                    f"[class*='search-result'] :text-is('{symbol}'), "
                    f"[class*='instrument'] :text-is('{symbol}'), "
                    f"tr:has-text('{symbol}'), "
                    f"div:has-text('{symbol}')"
                ).first
                await result.click(timeout=ACTION_TIMEOUT_MS)

            # Step 1b: Disable SL/TP click-trading if active.
            # The SL/TP button has class "changed" when enabled — clicking it
            # toggles it off so no automatic Stop Loss / Take Profit is added.
            try:
                sltp_btn = self._page.locator("button.sl-tp-button.changed").first
                if await sltp_btn.count() > 0 and await sltp_btn.is_visible():
                    await sltp_btn.click(timeout=2000)
                    self._log("SL/TP click-trading disabled")
            except Exception:
                pass

            # Step 1c: Switch active chart to M15 timeframe (best-effort).
            # Doesn't affect trade execution — purely visual consistency.
            try:
                # xStation5 chart header shows current interval as a clickable button,
                # e.g. "M30 ▼". Click it to open the period dropdown, then pick M15.
                interval_btn = self._page.locator(
                    "[class*='chart-interval'], [class*='interval-btn'], "
                    "[class*='period-btn'], button[class*='interval']"
                ).first
                await interval_btn.click(timeout=2000)
                await self._page.wait_for_timeout(200)
                m15_option = self._page.locator(
                    "text='M15', [data-interval='M15'], "
                    "li:has-text('M15'), button:has-text('M15'), "
                    "[class*='interval-option']:has-text('M15')"
                ).first
                await m15_option.click(timeout=2000)
                self._log(f"Chart set to M15 for {symbol}")
            except Exception:
                pass  # Non-critical — trading continues regardless of timeframe

            # Step 2: Set the volume in the click-trading bar.
            # xStation5 renders trading panels for ALL open chart tabs simultaneously.
            # Taking `.first` would grab EURUSD's stepper even when USDCAD is active,
            # then clicking it would switch the active chart back to EURUSD.
            # Fix: iterate all matching inputs and pick the one that is currently visible
            # (only the active chart's panel is visible).
            self._log(f"Setting volume to {volume}...")
            all_inputs = self._page.locator(
                "input[name='stepperInput'], input.xs-stepper-input"
            )
            deadline = asyncio.get_event_loop().time() + ACTION_TIMEOUT_MS / 1000
            volume_input = None
            while True:
                count = await all_inputs.count()
                for i in range(count):
                    candidate = all_inputs.nth(i)
                    if await candidate.is_visible():
                        volume_input = candidate
                        break
                if volume_input is not None:
                    break
                if asyncio.get_event_loop().time() > deadline:
                    raise Exception("Timed out waiting for visible stepper input")
                await self._page.wait_for_timeout(200)
            await volume_input.click(timeout=ACTION_TIMEOUT_MS)
            await volume_input.press("Control+a")
            await volume_input.type(str(round(volume, 2)), delay=50)
            self._log("Volume set")

            # Step 3: Click the price button (green=BUY/ask, red=SELL/bid).
            # Same multi-panel issue: find the visible buy/sell button, not just .first.
            if direction == Direction.BUY:
                all_btns = self._page.locator("#buyButton, .xs-btn-buy")
            else:
                all_btns = self._page.locator("#sellButton, .xs-btn-sell")
            btn = None
            count = await all_btns.count()
            for i in range(count):
                candidate = all_btns.nth(i)
                if await candidate.is_visible():
                    btn = candidate
                    break
            if btn is None:
                raise Exception(f"No visible {direction.value} button found")

            await self._screenshot(f"before_click_{direction.value}_{symbol}")
            self._log(f"Clicking {direction.value} button...")
            await btn.click(timeout=ACTION_TIMEOUT_MS)
            self._log(f"{direction.value} button clicked")
            await self._page.wait_for_timeout(2000)
            await self._screenshot(f"after_click_{direction.value}_{symbol}")

            # Step 4: Confirm the trade if a modal confirmation dialog appears.
            # Use narrow selectors — avoid 'OK' alone (too generic) and
            # 'Zamknij'/'Close' (could match position-close buttons).
            try:
                confirm = self._page.locator(
                    "button:text-is('Confirm'), button:text-is('Potwierdź'), "
                    "button:text-is('Accept'), button:text-is('Yes'), "
                    "button:text-is('Tak')"
                ).first
                await confirm.click(timeout=2000)
                self._log("Confirmation dialog accepted")
            except Exception:
                pass  # No confirmation dialog — normal for most account settings

            # Step 5 removed: success notifications auto-dismiss after a few seconds.
            # Programmatic dismissal via generic selectors risks clicking
            # unintended elements (e.g. "Zamknij wszystkie" close-all button).

            # Capture the newly opened XTB position ID so the bridge can close
            # the exact position later (not just any position of that direction).
            self._last_opened_position_id = None
            try:
                pos_tab = self._page.locator(
                    "div:has-text('Open positions'), div:has-text('Otwarte pozycje'), "
                    "[class*='positions-tab'], [class*='open-positions']"
                ).first
                await pos_tab.click(timeout=3000)
                await self._page.wait_for_timeout(500)
                ids = await self._get_xtb_child_ids(symbol, direction)
                if ids:
                    self._last_opened_position_id = max(ids, key=int)
                    self._log(f"XTB position ID captured: {self._last_opened_position_id}")
            except Exception:
                pass

            self._log(f"Trade opened: {direction.value} {symbol} {volume} lots")
            self._record_action_success()
            return True

        except Exception as e:
            self._log(f"Failed to open trade {direction.value} {symbol}: {e}")
            await self._screenshot(f"open_error_{symbol}")
            await self._record_action_failure(f"open {direction.value} {symbol}")
            return False

    async def _has_no_open_positions(self) -> bool:
        """Check if xStation5 shows the 'no open positions' empty state."""
        try:
            empty = self._page.locator(
                "xs6-open-positions-feature .empty-page, "
                "xs6-open-positions-feature app-empty-page"
            )
            return await empty.count() > 0 and await empty.first.is_visible()
        except Exception:
            return False

    async def close_trade(self, symbol: str, direction: Direction,
                          xtb_position_id: str | None = None) -> bool:
        if not self._page:
            self._log("Cannot close trade — browser not launched")
            return False

        if not await self._ensure_healthy():
            self._log("Cannot close trade — session recovery failed")
            return False

        try:
            self._log(f"Closing {direction.value} {symbol}...")
            await self._screenshot(f"before_close_{symbol}")

            # Make sure the Open Positions panel is visible (best-effort; ignore
            # if the tab selector doesn't match or panel is already active).
            positions_tab = self._page.locator(
                "div:has-text('Open positions'), div:has-text('Otwarte pozycje'), "
                "[class*='positions-tab'], [class*='open-positions']"
            ).first
            try:
                await positions_tab.click(timeout=3000)
                await self._page.wait_for_timeout(300)
            except Exception:
                pass

            # Find the close button for THIS specific position using JS evaluate().
            #
            # xStation5 panels have TWO types of [data-testid="close-button"]:
            #   1. Group header button — closes ALL positions for a symbol (e.g. all USDCAD)
            #   2. Individual row button — closes exactly one position
            #
            # Distinguishing them: individual position rows show direction text ("Buy"/"Sell")
            # in a compact ancestor (depth 1-4, text < 400 chars).  Group header rows show
            # only the symbol name — direction text is absent from their compact ancestors.
            #
            # Algorithm:
            #   a) Find the group-header close button for our symbol
            #      (compact ancestor has symbol text, no direction text).
            #   b) Find the next group-header button (to bound the search range).
            #   c) Return the first button between them whose compact ancestor has direction text.
            close_btns = self._page.locator('[data-testid="close-button"]')
            total = await close_btns.count()
            self._log(f"Found {total} close button(s) on page")

            _slashed = symbol[:3] + "/" + symbol[3:] if len(symbol) == 6 else symbol
            _pl = {"BUY": "KUP", "SELL": "SPRZEDAJ"}
            _sym_variants = list({symbol, _slashed})
            _dir_variants = list({
                direction.value,
                direction.value.title(),
                direction.value.lower(),
                _pl.get(direction.value, direction.value),
            })

            # xStation5 uses Angular Emulated ViewEncapsulation — NOT real Shadow DOM.
            # Each row (<tr>) has TWO close buttons: one in the tablet column and one in
            # the last (desktop) column.  The group-row tr has 2 buttons; child-row tr
            # also has 2 buttons.  We must use DOM element identity (tr !== groupTr) to
            # skip past the group-row buttons instead of checking isGroup() on the next
            # button (which would stop too early at the second button of the same row).
            _JS_CLOSE = r"""
            (btns, args) => {
                const [syms, dirs, posId] = args;
                const hasSym = t => syms.some(s => t.includes(s));
                const hasDir = t => dirs.some(d => t.includes(d));
                const getTr = btn => btn.closest('tr');
                const isChild = btn => { const r = getTr(btn); return r && r.classList.contains('child-row'); };
                const isGroup = btn => { const r = getTr(btn); return r && r.classList.contains('group-row'); };
                const trText = btn => { const r = getTr(btn); return r ? (r.innerText || '').trim() : ''; };
                // Each row has two close buttons: one in the hidden tablet column and one
                // in the visible desktop last-column td (class contains "row-renderer").
                const isDesktop = btn => !!btn.closest('td[class*="row-renderer"]');

                // Strategy 0: exact XTB position ID match (most precise)
                if (posId) {
                    for (let i = 0; i < btns.length; i++) {
                        if (isChild(btns[i]) && isDesktop(btns[i])) {
                            const m = trText(btns[i]).match(/^(\d+)/);
                            if (m && m[1] === posId) return i;
                        }
                    }
                }

                // Strategy 1: find group-row tr for our symbol via DOM identity,
                // then find the first DESKTOP child-row button with matching direction.
                // IMPORTANT: second loop starts from groupIdx (not 0) so we don't
                // accidentally enter the "past group" state on a different symbol's row.
                let groupIdx = -1;
                let groupTr = null;
                for (let i = 0; i < btns.length; i++) {
                    if (isGroup(btns[i]) && hasSym(trText(btns[i]))) {
                        groupIdx = i;
                        groupTr = getTr(btns[i]);
                        break;
                    }
                }
                if (groupTr !== null) {
                    let pastGroup = false;
                    let bestMatch = -1;
                    let firstChild = -1;
                    for (let i = groupIdx; i < btns.length; i++) {
                        const t = getTr(btns[i]);
                        if (!pastGroup) {
                            if (t !== groupTr) pastGroup = true;
                            else continue;
                        }
                        if (isChild(btns[i]) && isDesktop(btns[i])) {
                            if (firstChild === -1) firstChild = i;
                            if (hasDir(trText(btns[i]))) bestMatch = i; // keep last match
                        }
                        if (isGroup(btns[i])) break;
                    }
                    if (bestMatch !== -1) return bestMatch;
                    if (firstChild !== -1) return firstChild;
                }

                // Strategy 2: desktop child-row button whose tr text contains symbol
                for (let i = 0; i < btns.length; i++) {
                    if (isChild(btns[i]) && isDesktop(btns[i]) && hasSym(trText(btns[i]))) return i;
                }

                return -1;
            }
            """

            btn_idx: int = await close_btns.evaluate_all(_JS_CLOSE, [_sym_variants, _dir_variants, xtb_position_id or ""])
            self._log(
                f"JS close-button search: idx={btn_idx} total={total} "
                f"syms={_sym_variants}"
            )

            if btn_idx < 0:
                _JS_DIAG = """
                (btns) => btns.map((b, i) => {
                    const r = b.closest('tr');
                    return {
                        i,
                        testid: r ? r.getAttribute('data-testid') : 'no-tr',
                        cls: r ? r.className : '',
                        text: r ? (r.innerText || '').slice(0, 80) : ''
                    };
                })
                """
                diag: list = await close_btns.evaluate_all(_JS_DIAG)
                for d in diag:
                    self._log(
                        f"  btn[{d['i']}] testid={d['testid']} cls={d['cls']!r} "
                        f"text={d['text']!r}"
                    )

            target_btn = close_btns.nth(btn_idx) if btn_idx >= 0 else None

            if target_btn is None:
                self._log(f"No close button found for {symbol} — position may already be closed")
                await self._screenshot(f"close_not_found_{symbol}")
                return False

            # click() scrolls into view automatically — no separate scroll_into_view_if_needed
            await target_btn.click(timeout=ACTION_TIMEOUT_MS)
            self._log(f"Close button clicked for {symbol}")
            await self._page.wait_for_timeout(500)

            # Confirm close if a dialog appears (xStation5 may or may not show one
            # depending on user settings).
            try:
                confirm = self._page.locator(
                    "button:has-text('Confirm'), button:has-text('Potwierdź'), "
                    "button:has-text('Yes'), button:has-text('Tak'), "
                    "[data-testid*='confirm'], [data-testid*='submit']"
                ).first
                await confirm.click(timeout=3000)
                self._log("Close confirmation accepted")
            except Exception:
                pass  # No confirmation dialog — some accounts have it disabled

            await self._page.wait_for_timeout(500)
            await self._screenshot(f"after_close_{symbol}")
            self._log(f"Trade closed: {direction.value} {symbol}")
            self._record_action_success()
            return True

        except Exception as e:
            self._log(f"Failed to close trade {symbol}: {e}")
            await self._screenshot(f"close_error_{symbol}")
            # Don't trigger session recovery on close failures — the position may
            # already be gone and the retry loop handles retries independently.
            return False

    async def _get_xtb_child_ids(self, symbol: str, direction: Direction) -> set[str]:
        """Return XTB position IDs in the open-positions table for symbol+direction."""
        _slashed = symbol[:3] + "/" + symbol[3:] if len(symbol) == 6 else symbol
        _sym_variants = list({symbol, _slashed})
        _pl = {"BUY": "KUP", "SELL": "SPRZEDAJ"}
        _dir_variants = list({
            direction.value, direction.value.title(), direction.value.lower(),
            _pl.get(direction.value, direction.value),
        })
        close_btns = self._page.locator('[data-testid="close-button"]')
        ids: list[str] = await close_btns.evaluate_all(r"""
        (btns, args) => {
            const [syms, dirs] = args;
            const hasSym = t => syms.some(s => t.includes(s));
            const hasDir = t => dirs.some(d => t.includes(d));
            const getTr = btn => btn.closest('tr');
            const isChild = btn => { const r = getTr(btn); return r && r.classList.contains('child-row'); };
            const isGroup = btn => { const r = getTr(btn); return r && r.classList.contains('group-row'); };
            const isDesktop = btn => !!btn.closest('td[class*="row-renderer"]');
            const trText = btn => { const r = getTr(btn); return r ? (r.innerText || '').trim() : ''; };
            let groupIdx = -1, groupTr = null;
            for (let i = 0; i < btns.length; i++) {
                if (isGroup(btns[i]) && hasSym(trText(btns[i]))) {
                    groupIdx = i; groupTr = getTr(btns[i]); break;
                }
            }
            const ids = [];
            if (groupTr !== null) {
                let pastGroup = false;
                for (let i = groupIdx; i < btns.length; i++) {
                    const t = getTr(btns[i]);
                    if (!pastGroup) { if (t !== groupTr) pastGroup = true; else continue; }
                    if (isChild(btns[i]) && isDesktop(btns[i]) && hasDir(trText(btns[i]))) {
                        const m = trText(btns[i]).match(/^(\d+)/);
                        if (m) ids.push(m[1]);
                    }
                    if (isGroup(btns[i])) break;
                }
            }
            return ids;
        }
        """, [_sym_variants, _dir_variants])
        return set(ids)

    async def get_open_positions(self) -> list[dict]:
        """Scrape open positions from the xStation5 UI. Returns list of dicts."""
        if not self._logged_in or not self._page:
            return []

        try:
            # Click on open positions tab
            positions_tab = self._page.locator(
                "div:has-text('Open positions'), div:has-text('Otwarte pozycje'), "
                "[class*='positions-tab'], [class*='open-positions']"
            ).first
            try:
                await positions_tab.click(timeout=5000)
                await self._page.wait_for_timeout(500)
            except Exception:
                pass

            # Scrape position rows
            rows = self._page.locator(
                "tr[class*='position'], [class*='position-row'], "
                "[class*='trade-row']"
            )
            count = await rows.count()
            positions = []

            for i in range(count):
                row = rows.nth(i)
                text = await row.inner_text()
                positions.append({"raw_text": text, "index": i})

            return positions

        except Exception as e:
            log.warning("Failed to scrape positions: %s", e)
            return []

    async def scrape_open_position_texts(self) -> list[str] | None:
        """Return inner-text of each open-position row currently in XTB.

        Each row is anchored via its [data-testid="close-button"]. The row
        ancestor's full text lets the caller substring-match against known
        symbols to detect which mapped positions still exist in XTB.

        Returns:
            list[str]: row text for each open position (possibly empty if XTB
                confirmed no open positions).
            None: state could not be determined (don't prune mapping in this
                case — could produce false positives).
        """
        if not self._logged_in or not self._page:
            return None
        try:
            # Make sure the Open Positions panel is visible before scraping.
            # After open_trade() the chart view is active; without this the
            # panel is hidden and close buttons are never found.
            positions_tab = self._page.locator(
                "div:has-text('Open positions'), div:has-text('Otwarte pozycje'), "
                "[class*='positions-tab'], [class*='open-positions']"
            ).first
            try:
                await positions_tab.click(timeout=3000)
                await self._page.wait_for_timeout(500)
            except Exception:
                pass

            close_btns = self._page.locator('[data-testid="close-button"]')
            count = await close_btns.count()

            if count == 0:
                # 0 close buttons could mean: no open positions, OR panel
                # is hidden / not loaded. Confirm via the explicit empty state.
                if await self._has_no_open_positions():
                    return []
                return None

            # Individual position rows ("876667278 Sell 0.04 ...") do NOT contain
            # the symbol name — it lives in the group header above. Rather than
            # guessing ancestor depth (fragile across xStation5 releases), use
            # JS evaluate() to grab the full positions panel text in one shot.
            panel_text: str | None = await self._page.evaluate("""
                () => {
                    // Try known panel selectors first
                    const candidates = [
                        'xs6-open-positions-feature',
                        '[class*="open-positions"]',
                        '[class*="positions-panel"]',
                        '[class*="positions-list"]',
                        '[class*="positions-container"]'
                    ];
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.trim()) {
                            return el.innerText;
                        }
                    }
                    // Fallback: climb up from close buttons until we find
                    // a container with enough text (> 20 chars) that likely
                    // includes the symbol name from the group header.
                    const btns = document.querySelectorAll('[data-testid="close-button"]');
                    const parts = [];
                    for (const btn of btns) {
                        let el = btn.parentElement;
                        for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
                            const t = (el.innerText || '').trim();
                            if (t.length > 20) { parts.push(t); break; }
                        }
                    }
                    return parts.length ? parts.join('\\n') : null;
                }
            """)

            if panel_text and panel_text.strip():
                return [panel_text]
            return None
        except Exception as e:
            log.warning("scrape_open_position_texts failed: %s", e)
            return None

    def is_logged_in(self) -> bool:
        return self._logged_in

    # ------------------------------------------------------------------
    # Session health & auto-recovery
    # ------------------------------------------------------------------

    async def check_session_health(self) -> bool:
        """Verify the xStation5 page is alive and we're still logged in.

        Returns True if session is healthy, False if recovery is needed.
        Uses URL-based check (safe) + login-form detection only when URL
        looks suspicious. Avoids false positives from submit buttons on
        the trading page.
        """
        if not self._page or not self._logged_in:
            return False

        try:
            # Check if page is still navigable (not crashed/closed)
            url = self._page.url or ""
            if not url or "about:blank" in url:
                self._log("Health check: page is blank — session lost")
                return False

            # If we're on xStation5 URL, session is likely fine.
            # Only flag as unhealthy if we got redirected away from the
            # trading platform (e.g., to a login page on a different URL).
            if "xstation5.xtb.com" in url:
                return True

            # We're on an unexpected URL — check if it's a login page.
            # Use a narrow selector: the login form's email input, which
            # does NOT exist on the trading page (unlike button[type=submit]).
            login_form = self._page.locator(
                "input[type='email'], input[name='email'], "
                "input[placeholder*='email' i], input[placeholder*='ID' i]"
            ).first
            try:
                visible = await login_form.is_visible()
                if visible:
                    self._log("Health check: login form detected — session expired")
                    self._logged_in = False
                    return False
            except Exception:
                pass

            return True
        except Exception as e:
            self._log(f"Health check failed: {e}")
            return False

    async def recover_session(self) -> bool:
        """Attempt to recover a broken session by reloading and re-logging in.

        Returns True if recovery succeeded.
        """
        self._log("Attempting session recovery...")

        if not self._page:
            self._log("Recovery failed: no page object")
            return False

        try:
            # Try reloading the page first
            await self._page.goto("https://xstation5.xtb.com", timeout=NAV_TIMEOUT_MS)
            await self._page.wait_for_timeout(3000)

            # Check if we're back on the trading page (session cookie still valid)
            login_indicator = self._page.locator(
                "button[type='submit'], button:has-text('Log in'), "
                "button:has-text('Zaloguj'), button:has-text('Sign in')"
            ).first
            try:
                await login_indicator.wait_for(state="visible", timeout=5000)
            except Exception:
                # No login button = already logged in after reload
                self._logged_in = True
                self._consecutive_action_failures = 0
                self._log("Session recovered via page reload (cookie still valid)")
                return True

            # Cookie expired — need full re-login
            self._log("Cookie expired, performing full re-login...")
            result = await self.login()
            if result:
                self._consecutive_action_failures = 0
                self._log("Session recovered via re-login")
            return result

        except Exception as e:
            self._log(f"Session recovery failed: {e}")
            await self._screenshot("recovery_failed")
            return False

    async def _ensure_healthy(self) -> bool:
        """Pre-action health gate. Checks session and auto-recovers if needed.

        Returns True if ready to proceed with an action.
        Only triggers recovery when there's clear evidence of session loss
        (redirected away from xStation5, page blank). Does NOT trigger on
        ambiguous states to avoid disrupting active trading.
        """
        if not self._page:
            return False
        if not self._logged_in:
            self._log("Session lost — triggering auto-recovery")
            return await self.recover_session()

        # Quick URL sanity check — don't do full health check on every trade
        try:
            url = self._page.url or ""
            if "xstation5.xtb.com" in url:
                return True
            if not url or "about:blank" in url:
                self._log("Page blank — triggering auto-recovery")
                return await self.recover_session()
        except Exception:
            pass

        return True

    def _record_action_success(self) -> None:
        """Reset failure counter on successful action."""
        self._consecutive_action_failures = 0

    async def _record_action_failure(self, context: str) -> None:
        """Track failures and trigger recovery if threshold exceeded."""
        self._consecutive_action_failures += 1
        if self._consecutive_action_failures >= MAX_ACTION_FAILURES:
            self._log(
                f"{self._consecutive_action_failures} consecutive failures "
                f"(last: {context}) — triggering page recovery"
            )
            await self.recover_session()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _screenshot(self, name: str) -> None:
        if self._page:
            path = f"screenshots/{name}_{int(time.time())}.png"
            Path("screenshots").mkdir(exist_ok=True)
            await self._page.screenshot(path=path)
            log.info("Screenshot saved: %s", path)

    def _log(self, msg: str) -> None:
        log.info(msg)
        self._on_log(msg)


# ------------------------------------------------------------------
# Synchronous wrapper for use from the bridge thread
# ------------------------------------------------------------------

class XTBWebSync:
    """Synchronous wrapper around XTBWeb for use in the bridge thread."""

    def __init__(self, email: str, password: str, account_type: str = "demo",
                 on_log: Callable[[str], None] | None = None):
        self._xtb = XTBWeb(email, password, account_type, on_log)
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def launch(self) -> None:
        self._ensure_loop().run_until_complete(self._xtb.launch())

    def login(self) -> bool:
        return self._ensure_loop().run_until_complete(self._xtb.login())

    def open_trade(self, symbol: str, direction: Direction, volume: float) -> bool:
        return self._ensure_loop().run_until_complete(
            self._xtb.open_trade(symbol, direction, volume)
        )

    @property
    def last_opened_position_id(self) -> str | None:
        return self._xtb._last_opened_position_id

    def close_trade(self, symbol: str, direction: Direction,
                    xtb_position_id: str | None = None) -> bool:
        return self._ensure_loop().run_until_complete(
            self._xtb.close_trade(symbol, direction, xtb_position_id)
        )

    def get_open_positions(self) -> list[dict]:
        return self._ensure_loop().run_until_complete(
            self._xtb.get_open_positions()
        )

    def scrape_open_position_texts(self) -> list[str] | None:
        return self._ensure_loop().run_until_complete(
            self._xtb.scrape_open_position_texts()
        )

    def check_session_health(self) -> bool:
        return self._ensure_loop().run_until_complete(
            self._xtb.check_session_health()
        )

    def recover_session(self) -> bool:
        return self._ensure_loop().run_until_complete(
            self._xtb.recover_session()
        )

    def is_logged_in(self) -> bool:
        return self._xtb.is_logged_in()

    def close(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.run_until_complete(self._xtb.close())
            self._loop.close()
        self._loop = None
