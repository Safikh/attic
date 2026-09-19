"""Configuration management for PKM.

Handles loading, saving, and providing defaults for ~/.config/pkm/config.toml.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pkm.models import PkmConfig, VaultConfig

CONFIG_DIR = Path.home() / ".config" / "pkm"
CONFIG_FILE = CONFIG_DIR / "config.toml"
CACHE_DIR = Path(
    __import__("os").environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "pkm"


def default_config() -> PkmConfig:
    """Return sensible defaults when no config file exists."""
    return PkmConfig(
        vaults={
            "personal": VaultConfig(
                name="personal",
                path=Path.home() / "notes" / "personal",
                alias="p",
            ),
        },
        default_vault="personal",
    )


def load_config() -> PkmConfig:
    """Load config from disk, falling back to defaults."""
    if not CONFIG_FILE.exists():
        return default_config()

    with open(CONFIG_FILE, "rb") as f:
        raw = tomllib.load(f)

    settings = raw.get("settings", {})
    vaults: dict[str, VaultConfig] = {}

    for name, vault_data in raw.get("vaults", {}).items():
        path_str = vault_data.get("path", f"~/notes/{name}")
        vaults[name] = VaultConfig(
            name=name,
            path=Path(path_str).expanduser(),
            remote=vault_data.get("remote", ""),
            alias=vault_data.get("alias", ""),
        )

    sync_data = raw.get("sync", {})

    return PkmConfig(
        vaults=vaults,
        default_vault=settings.get("default_vault", "personal"),
        wip_cap=settings.get("wip_cap", 5),
        greeting_cooldown_hours=settings.get("greeting_cooldown_hours", 2),
        sync_enabled=sync_data.get("enabled", True),
        sync_interval_minutes=sync_data.get("interval_minutes", 30),
    )


def save_config(config: PkmConfig) -> None:
    """Write config to disk as TOML."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("[settings]")
    lines.append(f'default_vault = "{config.default_vault}"')
    lines.append(f"wip_cap = {config.wip_cap}")
    lines.append(f"greeting_cooldown_hours = {config.greeting_cooldown_hours}")
    lines.append("")

    for name, vault in config.vaults.items():
        lines.append(f"[vaults.{name}]")
        lines.append(f'path = "{vault.path}"')
        if vault.remote:
            lines.append(f'remote = "{vault.remote}"')
        if vault.alias:
            lines.append(f'alias = "{vault.alias}"')
        lines.append("")

    lines.append("[sync]")
    lines.append(f"enabled = {'true' if config.sync_enabled else 'false'}")
    lines.append(f"interval_minutes = {config.sync_interval_minutes}")
    lines.append("")

    CONFIG_FILE.write_text("\n".join(lines))


def get_greeting_stamp_file() -> Path:
    """Return the path for the greeting cooldown timestamp."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "last_greeting"
