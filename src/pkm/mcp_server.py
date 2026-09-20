"""MCP server for Attic PKM.

Exposes read tools, a suggestion-staging tool, and config-gated direct
write tools so AI agents can query and interact with the PKM.

Run with:  python -m pkm.mcp_server
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from pkm import config, store
from pkm.models import Disposition, Section

mcp = FastMCP("Attic PKM")


def _load_cfg():
    """Load the PKM config (fresh on each call for hot-reload)."""
    return config.load_config()


def _item_to_dict(item) -> dict:
    """Serialize an Item to a JSON-friendly dict."""
    return {
        "text": item.clean_text,
        "raw_line": item.raw_line,
        "source_file": item.source_file,
        "source_section": item.source_section.value if isinstance(item.source_section, Section) else str(item.source_section),
        "line_number": item.line_number,
        "tag": item.tag,
    }


# ---------------------------------------------------------------------------
# Read tools — always available
# ---------------------------------------------------------------------------

@mcp.tool
def list_vaults() -> list[dict]:
    """List all configured vaults with their names, aliases, and paths.

    Returns a list of vault objects with name, path, alias, and remote fields.
    """
    cfg = _load_cfg()
    return [
        {
            "name": v.name,
            "path": str(v.path),
            "alias": v.alias,
            "remote": v.remote,
        }
        for v in cfg.vaults.values()
    ]


@mcp.tool
def get_inbox(
    vault: Optional[str] = None,
    tag: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Get inbox items from a vault, optionally filtered by tag or age.

    Args:
        vault: Vault name, alias, or None for default vault.
        tag: Filter to items with this [tag].
        days: Filter to items from the last N days.

    Returns a list of item objects.
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    items = store.read_items(Path(vc.path) / "inbox.md")

    if tag:
        items = [i for i in items if i.tag == tag]

    if days is not None:
        cutoff = datetime.now().strftime("%Y-%m-%d")
        # Parse dates from item text and filter
        from datetime import timedelta
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        filtered = []
        for item in items:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", item.clean_text)
            if m and m.group(1) >= cutoff_date:
                filtered.append(item)
            elif not m:
                # Keep items without parseable dates
                filtered.append(item)
        items = filtered

    return [_item_to_dict(i) for i in items]


@mcp.tool
def get_active(
    vault: Optional[str] = None,
    section: Optional[str] = None,
) -> dict:
    """Get active items from a vault, grouped by section.

    Args:
        vault: Vault name, alias, or None for default vault.
        section: Filter to a specific section: 'flight', 'waiting', or 'scratch'.

    Returns a dict with keys 'flight', 'waiting', 'scratch', each containing
    a list of item objects.
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    active_path = Path(vc.path) / "active.md"

    result = {}
    sections_to_check = [Section.FLIGHT, Section.WAITING, Section.SCRATCH]

    if section:
        try:
            sections_to_check = [Section(section)]
        except ValueError:
            return {"error": f"Unknown section: {section}. Use 'flight', 'waiting', or 'scratch'."}

    for sec in sections_to_check:
        items = store.read_items(active_path, sec)
        result[sec.value] = [_item_to_dict(i) for i in items]

    return result


@mcp.tool
def get_log(
    vault: Optional[str] = None,
    days: Optional[int] = None,
    disposition: Optional[str] = None,
) -> list[dict]:
    """Get log entries (completed/dropped/not-actionable items).

    Args:
        vault: Vault name, alias, or None for default vault.
        days: Filter to entries from the last N days.
        disposition: Filter by disposition: 'completed', 'dropped', or 'not-actionable'.

    Returns a list of item objects.
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    items = store.read_items(Path(vc.path) / "log.md")

    if disposition:
        items = [i for i in items if i.clean_text.startswith(disposition)]

    if days is not None:
        from datetime import timedelta
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        filtered = []
        for item in items:
            m = re.search(r"\[(\d{4}-\d{2}-\d{2})", item.clean_text)
            if m and m.group(1) >= cutoff_date:
                filtered.append(item)
        items = filtered

    return [_item_to_dict(i) for i in items]


@mcp.tool
def get_project_notes(name: str, vault: Optional[str] = None) -> dict:
    """Get project notes and all tagged items for a project.

    Args:
        name: Project/tag name.
        vault: Vault name, alias, or None for default vault.

    Returns a dict with 'note_content' (str or None) and 'tagged_items' (list).
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    vault_path = Path(vc.path)

    # Get project note file
    note_path = vault_path / "projects" / f"{name}.md"
    note_content = None
    if note_path.exists():
        note_content = note_path.read_text()

    # Get all tagged items
    tagged_items = store.get_project_items(vault_path, name)

    return {
        "note_content": note_content,
        "tagged_items": [_item_to_dict(i) for i in tagged_items],
    }


@mcp.tool
def search_notes(query: str, vault: Optional[str] = None) -> list[dict]:
    """Full-text search across vault markdown files.

    Searches for the query string (case-insensitive) in all .md files.

    Args:
        query: Search text.
        vault: Vault name, alias, or None to search all vaults.

    Returns a list of match objects with file, line_number, and line_content.
    """
    cfg = _load_cfg()

    vaults_to_search = []
    if vault:
        vaults_to_search.append(cfg.resolve_vault(vault))
    else:
        vaults_to_search = list(cfg.vaults.values())

    results = []
    query_lower = query.lower()

    for vc in vaults_to_search:
        vault_path = Path(vc.path)
        for md_file in sorted(vault_path.rglob("*.md")):
            try:
                lines = md_file.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines):
                if query_lower in line.lower():
                    results.append({
                        "vault": vc.name,
                        "file": str(md_file.relative_to(vault_path)),
                        "line_number": i + 1,
                        "line_content": line.strip(),
                    })
                    if len(results) >= 50:  # Cap results
                        return results

    return results


@mcp.tool
def get_context(
    tag: Optional[str] = None,
    vault: Optional[str] = None,
) -> dict:
    """Get a full context dump suitable for LLM consumption.

    Equivalent to `pkm ctx`. Returns structured content from active.md,
    inbox.md, and log.md across vaults.

    Args:
        tag: Filter to items matching this tag.
        vault: Vault name, alias, or None for all vaults.
    """
    cfg = _load_cfg()

    vaults_to_dump = []
    if vault:
        vaults_to_dump.append(cfg.resolve_vault(vault))
    else:
        vaults_to_dump = list(cfg.vaults.values())

    context = {}
    for vc in vaults_to_dump:
        vault_path = Path(vc.path)
        vault_data = {}

        for f_name in ["active.md", "inbox.md", "log.md"]:
            f_path = vault_path / f_name
            if not f_path.exists():
                continue
            content = f_path.read_text()
            if tag:
                lines = [
                    l for l in content.splitlines()
                    if l.startswith("#") or f"[{tag}]" in l or tag.lower() in l.lower()
                ]
                if lines:
                    vault_data[f_name] = "\n".join(lines)
            else:
                vault_data[f_name] = content.strip()

        if vault_data:
            context[vc.name] = vault_data

    return context


@mcp.tool
def get_stats(vault: Optional[str] = None) -> dict:
    """Get PKM metrics: WIP count, inbox count, completions today and this week.

    Args:
        vault: Vault name, alias, or None for all vaults.
    """
    cfg = _load_cfg()

    vaults_to_check = []
    if vault:
        vaults_to_check.append(cfg.resolve_vault(vault))
    else:
        vaults_to_check = list(cfg.vaults.values())

    stats = {"wip_cap": cfg.wip_cap, "vaults": {}}

    for vc in vaults_to_check:
        vault_path = Path(vc.path)
        flight_count = store.count_flight_items(vault_path / "active.md")
        inbox_items = store.read_items(vault_path / "inbox.md")
        done_today, last_done = store.get_completions_today(vault_path / "log.md")
        done_week = store.get_completions_this_week(vault_path / "log.md")

        stats["vaults"][vc.name] = {
            "wip_count": flight_count,
            "wip_cap": cfg.wip_cap,
            "over_cap": flight_count > cfg.wip_cap,
            "inbox_count": len(inbox_items),
            "done_today": done_today,
            "done_this_week": done_week,
            "last_completed": last_done,
        }

    return stats


@mcp.tool
def list_tags(vault: Optional[str] = None) -> dict[str, int]:
    """List all tags found across vault files with their occurrence counts.

    Args:
        vault: Vault name, alias, or None for default vault.
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    return store.get_all_tags(Path(vc.path))


@mcp.tool
def get_stale_items(days: int = 7, vault: Optional[str] = None) -> list[dict]:
    """Get inbox items older than N days.

    Args:
        days: Age threshold in days (default 7).
        vault: Vault name, alias, or None for default vault.
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    items = store.read_items(Path(vc.path) / "inbox.md")

    from datetime import timedelta
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    stale = []

    for item in items:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", item.clean_text)
        if m and m.group(1) < cutoff_date:
            stale.append(_item_to_dict(item))

    return stale


# ---------------------------------------------------------------------------
# Suggestion tool — always available
# ---------------------------------------------------------------------------

@mcp.tool
def suggest(
    text: str,
    action: str = "capture",
    rationale: str = "",
    vault: Optional[str] = None,
) -> str:
    """Suggest a note or task for the user to review.

    Instead of writing directly, this stages the suggestion in suggestions.md
    for the user to accept or reject via `pkm review-suggestions`.

    This is the PREFERRED way for AI agents to propose changes.

    Args:
        text: The note or task text to suggest.
        action: What to do if accepted: 'capture' (inbox), 'promote' (flight),
                'tag', or 'archive'.
        rationale: Why you're suggesting this (shown to user during review).
        vault: Vault name, alias, or None for default vault.

    Returns a confirmation message.
    """
    cfg = _load_cfg()
    vc = cfg.resolve_vault(vault)
    vault_path = Path(vc.path)
    suggestions_file = vault_path / "suggestions.md"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = []
    if suggestions_file.exists():
        lines = suggestions_file.read_text().splitlines(True)
    else:
        lines = ["# Suggestions\n", "<!-- AI-generated suggestions. Review with `pkm review-suggestions` -->\n", "\n"]

    # Ensure trailing newline
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    entry_lines = [
        f"\n## {now}\n",
        f"- **{action}**: {text}\n",
    ]
    if rationale:
        entry_lines.append(f"  > Rationale: {rationale}\n")

    lines.extend(entry_lines)

    suggestions_file.write_text("".join(lines))
    return f"Suggestion staged in {vc.name}/suggestions.md. User can review with `pkm review-suggestions`."


# ---------------------------------------------------------------------------
# Direct write tools — config-gated, disabled by default
# ---------------------------------------------------------------------------

def _register_write_tools():
    """Conditionally register write tools if config allows."""
    cfg = _load_cfg()
    if not cfg.mcp_allow_direct_writes:
        return

    @mcp.tool
    def capture_note(
        text: str,
        tag: Optional[str] = None,
        url: Optional[str] = None,
        vault: Optional[str] = None,
    ) -> str:
        """Capture a note to the inbox.

        IMPORTANT: Only use when the user explicitly asks you to add a note.
        For suggestions, use the `suggest` tool instead.

        Args:
            text: The note text.
            tag: Optional [tag] to attach.
            url: Optional URL provenance.
            vault: Vault name, alias, or None for default vault.
        """
        cfg = _load_cfg()
        vc = cfg.resolve_vault(vault)
        inbox_path = Path(vc.path) / "inbox.md"

        if url and tag:
            store.append_to_inbox_with_url(inbox_path, text, url, tag)
        elif tag:
            store.append_to_inbox_with_tag(inbox_path, text, tag)
        else:
            store.append_to_inbox(inbox_path, text)

        return f"Captured to {vc.name}/inbox.md: {text}"

    @mcp.tool
    def add_active_task(
        text: str,
        section: str = "flight",
        vault: Optional[str] = None,
    ) -> str:
        """Add a task directly to active.md.

        IMPORTANT: Only use when the user explicitly asks you to add a task.
        For suggestions, use the `suggest` tool instead.

        Args:
            text: The task text.
            section: Section to add to: 'flight', 'waiting', or 'scratch'.
            vault: Vault name, alias, or None for default vault.
        """
        cfg = _load_cfg()
        vc = cfg.resolve_vault(vault)
        active_path = Path(vc.path) / "active.md"

        try:
            sec = Section(section)
        except ValueError:
            return f"Unknown section: {section}. Use 'flight', 'waiting', or 'scratch'."

        as_checkbox = sec == Section.FLIGHT
        store.add_item(active_path, text, section=sec, as_checkbox=as_checkbox)

        wip = store.count_flight_items(active_path)
        cfg_obj = _load_cfg()
        cap_msg = f" (WIP: {wip}/{cfg_obj.wip_cap})" if sec == Section.FLIGHT else ""
        return f"Added to {vc.name}/active.md [{sec.value}]: {text}{cap_msg}"


# Register write tools at import time based on config
_register_write_tools()


if __name__ == "__main__":
    mcp.run()
