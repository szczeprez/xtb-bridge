"""
XTB Bridge — build script
Creates a standalone Windows distribution in dist/xtb_bridge/

Usage:
    python build.py            # full build
    python build.py --no-zip   # skip creating the .zip archive
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST_DIR = ROOT / "dist" / "xtb_bridge"
BROWSERS_DEST = DIST_DIR / "_playwright_browsers"

PYINSTALLER_MIN_VERSION = (6, 12)


# ── Helpers ─────────────────────────────────────────────────────────────────────

def run(cmd: list[str], **kwargs) -> None:
    print(f"\n>>> {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"\nERROR: command failed with exit code {result.returncode}")
        sys.exit(result.returncode)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        from PyInstaller import __version__ as ver
        parts = tuple(int(x) for x in ver.split(".")[:2])
        if parts >= PYINSTALLER_MIN_VERSION:
            print(f"PyInstaller {ver} — OK")
            return
        print(f"PyInstaller {ver} is too old (need >= {'.'.join(map(str, PYINSTALLER_MIN_VERSION))}), upgrading...")
    except ImportError:
        print("PyInstaller not found, installing...")

    run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.12"])


def find_playwright_browsers_source() -> Path:
    """Return the ms-playwright directory on this machine."""
    import os as _os
    custom = _os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom:
        p = Path(custom)
    else:
        p = Path.home() / "AppData" / "Local" / "ms-playwright"
    if not p.exists():
        print(
            f"\nERROR: Playwright browsers not found at {p}\n"
            "Run:  python -m playwright install chromium\n"
        )
        sys.exit(1)
    return p


def find_chromium_dir(browsers_root: Path) -> Path:
    """Return the chromium-XXXX directory (not headless_shell)."""
    candidates = [
        d for d in browsers_root.iterdir()
        if d.is_dir() and d.name.startswith("chromium-") and "headless" not in d.name
    ]
    if not candidates:
        print(f"\nERROR: No Chromium found in {browsers_root}\n"
              "Run:  python -m playwright install chromium\n")
        sys.exit(1)
    return sorted(candidates)[-1]  # latest if multiple


def copy_chromium(browsers_root: Path, chromium_dir: Path) -> None:
    dest = BROWSERS_DEST / chromium_dir.name
    if dest.exists():
        print(f"Chromium already copied ({dest.name}), skipping.")
        return
    size_mb = sum(f.stat().st_size for f in chromium_dir.rglob("*") if f.is_file()) // (1024 * 1024)
    print(f"\nCopying Chromium ({size_mb} MB) → {dest}")
    print("This may take a minute...")
    shutil.copytree(chromium_dir, dest)
    print("Chromium copied.")


def copy_runtime_files() -> None:
    """Copy config.toml template and other user-facing files next to the exe."""
    config_src = ROOT / "config.toml"
    config_dst = DIST_DIR / "config.toml"
    if config_src.exists() and not config_dst.exists():
        shutil.copy2(config_src, config_dst)
        print(f"Copied config.toml → {config_dst}")

    readme_src = ROOT / "README.md"
    if readme_src.exists():
        shutil.copy2(readme_src, DIST_DIR / "README.md")


def write_launcher_bat() -> None:
    """Write a start.bat next to the exe (some users prefer double-clicking .bat)."""
    bat = DIST_DIR / "start.bat"
    bat.write_text(
        '@echo off\n'
        'cd /d "%~dp0"\n'
        'start "" "xtb_bridge.exe"\n',
        encoding="utf-8",
    )
    print(f"Written {bat}")


def zip_distribution(no_zip: bool) -> None:
    if no_zip:
        return
    zip_path = ROOT / "dist" / "xtb_bridge_windows.zip"
    print(f"\nZipping distribution → {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in DIST_DIR.rglob("*"):
            if f.is_file():
                zf.write(f, Path("xtb_bridge") / f.relative_to(DIST_DIR))
    size_mb = zip_path.stat().st_size // (1024 * 1024)
    print(f"Archive created: {zip_path.name} ({size_mb} MB)")


# ── Main ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    no_zip = "--no-zip" in sys.argv

    print("=" * 60)
    print("XTB Bridge — Windows build")
    print("=" * 60)

    # 1. Python version check
    if sys.version_info < (3, 11):
        print(f"ERROR: Python 3.11+ required (found {sys.version})")
        sys.exit(1)
    print(f"Python {sys.version.split()[0]} — OK")

    # 2. Ensure PyInstaller
    ensure_pyinstaller()

    # 3. Find Playwright browsers
    browsers_root = find_playwright_browsers_source()
    chromium_dir = find_chromium_dir(browsers_root)
    print(f"Chromium: {chromium_dir.name}")

    # 4. Run PyInstaller
    run([
        sys.executable, "-m", "PyInstaller",
        "xtb_bridge.spec",
        "--noconfirm",
        "--workpath", str(ROOT / "build"),
        "--distpath", str(ROOT / "dist"),
    ], cwd=ROOT)

    # 5. Copy Chromium into the dist folder
    copy_chromium(browsers_root, chromium_dir)

    # 6. Copy runtime files (config.toml, README)
    copy_runtime_files()

    # 7. Write start.bat
    write_launcher_bat()

    # 8. Optional: zip the whole dist folder
    zip_distribution(no_zip)

    print("\n" + "=" * 60)
    print("BUILD COMPLETE")
    print(f"  Distribution folder: {DIST_DIR}")
    if not no_zip:
        print(f"  Archive:             {ROOT / 'dist' / 'xtb_bridge_windows.zip'}")
    print()
    print("IMPORTANT: The target machine still needs MetaTrader 5")
    print("terminal installed (the bridge reads positions from it).")
    print("=" * 60)


if __name__ == "__main__":
    main()
