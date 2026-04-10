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
        if not self._logged_in or not self._page:
            self._log("Cannot open trade — not logged in")
            return False

        try:
            self._log(f"Opening {direction.value} {symbol} {volume} lots...")

            # Step 1: Navigate to the instrument
            # Take debug screenshot to see current state
            await self._screenshot(f"before_trade_{symbol}")

            # Clamp volume to xStation5 allowed range
            volume = max(0.01, min(350.0, round(volume, 2)))

            # Click on the instrument tab if already open, or find it in market watch
            tab = self._page.locator(f"span.chart-symbol-label:has-text('{symbol}')").first
            try:
                await tab.click(timeout=3000)
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

            await self._page.wait_for_timeout(500)

            # Step 2: Set the volume in the click-trading bar
            self._log(f"Setting volume to {volume}...")
            volume_input = self._page.locator("input.xs-stepper-input").first
            await volume_input.click(timeout=ACTION_TIMEOUT_MS)
            await volume_input.click(click_count=3)
            await volume_input.press("Backspace")
            await volume_input.type(str(round(volume, 2)), delay=50)
            self._log("Volume set")

            # Step 3: Click the price button (green=BUY/ask, red=SELL/bid)
            if direction == Direction.BUY:
                btn = self._page.locator(".xs-btn-buy").first
            else:
                btn = self._page.locator(".xs-btn-sell").first

            await self._screenshot(f"before_click_{direction.value}_{symbol}")
            self._log(f"Clicking {direction.value} button...")
            await btn.click(timeout=ACTION_TIMEOUT_MS)
            self._log(f"{direction.value} button clicked")
            await self._page.wait_for_timeout(2000)
            await self._screenshot(f"after_click_{direction.value}_{symbol}")

            # Step 4: Confirm the trade if a confirmation dialog appears
            try:
                confirm = self._page.locator(
                    "button:has-text('Confirm'), button:has-text('Potwierdź'), "
                    "button:has-text('OK'), button:has-text('Accept')"
                ).first
                await confirm.click(timeout=3000)
                self._log("Confirmation dialog accepted")
            except Exception:
                pass  # No confirmation dialog

            # Step 5: Close any success notification
            try:
                close_notif = self._page.locator(
                    "button:has-text('Close'), button:has-text('Zamknij'), "
                    "[class*='notification'] button, [class*='toast'] button"
                ).first
                await close_notif.click(timeout=3000)
            except Exception:
                pass

            # Step 6: Verify the trade actually opened
            await self._page.wait_for_timeout(1500)
            no_positions = await self._has_no_open_positions()
            self._log(f"Position check — empty: {no_positions}")
            if no_positions:
                self._log("Trade not confirmed — retrying click...")
                await self._screenshot(f"retry_trade_{symbol}")
                await btn.click(timeout=ACTION_TIMEOUT_MS)
                await self._page.wait_for_timeout(2000)
                await self._screenshot(f"after_retry_{symbol}")

            self._log(f"Trade opened: {direction.value} {symbol} {volume} lots")
            return True

        except Exception as e:
            self._log(f"Failed to open trade {direction.value} {symbol}: {e}")
            await self._screenshot(f"open_error_{symbol}")
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

    async def close_trade(self, symbol: str, direction: Direction) -> bool:
        if not self._logged_in or not self._page:
            self._log("Cannot close trade — not logged in")
            return False

        try:
            self._log(f"Closing {direction.value} {symbol}...")

            # Find the open positions panel/tab
            # xStation5 typically has a "Open positions" tab at the bottom
            positions_tab = self._page.locator(
                "div:has-text('Open positions'), div:has-text('Otwarte pozycje'), "
                "[class*='positions-tab'], [class*='open-positions']"
            ).first
            try:
                await positions_tab.click(timeout=5000)
                await self._page.wait_for_timeout(500)
            except Exception:
                pass  # Tab might already be active

            # Find the row for this specific position
            position_row = self._page.locator(
                f"tr:has-text('{symbol}'), "
                f"[class*='position-row']:has-text('{symbol}'), "
                f"div[class*='position']:has-text('{symbol}')"
            ).first

            # Click close button on this position row
            close_btn = position_row.locator(
                "button:has-text('Close'), button:has-text('Zamknij'), "
                "button[class*='close'], [class*='close-btn'], "
                "span:has-text('×'), button:has-text('X')"
            ).first
            await close_btn.click(timeout=ACTION_TIMEOUT_MS)
            await self._page.wait_for_timeout(500)

            # Confirm close if dialog appears
            try:
                confirm = self._page.locator(
                    "button:has-text('Confirm'), button:has-text('Potwierdź'), "
                    "button:has-text('Close'), button:has-text('Yes'), "
                    "button:has-text('Tak')"
                ).first
                await confirm.click(timeout=5000)
            except Exception:
                pass

            self._log(f"Trade closed: {direction.value} {symbol}")
            return True

        except Exception as e:
            self._log(f"Failed to close trade {symbol}: {e}")
            await self._screenshot(f"close_error_{symbol}")
            return False

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

    def is_logged_in(self) -> bool:
        return self._logged_in

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

    def close_trade(self, symbol: str, direction: Direction) -> bool:
        return self._ensure_loop().run_until_complete(
            self._xtb.close_trade(symbol, direction)
        )

    def get_open_positions(self) -> list[dict]:
        return self._ensure_loop().run_until_complete(
            self._xtb.get_open_positions()
        )

    def is_logged_in(self) -> bool:
        return self._xtb.is_logged_in()

    def close(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.run_until_complete(self._xtb.close())
            self._loop.close()
        self._loop = None
