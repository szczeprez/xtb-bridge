from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = Path("config.toml")
MAPPING_FILE = Path("mapping.json")


@dataclass
class Config:
    # MT5
    mt5_terminal_path: str | None = None

    # XTB
    xtb_email: str = ""
    xtb_password: str = ""
    xtb_account_type: str = "demo"  # "real" or "demo"

    # Bridge
    pairs: list[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "GOLD"])
    symbol_map: dict[str, str] = field(
        default_factory=lambda: {
            "EURUSD": "EURUSD",
            "GBPUSD": "GBPUSD",
            "GOLD": "XAUUSD",
        }
    )
    lot_ratio: float = 0.5
    poll_interval_ms: int = 500
    reverse_mode: bool = False

    @property
    def xtb_url(self) -> str:
        return "https://xstation5.xtb.com"

    def map_symbol(self, mt5_symbol: str) -> str | None:
        return self.symbol_map.get(mt5_symbol)

    def validate(self) -> list[str]:
        errors = []
        if not self.xtb_email:
            errors.append("XTB email is required")
        if not self.xtb_password:
            errors.append("XTB password is required")
        if self.xtb_account_type not in ("real", "demo"):
            errors.append("XTB account type must be 'real' or 'demo'")
        if not self.pairs:
            errors.append("At least one currency pair is required")
        if self.lot_ratio <= 0:
            errors.append("Lot ratio must be > 0")
        if self.poll_interval_ms < 100:
            errors.append("Poll interval must be >= 100ms")
        for pair in self.pairs:
            if pair not in self.symbol_map:
                errors.append(f"Symbol map missing entry for {pair}")
        return errors


def load_config(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        return Config()

    with open(path, "rb") as f:
        data = tomllib.load(f)

    mt5 = data.get("mt5", {})
    xtb = data.get("xtb", {})
    bridge = data.get("bridge", {})
    symbols = data.get("symbols", {})

    return Config(
        mt5_terminal_path=mt5.get("terminal_path"),
        xtb_email=xtb.get("email", ""),
        xtb_password=xtb.get("password", ""),
        xtb_account_type=xtb.get("account_type", "demo"),
        pairs=bridge.get("pairs", ["EURUSD", "GBPUSD", "GOLD"]),
        symbol_map=symbols if symbols else {
            "EURUSD": "EURUSD",
            "GBPUSD": "GBPUSD",
            "GOLD": "XAUUSD",
        },
        lot_ratio=bridge.get("lot_ratio", 0.5),
        poll_interval_ms=bridge.get("poll_interval_ms", 500),
        reverse_mode=bridge.get("reverse_mode", False),
    )


def save_mapping(mapping_dict: dict, path: Path = MAPPING_FILE) -> None:
    with open(path, "w") as f:
        json.dump(mapping_dict, f, indent=2)


def load_mapping(path: Path = MAPPING_FILE) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)
