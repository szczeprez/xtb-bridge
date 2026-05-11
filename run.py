"""XTB Bridge launcher — entry point for both dev and frozen (PyInstaller) mode."""
import os
import sys
from pathlib import Path


def _setup_frozen_env() -> None:
    """Configure runtime environment when running as a PyInstaller bundle."""
    bundle = Path(sys.executable).parent

    # Make all relative paths (config.toml, mapping.json, logs, screenshots)
    # resolve next to the exe rather than wherever the user double-clicked from.
    os.chdir(bundle)

    # Tell Playwright where to find the bundled Chromium browser.
    # The build script copies it to _playwright_browsers/ inside the dist folder.
    browsers_dir = bundle / "_playwright_browsers"
    if browsers_dir.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)


if getattr(sys, "frozen", False):
    _setup_frozen_env()

# Import after env is configured so Playwright picks up PLAYWRIGHT_BROWSERS_PATH
from xtb_bridge.main import main  # noqa: E402

if __name__ == "__main__":
    main()
