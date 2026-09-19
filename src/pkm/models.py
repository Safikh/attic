"""Data models for PKM."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Section(str, Enum):
    """Sections within active.md."""

    FLIGHT = "flight"
    WAITING = "waiting"
    SCRATCH = "scratch"

    @property
    def header(self) -> str:
        return {
            Section.FLIGHT: "## In Flight",
            Section.WAITING: "## Waiting / Blocked",
            Section.SCRATCH: "## Scratchpad",
        }[self]


class Disposition(str, Enum):
    """How an item exited active circulation."""

    COMPLETED = "completed"
    DROPPED = "dropped"
    NOT_ACTIONABLE = "not-actionable"


@dataclass
class Item:
    """A single note/task line with its raw text and location metadata."""

    raw_line: str
    line_number: int  # 0-indexed position in the source file
    source_file: str = ""  # which file this came from
    source_section: Section | str = ""  # which section (for active.md items)

    @property
    def clean_text(self) -> str:
        """Strip bullet markers and checkboxes to get the core text."""
        text = self.raw_line.strip()
        # Strip leading bullet
        for prefix in ("- [ ] ", "- [x] ", "- ", "* "):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        return text.strip()

    @property
    def tag(self) -> str | None:
        """Extract [tag] if present."""
        text = self.clean_text
        # Look for [tag] pattern — could be anywhere in the line
        import re

        m = re.search(r"\[([^\]]+)\]", text)
        if m:
            val = m.group(1)
            # Exclude checkbox markers
            if val not in ("x", " ", "/"):
                return val
        return None


@dataclass
class VaultConfig:
    """Configuration for a single vault."""

    name: str
    path: Path
    remote: str = ""
    alias: str = ""


@dataclass
class PkmConfig:
    """Global PKM configuration."""

    vaults: dict[str, VaultConfig] = field(default_factory=dict)
    default_vault: str = "personal"
    wip_cap: int = 5
    greeting_cooldown_hours: int = 2
    sync_enabled: bool = True
    sync_interval_minutes: int = 30

    def resolve_vault(self, name_or_alias: str | None = None) -> VaultConfig:
        """Resolve a vault name or alias to a VaultConfig.

        Args:
            name_or_alias: Full name, alias, or None for default.

        Returns:
            The matching VaultConfig.

        Raises:
            KeyError: If no matching vault is found.
        """
        if name_or_alias is None:
            if self.default_vault in self.vaults:
                return self.vaults[self.default_vault]
            raise KeyError(f"Default vault '{self.default_vault}' not configured")

        # Exact name match
        if name_or_alias in self.vaults:
            return self.vaults[name_or_alias]

        # Alias match
        for vault in self.vaults.values():
            if vault.alias and vault.alias == name_or_alias:
                return vault

        # Prefix match on name
        matches = [v for k, v in self.vaults.items() if k.startswith(name_or_alias)]
        if len(matches) == 1:
            return matches[0]

        # Prefix match on alias
        matches = [
            v
            for v in self.vaults.values()
            if v.alias and v.alias.startswith(name_or_alias)
        ]
        if len(matches) == 1:
            return matches[0]

        raise KeyError(f"No vault matches '{name_or_alias}'")
