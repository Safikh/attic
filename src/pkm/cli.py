"""CLI entrypoint and Typer commands for PKM."""

from __future__ import annotations

import json as json_mod
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from pkm import config, dashboard, store, sync
from pkm.models import Disposition, Item, PkmConfig, Section, VaultConfig

app = typer.Typer(
    name="pkm",
    help="Terminal-native personal knowledge management CLI.",
    no_args_is_help=False,
)
setup_app = typer.Typer(help="Setup wizard and vault management.")
app.add_typer(setup_app, name="setup")

console = Console()


def _get_editor() -> str:
    return os.environ.get("EDITOR", "vim")


def _run_fzf(
    options: list[str],
    prompt: str = "> ",
    multi: bool = False,
    preview: Optional[str] = None,
) -> list[str]:
    """Run fzf with given string options. Returns selected strings."""
    if not shutil.which("fzf"):
        raise typer.BadParameter("fzf is not installed or not in PATH.")

    cmd = ["fzf", "--prompt", prompt, "--height", "50%", "--reverse", "--border"]
    if multi:
        cmd.append("-m")
    if preview:
        cmd.extend(["--preview", preview])

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, _ = proc.communicate(input="\n".join(options))
    if proc.returncode != 0:
        return []
    return [line for line in stdout.splitlines() if line.strip()]


def _select_items_fzf(
    candidates: list[tuple[str, Item, Path]],
    prompt: str = "> ",
    multi: bool = False,
) -> list[tuple[str, Item, Path]]:
    """Run fzf with indexed options to guarantee unambiguous selection of duplicate text items."""
    if not candidates:
        return []

    lines = [f"{i:03d} │ {label}" for i, (label, _, _) in enumerate(candidates)]
    selected_lines = _run_fzf(lines, prompt=prompt, multi=multi)
    if not selected_lines:
        return []

    chosen: list[tuple[str, Item, Path]] = []
    for sel in selected_lines:
        try:
            idx_str = sel.split("│")[0].strip()
            idx = int(idx_str)
            if 0 <= idx < len(candidates):
                chosen.append(candidates[idx])
        except (ValueError, IndexError):
            continue
    return chosen


def _check_wip_capacity(
    active_path: Path,
    incoming_count: int,
    wip_cap: int,
    override: bool = False,
    action_name: str = "add",
) -> bool:
    """Enforce WIP limit before promoting or adding to In Flight."""
    if override:
        return True
    flight_items = store.read_items(active_path, Section.FLIGHT)
    current_count = len(flight_items)
    if current_count + incoming_count > wip_cap:
        console.print(f"\n[bold red]🛑 WIP limit reached ({current_count}/{wip_cap} items In Flight)![/bold red]")
        console.print(f"[red]Cannot {action_name} {incoming_count} item(s) without exceeding capacity.[/red]")
        console.print("[yellow]To protect your focus, finish, block, or drop an active item first:[/yellow]")
        console.print("  • [green]pkm done[/green]     - Mark a task complete")
        console.print("  • [yellow]pkm block[/yellow]    - Move a blocked task to Waiting")
        console.print("  • [dim]pkm drop[/dim]     - Drop a task")
        console.print(f"Or bypass this limit using: [bold cyan]--override[/bold cyan] (or [bold cyan]-f[/bold cyan])\n")
        return False
    return True


def _select_vault_interactive(cfg: PkmConfig) -> VaultConfig:
    """If user didn't specify vault or wants to choose via fzf."""
    vault_names = list(cfg.vaults.keys())
    if not vault_names:
        raise typer.BadParameter("No vaults configured. Run 'pkm setup' first.")
    if len(vault_names) == 1:
        return cfg.vaults[vault_names[0]]

    choices = [f"{v.name} ({v.alias or '-'}) -> {v.path}" for v in cfg.vaults.values()]
    selected = _run_fzf(choices, prompt="Select Vault > ")
    if not selected:
        raise typer.Exit(code=0)
    selected_name = selected[0].split()[0]
    return cfg.vaults[selected_name]


def _resolve_vault_param(cfg: PkmConfig, vault_param: Optional[str]) -> VaultConfig:
    """Resolve vault parameter from flag, or prompt via fzf if empty/requested."""
    if vault_param is None:
        return cfg.resolve_vault()
    if vault_param == "":
        return _select_vault_interactive(cfg)
    try:
        return cfg.resolve_vault(vault_param)
    except KeyError:
        console.print(f"[red]Vault '{vault_param}' not found. Choose one:[/red]")
        return _select_vault_interactive(cfg)


# -----------------------------------------------------------------------------
# Capture
# -----------------------------------------------------------------------------
@app.command("capture")
def capture_cmd(
    text: list[str] = typer.Argument(None, help="Note or task text to capture"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault name or alias"),
    clip: bool = typer.Option(False, "--clip", "-c", help="Capture from clipboard"),
    ctx: bool = typer.Option(False, "--ctx", help="Auto-tag with current git repo name"),
    tag: Optional[str] = typer.Option(None, "-t", "--tag", help="Tag to attach (omit value for fzf picker)"),
    url: Optional[str] = typer.Option(None, "--url", help="Provenance URL"),
):
    """Capture a thought or task to inbox.md. Opens editor if no text provided."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    inbox_path = vault_path / "inbox.md"
    vault_path.mkdir(parents=True, exist_ok=True)

    # Determine tag
    chosen_tag = tag
    if tag == "":  # User specified -t with empty string -> show fzf picker
        tags = store.get_all_tags(vault_path)
        tag_list = list(tags.keys())
        if tag_list:
            sel = _run_fzf(tag_list, prompt="Select Project Tag > ")
            chosen_tag = sel[0] if sel else None
        else:
            chosen_tag = None
    elif not chosen_tag and ctx:
        # Check git repo name
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0 and res.stdout.strip():
            chosen_tag = Path(res.stdout.strip()).name

    body = " ".join(text).strip() if text else ""

    # Clipboard capture
    if clip:
        clip_text = ""
        for clip_cmd in [["pbpaste"], ["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]]:
            if shutil.which(clip_cmd[0]):
                res = subprocess.run(clip_cmd, capture_output=True, text=True)
                if res.returncode == 0 and res.stdout.strip():
                    clip_text = res.stdout.strip()
                    break
        if clip_text:
            body = f"{body} {clip_text}".strip() if body else clip_text
        else:
            console.print("[yellow]Clipboard empty or no clipboard utility found.[/yellow]")

    # If still no content, open editor
    if not body:
        if not inbox_path.exists():
            inbox_path.touch()
        editor = _get_editor()
        subprocess.run([editor, str(inbox_path)])
        return

    # Append to inbox
    if url:
        store.append_to_inbox_with_url(inbox_path, body, url=url, tag=chosen_tag)
    elif chosen_tag:
        store.append_to_inbox_with_tag(inbox_path, body, tag=chosen_tag)
    else:
        store.append_to_inbox(inbox_path, body)

    tag_msg = f" [[cyan]{chosen_tag}[/cyan]]" if chosen_tag else ""
    console.print(f"[green]Captured to {target_vault.name} inbox{tag_msg}:[/green] {body}")


# -----------------------------------------------------------------------------
# Act (Direct add to active.md)
# -----------------------------------------------------------------------------
@app.command("act")
def act_cmd(
    text: list[str] = typer.Argument(None, help="Text to add"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
    scratch: bool = typer.Option(False, "-s", "--scratch", help="Add to Scratchpad"),
    blocked: bool = typer.Option(False, "-b", "--blocked", "-w", "--waiting", help="Add to Waiting/Blocked"),
    flight: bool = typer.Option(False, "-a", "--flight", help="Add to In Flight (default)"),
    override: bool = typer.Option(False, "-f", "--override", help="Bypass WIP limit check"),
):
    """Add directly to active focus sections (In Flight, Waiting, or Scratchpad)."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    active_path = vault_path / "active.md"

    if not text:
        # Open in editor
        if not active_path.exists():
            active_path.touch()
        subprocess.run([_get_editor(), str(active_path)])
        return

    body = " ".join(text).strip()
    target_section = Section.FLIGHT
    as_checkbox = True

    if scratch:
        target_section = Section.SCRATCH
        as_checkbox = False
    elif blocked:
        target_section = Section.WAITING
        as_checkbox = False

    if target_section == Section.FLIGHT:
        if not _check_wip_capacity(active_path, 1, cfg.wip_cap, override, "add"):
            raise typer.Exit(1)

    store.add_item(active_path, body, section=target_section, as_checkbox=as_checkbox)
    console.print(f"[green]Added to {target_section.header} ({target_vault.name}):[/green] {body}")

    if target_section == Section.FLIGHT:
        dashboard.render_count(vault_path, cfg.wip_cap)


# -----------------------------------------------------------------------------
# Unified Move & Shortcuts
# -----------------------------------------------------------------------------
@app.command("move")
def move_cmd(
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
    override: bool = typer.Option(False, "-f", "--override", help="Bypass WIP limit check"),
):
    """Interactive 2-step triage tool: pick items across active/inbox, then pick destination."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)

    inbox_path = vault_path / "inbox.md"
    active_path = vault_path / "active.md"
    log_path = vault_path / "log.md"
    someday_path = vault_path / "someday.md"

    # Collect candidate items
    candidates: list[tuple[str, Item, Path]] = []

    if inbox_path.exists():
        for item in store.read_items(inbox_path):
            candidates.append((f"[inbox]      L{item.line_number + 1:02d} │ {item.clean_text}", item, inbox_path))

    if active_path.exists():
        for item in store.read_items(active_path, Section.FLIGHT):
            candidates.append((f"[in-flight]  L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path))
        for item in store.read_items(active_path, Section.WAITING):
            candidates.append((f"[waiting]    L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path))
        for item in store.read_items(active_path, Section.SCRATCH):
            candidates.append((f"[scratch]    L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path))

    if not candidates:
        console.print("[yellow]No items found to move.[/yellow]")
        return

    chosen_items = _select_items_fzf(
        candidates,
        prompt="Move Item(s) (Tab = select multiple, Enter = confirm) > ",
        multi=True,
    )

    if not chosen_items:
        return

    # Select destination
    destinations = [
        "In Flight",
        "Waiting / Blocked",
        "Scratchpad",
        "Done ✅ (log.md)",
        "Someday 📦 (someday.md)",
    ]
    dest_choice = _run_fzf(destinations, prompt="Select Destination > ")
    if not dest_choice:
        return

    dest = dest_choice[0]
    disposition = Disposition.COMPLETED

    if "Done" in dest:
        disp_choice = _run_fzf(
            ["completed", "dropped", "not-actionable"],
            prompt="Select Done Disposition > ",
        )
        if disp_choice:
            disposition = Disposition(disp_choice[0])

    if "In Flight" in dest:
        if not _check_wip_capacity(active_path, len(chosen_items), cfg.wip_cap, override, "move to In Flight"):
            return

    # Group chosen items by source file for atomic batch moves
    by_src: dict[Path, list[Item]] = {}
    for label, item, src_file in chosen_items:
        by_src.setdefault(src_file, []).append(item)

    count = 0
    for src_file, items_in_src in by_src.items():
        if "In Flight" in dest:
            count += store.move_items(items_in_src, from_file=src_file, to_file=active_path, to_section=Section.FLIGHT)
        elif "Waiting" in dest:
            count += store.move_items(items_in_src, from_file=src_file, to_file=active_path, to_section=Section.WAITING)
        elif "Scratchpad" in dest:
            count += store.move_items(items_in_src, from_file=src_file, to_file=active_path, to_section=Section.SCRATCH)
        elif "Done" in dest:
            count += store.move_items(items_in_src, from_file=src_file, to_file=log_path, disposition=disposition)
        elif "Someday" in dest:
            count += store.move_items(items_in_src, from_file=src_file, to_file=someday_path)

    for label, item, _ in chosen_items:
        console.print(f"[green]Moved:[/green] {item.clean_text} -> [cyan]{dest}[/cyan]")

    console.print(f"[bold green]Successfully moved {count} item(s).[/bold green]")
    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("promote")
def promote_cmd(
    vault: Optional[str] = typer.Option(None, "-v", "--vault"),
    override: bool = typer.Option(False, "-f", "--override", help="Bypass WIP limit check"),
):
    """fzf multi-select inbox items -> In Flight."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    inbox_path = vault_path / "inbox.md"
    active_path = vault_path / "active.md"

    items = store.read_items(inbox_path)
    if not items:
        console.print("[yellow]Inbox is empty.[/yellow]")
        return

    candidates = [(f"L{item.line_number + 1:02d} │ {item.clean_text}", item, inbox_path) for item in items]
    chosen = _select_items_fzf(
        candidates,
        prompt="Promote to In Flight (Tab = select multiple) > ",
        multi=True,
    )
    if not chosen:
        return

    if not _check_wip_capacity(active_path, len(chosen), cfg.wip_cap, override, "promote"):
        return

    items_to_move = [c[1] for c in chosen]
    store.move_items(items_to_move, from_file=inbox_path, to_file=active_path, to_section=Section.FLIGHT)

    for it in items_to_move:
        console.print(f"[green]Promoted:[/green] {it.clean_text}")

    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("done")
def done_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """fzf multi-select In Flight items -> log.md (as completed)."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    active_path = vault_path / "active.md"
    log_path = vault_path / "log.md"

    items = store.read_items(active_path, Section.FLIGHT)
    if not items:
        console.print("[yellow]No In Flight items to mark done.[/yellow]")
        return

    candidates = [(f"L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path) for item in items]
    chosen = _select_items_fzf(
        candidates,
        prompt="Mark Done (Tab = select multiple) > ",
        multi=True,
    )
    if not chosen:
        return

    items_to_move = [c[1] for c in chosen]
    store.move_items(
        items_to_move,
        from_file=active_path,
        to_file=log_path,
        disposition=Disposition.COMPLETED,
    )

    for it in items_to_move:
        console.print(f"[green]Completed:[/green] {it.clean_text}")

    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("drop")
def drop_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """fzf multi-select items -> log.md (as dropped)."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    active_path = vault_path / "active.md"
    log_path = vault_path / "log.md"

    items = store.read_items(active_path)
    if not items:
        console.print("[yellow]No items in active.md to drop.[/yellow]")
        return

    candidates = [(f"[{item.source_section}] L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path) for item in items]
    chosen = _select_items_fzf(
        candidates,
        prompt="Drop Item (Tab = select multiple) > ",
        multi=True,
    )
    if not chosen:
        return

    items_to_move = [c[1] for c in chosen]
    store.move_items(
        items_to_move,
        from_file=active_path,
        to_file=log_path,
        disposition=Disposition.DROPPED,
    )

    for it in items_to_move:
        console.print(f"[yellow]Dropped:[/yellow] {it.clean_text}")


@app.command("block")
def block_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """fzf multi-select In Flight items -> Waiting / Blocked."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    active_path = vault_path / "active.md"

    items = store.read_items(active_path, Section.FLIGHT)
    if not items:
        console.print("[yellow]No In Flight items to block.[/yellow]")
        return

    candidates = [(f"L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path) for item in items]
    chosen = _select_items_fzf(
        candidates,
        prompt="Move to Waiting / Blocked (Tab = select multiple) > ",
        multi=True,
    )
    if not chosen:
        return

    items_to_move = [c[1] for c in chosen]
    store.move_items(items_to_move, from_file=active_path, to_file=active_path, to_section=Section.WAITING)

    for it in items_to_move:
        console.print(f"[yellow]Blocked:[/yellow] {it.clean_text}")

    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("unblock")
def unblock_cmd(
    vault: Optional[str] = typer.Option(None, "-v", "--vault"),
    override: bool = typer.Option(False, "-f", "--override", help="Bypass WIP limit check"),
):
    """fzf multi-select Waiting items -> In Flight."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    active_path = vault_path / "active.md"

    items = store.read_items(active_path, Section.WAITING)
    if not items:
        console.print("[yellow]No waiting items to unblock.[/yellow]")
        return

    candidates = [(f"L{item.line_number + 1:02d} │ {item.clean_text}", item, active_path) for item in items]
    chosen = _select_items_fzf(
        candidates,
        prompt="Unblock to In Flight (Tab = select multiple) > ",
        multi=True,
    )
    if not chosen:
        return

    if not _check_wip_capacity(active_path, len(chosen), cfg.wip_cap, override, "unblock"):
        return

    items_to_move = [c[1] for c in chosen]
    store.move_items(items_to_move, from_file=active_path, to_file=active_path, to_section=Section.FLIGHT)

    for it in items_to_move:
        console.print(f"[green]Unblocked:[/green] {it.clean_text}")

    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("bankrupt")
def bankrupt_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Reset inbox: move all inbox.md items to someday.md archive."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    inbox_path = vault_path / "inbox.md"
    someday_path = vault_path / "someday.md"

    count = store.bankrupt_inbox(inbox_path, someday_path)
    if count == 0:
        console.print(f"[yellow]{target_vault.name} inbox was already empty.[/yellow]")
    else:
        console.print(f"[bold green]Inbox bankruptcy complete![/bold green] Archived {count} item(s) to someday.md.")


# -----------------------------------------------------------------------------
# Unified Projects & Tags
# -----------------------------------------------------------------------------
PROJECT_TEMPLATE = """# {name}

## Context
<!-- What is this project? Why does it exist? -->

## Decisions
<!-- Key decisions and their rationale -->

## Links
<!-- Relevant URLs, docs, PRs -->

## Notes
<!-- Running notes, scratchpad -->
"""


@app.command("project")
def project_cmd(
    name: Optional[str] = typer.Argument(None, help="Project name / tag"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
    items_only: bool = typer.Option(False, "--items", help="Show all items tagged with this project"),
):
    """Unified project and tag hub. View items or edit project notes."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    projects_dir = vault_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    # If project name specified
    if name:
        if items_only:
            items = store.get_project_items(vault_path, name)
            if not items:
                console.print(f"[yellow]No items found tagged with [{name}][/yellow]")
            else:
                table = Table(title=f"Items tagged [{name}] in {target_vault.name}", box=box.ROUNDED)
                table.add_column("Source", style="cyan")
                table.add_column("Text", style="white")
                for it in items:
                    table.add_row(it.source_file, it.clean_text)
                console.print(table)
            return

        # Open / create project file
        proj_file = projects_dir / f"{name}.md"
        if not proj_file.exists():
            proj_file.write_text(PROJECT_TEMPLATE.format(name=name))
            console.print(f"[green]Created project file:[/green] {proj_file}")

        subprocess.run([_get_editor(), str(proj_file)])
        return

    # If no name given, show all projects with counts and note status
    tags = store.get_all_tags(vault_path)
    existing_note_names = {f.stem for f in projects_dir.glob("*.md")}
    all_projects = sorted(set(tags.keys()) | existing_note_names)

    if not all_projects:
        console.print("[yellow]No projects or tags found yet. Tag tasks using -t <name>[/yellow]")
        return

    table = Table(title=f"Projects & Tags ({target_vault.name})", box=box.ROUNDED)
    table.add_column("Project", style="bold cyan")
    table.add_column("Item Count", style="yellow")
    table.add_column("Notes File", style="green")

    for proj in all_projects:
        cnt = tags.get(proj, 0)
        has_notes = "📄 has notes" if proj in existing_note_names else "-"
        table.add_row(proj, f"{cnt} item(s)", has_notes)

    console.print(table)
    console.print("[dim]Run 'pkm project <name>' to view/edit notes, or 'pkm project --items <name>' for tasks.[/dim]")


# -----------------------------------------------------------------------------
# Visibility & Dashboard
# -----------------------------------------------------------------------------
@app.command("dash")
def dash_cmd(
    greeting: bool = typer.Option(False, "--greeting", help="Run in greeting mode (respects 2h cooldown)"),
    full: bool = typer.Option(False, "--full", help="Show Waiting and Scratchpad sections"),
):
    """Side-by-side terminal dashboard with dopamine footer."""
    cfg = config.load_config()
    dashboard.render_dashboard(cfg, greeting=greeting, full=full)


@app.command("next")
def next_cmd():
    """What should I do now? Prints the #1 In Flight item from default vault."""
    cfg = config.load_config()
    dashboard.render_next(cfg)


@app.command("top")
def top_cmd():
    """Print the #1 In Flight item from every configured vault."""
    cfg = config.load_config()
    dashboard.render_top(cfg)


@app.command("today")
def today_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Show active items and completed log entries for today."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    dashboard.render_today(Path(target_vault.path))


@app.command("count")
def count_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Count items currently In Flight against the WIP cap."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    dashboard.render_count(Path(target_vault.path), cfg.wip_cap)


@app.command("search")
def search_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Interactive fuzzy search across all vault markdown files using ripgrep and fzf."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)

    if not shutil.which("fzf"):
        console.print("[red]fzf is required for interactive search.[/red]")
        return

    # Use ripgrep if available
    if shutil.which("rg"):
        rg_cmd = f"rg --line-number --no-heading --color=always '' '{vault_path}'"
    else:
        rg_cmd = f"grep -rn --color=always '' '{vault_path}'"

    fzf_cmd = (
        f"{rg_cmd} | fzf --ansi --delimiter : "
        "--preview 'bat --style=numbers --color=always --highlight-line {2} {1} 2>/dev/null || cat {1}' "
        "--preview-window 'up,60%,border-bottom'"
    )

    proc = subprocess.run(fzf_cmd, shell=True, capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        parts = proc.stdout.strip().split(":")
        if len(parts) >= 2:
            file_match = parts[0]
            line_match = parts[1]
            editor = _get_editor()
            subprocess.run([editor, f"+{line_match}", file_match])


@app.command("random")
def random_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Surface a random item from inbox.md."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    items = store.read_items(Path(target_vault.path) / "inbox.md")
    if not items:
        console.print("[yellow]Inbox is empty.[/yellow]")
        return

    import random

    selected = random.choice(items)
    console.print(Panel(f"🎲 [bold cyan]{selected.clean_text}[/bold cyan]", title="Random Inbox Item", box=box.ROUNDED))


# -----------------------------------------------------------------------------
# Review, Stale, Metrics, Context Export
# -----------------------------------------------------------------------------
@app.command("week")
def week_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Show items completed in the last 7 days."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    log_path = Path(target_vault.path) / "log.md"
    cnt = store.get_completions_this_week(log_path)
    console.print(f"[bold green]Completed in the last 7 days ({target_vault.name}):[/bold green] {cnt}")


@app.command("stale")
def stale_cmd(
    days: int = typer.Argument(7, help="Staleness threshold in days"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault"),
):
    """List inbox items older than N days."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    inbox_path = Path(target_vault.path) / "inbox.md"
    items = store.read_items(inbox_path)

    import re
    from datetime import datetime, timedelta

    cutoff = datetime.now() - timedelta(days=days)
    stale_items = []

    for it in items:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", it.clean_text)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
                if dt < cutoff:
                    stale_items.append(it)
            except ValueError:
                pass

    if not stale_items:
        console.print(f"[green]No inbox items older than {days} day(s).[/green]")
    else:
        table = Table(title=f"Stale Inbox Items (> {days} days)", box=box.ROUNDED)
        table.add_column("Item", style="yellow")
        for it in stale_items:
            table.add_row(it.clean_text)
        console.print(table)


@app.command("review")
def review_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Open inbox.md in editor for manual triage review."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    inbox_path = Path(target_vault.path) / "inbox.md"
    if not inbox_path.exists():
        inbox_path.touch()
    subprocess.run([_get_editor(), "+normal G$", str(inbox_path)])


@app.command("ctx")
def ctx_cmd(
    tag: Optional[str] = typer.Argument(None, help="Filter tag, or --auto"),
    auto: bool = typer.Option(False, "--auto", help="Infer tag from current git repo"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: 'text' or 'json'"),
    stats: bool = typer.Option(False, "--stats", help="Append WIP/inbox/completion metrics"),
    include_projects: bool = typer.Option(False, "--include-projects", help="Append project notes for referenced tags"),
):
    """Export read-only active/inbox/log context for AI agents."""
    import json as json_mod
    cfg = config.load_config()
    filter_tag = tag

    if auto or filter_tag == "--auto":
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            filter_tag = Path(res.stdout.strip()).name
        else:
            filter_tag = None

    if format == "json":
        output = {"vaults": {}, "filter_tag": filter_tag}

        for v_name, vault in cfg.vaults.items():
            v_path = Path(vault.path)
            vault_data = {"path": str(v_path)}
            referenced_tags = set()

            for f_name in ["active.md", "inbox.md", "log.md"]:
                f_path = v_path / f_name
                if not f_path.exists() or f_path.stat().st_size == 0:
                    continue

                items = store.read_items(f_path)
                if filter_tag:
                    items = [
                        i for i in items
                        if i.tag == filter_tag or (filter_tag and filter_tag.lower() in i.clean_text.lower())
                    ]

                if items:
                    vault_data[f_name] = [
                        {
                            "text": i.clean_text,
                            "tag": i.tag,
                            "section": i.source_section.value if isinstance(i.source_section, Section) else str(i.source_section),
                        }
                        for i in items
                    ]
                    for i in items:
                        if i.tag:
                            referenced_tags.add(i.tag)

            if include_projects and referenced_tags:
                projects_data = {}
                for ptag in sorted(referenced_tags):
                    proj_file = v_path / "projects" / f"{ptag}.md"
                    if proj_file.exists():
                        projects_data[ptag] = proj_file.read_text().strip()
                if projects_data:
                    vault_data["projects"] = projects_data

            if stats:
                flight_count = store.count_flight_items(v_path / "active.md")
                inbox_items = store.read_items(v_path / "inbox.md")
                done_today, last_done = store.get_completions_today(v_path / "log.md")
                done_week = store.get_completions_this_week(v_path / "log.md")
                vault_data["stats"] = {
                    "wip_count": flight_count,
                    "wip_cap": cfg.wip_cap,
                    "inbox_count": len(inbox_items),
                    "done_today": done_today,
                    "done_this_week": done_week,
                    "last_completed": last_done,
                }

            if len(vault_data) > 1:  # More than just 'path'
                output["vaults"][v_name] = vault_data

        console.print(json_mod.dumps(output, indent=2, default=str))
    else:
        # Original text output
        for v_name, vault in cfg.vaults.items():
            v_path = Path(vault.path)
            printed_vault = False

            for f_name in ["active.md", "inbox.md", "log.md"]:
                f_path = v_path / f_name
                if not f_path.exists() or f_path.stat().st_size == 0:
                    continue

                content = f_path.read_text()
                if filter_tag:
                    lines = [
                        l
                        for l in content.splitlines()
                        if l.startswith("#") or f"[{filter_tag}]" in l or filter_tag.lower() in l.lower()
                    ]
                    if lines:
                        if not printed_vault:
                            console.print(f"\n=== PKM VAULT: {v_name.upper()} ({v_path}) ===")
                            printed_vault = True
                        console.print(f"--- {f_name} (filtered: {filter_tag}) ---")
                        console.print("\n".join(lines))
                else:
                    if not printed_vault:
                        console.print(f"\n=== PKM VAULT: {v_name.upper()} ({v_path}) ===")
                        printed_vault = True
                    console.print(f"--- {f_name} ---")
                    console.print(content.strip())

            if include_projects and printed_vault:
                projects_dir = v_path / "projects"
                if projects_dir.is_dir():
                    for proj_file in sorted(projects_dir.glob("*.md")):
                        if filter_tag and filter_tag.lower() != proj_file.stem.lower():
                            continue
                        console.print(f"--- projects/{proj_file.name} ---")
                        console.print(proj_file.read_text().strip())

            if stats and printed_vault:
                flight_count = store.count_flight_items(v_path / "active.md")
                inbox_items = store.read_items(v_path / "inbox.md")
                done_today, _ = store.get_completions_today(v_path / "log.md")
                done_week = store.get_completions_this_week(v_path / "log.md")
                console.print(f"--- stats ---")
                console.print(f"WIP: {flight_count}/{cfg.wip_cap} | Inbox: {len(inbox_items)} | Done today: {done_today} | This week: {done_week}")


# -----------------------------------------------------------------------------
# AI Suggestion Staging
# -----------------------------------------------------------------------------
@app.command("suggest")
def suggest_cmd(
    text: list[str] = typer.Argument(None, help="Suggestion text"),
    action: str = typer.Option("capture", "--action", "-a", help="Proposed action: capture, promote, tag, archive"),
    rationale: str = typer.Option("", "--rationale", "-r", help="Why this is suggested"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault"),
):
    """Stage a suggestion for user review (preferred AI write path)."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    suggestions_file = vault_path / "suggestions.md"

    if not text:
        console.print("[yellow]No suggestion text provided.[/yellow]")
        raise typer.Exit(1)

    suggestion_text = " ".join(text)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = []
    if suggestions_file.exists():
        lines = suggestions_file.read_text().splitlines(True)
    else:
        lines = ["# Suggestions\n", "<!-- AI-generated suggestions. Review with `pkm review-suggestions` -->\n", "\n"]

    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    entry_lines = [
        f"\n## {now}\n",
        f"- **{action}**: {suggestion_text}\n",
    ]
    if rationale:
        entry_lines.append(f"  > Rationale: {rationale}\n")

    lines.extend(entry_lines)
    suggestions_file.write_text("".join(lines))
    console.print(f"[green]Suggestion staged in {target_vault.name}/suggestions.md[/green]")
    console.print(f"  [dim]{action}: {suggestion_text}[/dim]")
    console.print(f"  [dim]Review with: pkm review-suggestions -v {target_vault.name}[/dim]")


@app.command("review-suggestions")
def review_suggestions_cmd(
    vault: Optional[str] = typer.Option(None, "-v", "--vault"),
):
    """Interactively review and accept/reject AI suggestions."""
    import re
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    suggestions_file = vault_path / "suggestions.md"

    if not suggestions_file.exists():
        console.print(f"[dim]No suggestions pending in {target_vault.name}.[/dim]")
        return

    content = suggestions_file.read_text()
    # Parse suggestion entries: lines starting with "- **action**: text"
    suggestion_lines = []
    for i, line in enumerate(content.splitlines()):
        m = re.match(r"^- \*\*(\w+)\*\*: (.+)$", line)
        if m:
            suggestion_lines.append({
                "line_index": i,
                "action": m.group(1),
                "text": m.group(2),
                "display": f"[{m.group(1)}] {m.group(2)}",
            })

    if not suggestion_lines:
        console.print(f"[dim]No suggestions pending in {target_vault.name}.[/dim]")
        return

    # Use fzf for multi-select
    fzf_input = "\n".join(f"{i}: {s['display']}" for i, s in enumerate(suggestion_lines))
    selected = _run_fzf(
        fzf_input,
        prompt="Accept suggestions (TAB to select, ENTER to confirm): ",
        multi=True,
    )
    if not selected:
        console.print("[dim]No suggestions accepted.[/dim]")
        return

    accepted_indices = set()
    for sel in selected:
        idx_str = sel.split(":")[0].strip()
        try:
            accepted_indices.add(int(idx_str))
        except ValueError:
            continue

    inbox_path = vault_path / "inbox.md"
    active_path = vault_path / "active.md"

    for idx in sorted(accepted_indices):
        s = suggestion_lines[idx]
        if s["action"] == "capture":
            store.append_to_inbox(inbox_path, s["text"])
            console.print(f"[green]✅ Captured:[/green] {s['text']}")
        elif s["action"] == "promote":
            store.add_item(active_path, s["text"], section=Section.FLIGHT, as_checkbox=True)
            console.print(f"[green]✅ Promoted to In Flight:[/green] {s['text']}")
        elif s["action"] == "tag":
            store.append_to_inbox(inbox_path, s["text"])
            console.print(f"[green]✅ Tagged & captured:[/green] {s['text']}")
        elif s["action"] == "archive":
            console.print(f"[yellow]📦 Archived (no action):[/yellow] {s['text']}")
        else:
            store.append_to_inbox(inbox_path, s["text"])
            console.print(f"[green]✅ Captured (default):[/green] {s['text']}")

    # Remove accepted suggestions from file
    all_lines = content.splitlines(True)
    accepted_line_indices = {suggestion_lines[i]["line_index"] for i in accepted_indices}
    # Also remove the rationale lines (next line starting with "  >")
    remove_indices = set()
    for li in accepted_line_indices:
        remove_indices.add(li)
        # Check if next line is a rationale
        if li + 1 < len(all_lines) and all_lines[li + 1].strip().startswith(">"):
            remove_indices.add(li + 1)

    new_lines = [l for i, l in enumerate(all_lines) if i not in remove_indices]
    # Clean up empty section headers (## timestamp with nothing after)
    cleaned = []
    for i, line in enumerate(new_lines):
        if line.startswith("## "):
            # Check if next non-empty line is another header or EOF
            remaining = [l for l in new_lines[i+1:] if l.strip()]
            if not remaining or remaining[0].startswith("## ") or remaining[0].startswith("# "):
                continue  # Skip orphaned section header
        cleaned.append(line)

    suggestions_file.write_text("".join(cleaned))

    rejected = len(suggestion_lines) - len(accepted_indices)
    if rejected > 0:
        console.print(f"\n[dim]{rejected} suggestion(s) remain for later review.[/dim]")
    else:
        console.print(f"\n[green]All suggestions processed! 🎉[/green]")



# -----------------------------------------------------------------------------
# AI-First Commands: Ask, Index, Summarize, Relate, Triage
# -----------------------------------------------------------------------------
@app.command("ask")
def ask_cmd(
    question: list[str] = typer.Argument(..., help="Question or query for PKM assistant"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
):
    """Ask your PKM questions using an agentic query loop with live retrieval."""
    from rich.markdown import Markdown
    from pkm.ai import AiEngine

    q_str = " ".join(question)
    cfg = config.load_config()

    def on_tool(name: str, args: dict):
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items() if v is not None)
        console.print(f"  [dim cyan]⚡ Tool: {name}({args_str})[/dim cyan]")

    console.print(f"[bold cyan]🤖 Asking Attic AI:[/bold cyan] {q_str}\n")
    try:
        engine = AiEngine(cfg)
        with console.status("[dim]Thinking and gathering notes...[/dim]"):
            answer = engine.agent_query(q_str, on_tool_call=on_tool)
        console.print()
        console.print(Panel(Markdown(answer), title="[bold]Attic AI[/bold]", border_style="cyan", box=box.ROUNDED))
    except Exception as ex:
        console.print(f"[red]Error during AI query:[/red] {ex}")
        raise typer.Exit(1)


@app.command("index")
def index_cmd(
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Vault to index"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force complete rebuild of embedding index"),
):
    """Build or incrementally refresh the SQLite embedding index."""
    from pkm.ai import AiEngine
    from pkm.embeddings import EmbeddingIndex

    cfg = config.load_config()
    target_vaults = [_resolve_vault_param(cfg, vault)] if vault else list(cfg.vaults.values())

    try:
        engine = AiEngine(cfg)
        idx = EmbeddingIndex(embed_fn=engine.embed)
        console.print(f"[cyan]Scanning notes for embeddings {'(rebuild)' if rebuild else '(incremental)'}...[/cyan]")
        count = idx.refresh(target_vaults, force=rebuild, progress_cb=lambda msg: console.print(f"  [dim]{msg}[/dim]"))
        total = idx.count()
        console.print(f"[green]✅ Index update complete: {count} chunks embedded (total in index: {total}).[/green]")
    except Exception as ex:
        console.print(f"[red]Error updating index:[/red] {ex}")
        raise typer.Exit(1)


@app.command("summarize")
def summarize_cmd(
    period: str = typer.Option("week", "--period", "-p", help="Timeframe: day, week, month"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
):
    """Generate a narrative summary of accomplishments and notes using AI."""
    from datetime import timedelta
    from rich.markdown import Markdown
    from pkm.ai import AiEngine

    cfg = config.load_config()
    target_vaults = [_resolve_vault_param(cfg, vault)] if vault else list(cfg.vaults.values())

    days = 1 if period == "day" else (30 if period == "month" else 7)
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    all_completed = []
    for vc in target_vaults:
        log_file = Path(vc.path) / "log.md"
        items = store.read_items(log_file)
        for it in items:
            m = re.search(r"\[(\d{4}-\d{2}-\d{2})", it.clean_text)
            if m and m.group(1) >= cutoff_date:
                all_completed.append(f"[{vc.name}] {it.clean_text}")

    if not all_completed:
        console.print(f"[yellow]No log entries found for the past {period} ({days} days).[/yellow]")
        return

    prompt = (
        f"Here are the completed tasks and log entries from the user's PKM for the last {period} ({days} days):\n\n"
        + "\n".join(f"- {c}" for c in all_completed)
        + "\n\nPlease write a concise, motivating summary of what was accomplished, key themes, and momentum highlights. "
        "Keep it structured and ADHD-friendly with bullet points."
    )

    try:
        engine = AiEngine(cfg)
        with console.status(f"[dim]Summarizing past {period}...[/dim]"):
            summary = engine.complete(prompt)
        console.print(Panel(Markdown(summary), title=f"[bold]Summary ({period.capitalize()})[/bold]", border_style="green", box=box.ROUNDED))
    except Exception as ex:
        console.print(f"[red]Error generating summary:[/red] {ex}")
        raise typer.Exit(1)


@app.command("relate")
def relate_cmd(
    text: list[str] = typer.Argument(..., help="Item text or concept to find relations for"),
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
    k: int = typer.Option(5, "-k", help="Number of related items to surface"),
):
    """Surface semantically related items across all vault notes."""
    from pkm.ai import AiEngine
    from pkm.embeddings import EmbeddingIndex

    cfg = config.load_config()
    query_text = " ".join(text)

    try:
        engine = AiEngine(cfg)
        idx = EmbeddingIndex(embed_fn=engine.embed)
        target_vaults = [_resolve_vault_param(cfg, vault)] if vault else list(cfg.vaults.values())
        idx.refresh(target_vaults)
        hits = idx.search(query=query_text, k=k, vault=vault)

        if not hits:
            console.print("[dim]No related items found.[/dim]")
            return

        table = Table(title=f"Related Notes for: '{query_text}'", box=box.ROUNDED)
        table.add_column("Similarity", style="bold green", justify="right", width=10)
        table.add_column("Vault", style="cyan", width=12)
        table.add_column("Source", style="dim", width=20)
        table.add_column("Content")

        for h in hits:
            score_pct = f"{int(h.score * 100)}%"
            table.add_row(score_pct, h.vault, f"{h.file}:{h.line}", h.text)

        console.print(table)
    except Exception as ex:
        console.print(f"[red]Error finding related items:[/red] {ex}")
        raise typer.Exit(1)


@app.command("triage")
def triage_cmd(
    vault: Optional[str] = typer.Option(None, "-v", "--vault", help="Target vault"),
    smart: bool = typer.Option(True, "--smart/--manual", help="Use AI to recommend actions for inbox items"),
):
    """Triage inbox items with AI-suggested dispositions or manual review."""
    if not smart:
        move_cmd(vault)
        return

    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    inbox_items = store.read_items(vault_path / "inbox.md")

    if not inbox_items:
        console.print(f"[dim]Inbox is empty in vault '{target_vault.name}'. Nothing to triage![/dim]")
        return

    active_items = store.read_items(vault_path / "active.md")
    active_flight = [i.clean_text for i in active_items if i.source_section == Section.FLIGHT]

    inbox_list = "\n".join(f"- ({idx}) {it.clean_text}" for idx, it in enumerate(inbox_items))
    flight_list = "\n".join(f"- {t}" for t in active_flight)

    prompt = f"""You are triaging the user's inbox items into appropriate actions.

Current active In Flight tasks (WIP limit is {cfg.wip_cap}):
{flight_list or "(None)"}

Inbox items to triage:
{inbox_list}

For each numbered item, propose one action:
- PROMOTE: Move to In Flight (only if high priority and fits within WIP cap)
- WAITING: Move to Waiting / Blocked
- ARCHIVE: Move to someday.md (stale or future idea)
- DROP: Discard / log as dropped

Format your response EXACTLY as a JSON array of objects with keys: "index" (int), "action" (string), "rationale" (string).
Example:
[
  {{"index": 0, "action": "PROMOTE", "rationale": "High priority task matching active project"}},
  {{"index": 1, "action": "ARCHIVE", "rationale": "Someday idea"}}
]
"""
    import json as json_mod
    from pkm.ai import AiEngine

    try:
        engine = AiEngine(cfg)
        with console.status("[dim]AI is analyzing inbox items...[/dim]"):
            raw_resp = engine.complete(prompt)

        m = re.search(r"\[.*\]", raw_resp, re.DOTALL)
        if not m:
            console.print("[yellow]Could not parse structured triage recommendations. Raw response:[/yellow]")
            console.print(raw_resp)
            return

        recs = json_mod.loads(m.group(0))
        table = Table(title="AI Triage Recommendations", box=box.ROUNDED)
        table.add_column("#", justify="right", style="dim", width=4)
        table.add_column("Proposed Action", style="bold cyan", width=15)
        table.add_column("Item", style="white")
        table.add_column("Rationale", style="dim")

        rec_map = {}
        for r in recs:
            idx = r.get("index")
            if idx is not None and 0 <= idx < len(inbox_items):
                item_text = inbox_items[idx].clean_text
                action = r.get("action", "ARCHIVE").upper()
                rat = r.get("rationale", "")
                table.add_row(str(idx), action, item_text, rat)
                rec_map[idx] = (action, inbox_items[idx])

        console.print(table)
        console.print("\nOptions: [bold]a[/bold]=apply all, [bold]s[/bold]=stage as suggestions, [bold]q[/bold]=quit")
        choice = typer.prompt("Action", default="s").strip().lower()

        if choice == "a":
            sorted_indices = sorted(rec_map.keys(), reverse=True)
            for idx in sorted_indices:
                act, item = rec_map[idx]
                if act == "PROMOTE":
                    store.move_item(item, vault_path / "inbox.md", vault_path / "active.md", to_section=Section.FLIGHT)
                elif act == "WAITING":
                    store.move_item(item, vault_path / "inbox.md", vault_path / "active.md", to_section=Section.WAITING)
                elif act == "DROP":
                    store.move_item(item, vault_path / "inbox.md", vault_path / "log.md", disposition=Disposition.DROPPED)
                elif act == "ARCHIVE":
                    store.move_item(item, vault_path / "inbox.md", vault_path / "someday.md")
            console.print("[green]✅ Applied all triage actions![/green]")
        elif choice == "s":
            for idx, (act, item) in rec_map.items():
                rat = next((r.get("rationale", "") for r in recs if r.get("index") == idx), "")
                act_name = "promote" if act == "PROMOTE" else ("archive" if act == "ARCHIVE" else "capture")
                suggest_cmd(text=[item.clean_text], action=act_name, rationale=rat, vault=target_vault.name)
            console.print(f"[green]✅ Staged {len(rec_map)} recommendation(s) in suggestions.md. Review anytime with 'pkm review-suggestions'![/green]")
        else:
            console.print("[dim]Aborted triage.[/dim]")
    except Exception as ex:
        console.print(f"[red]Error during smart triage:[/red] {ex}")
        raise typer.Exit(1)


# -----------------------------------------------------------------------------
# Safety: Sync, Undo, Doctor
# -----------------------------------------------------------------------------
@app.command("sync")
def sync_cmd(
    install: bool = typer.Option(False, "--install", help="Install automated background sync schedule"),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove background sync schedule"),
    status: bool = typer.Option(False, "--status", help="Check background sync schedule status"),
    embed: bool = typer.Option(False, "-e", "--embed", help="Refresh AI embeddings after git sync"),
):
    """Sync all vaults with remote git repositories."""
    cfg = config.load_config()

    if status:
        stat = sync.schedule_status()
        console.print(f"Sync schedule status: [bold cyan]{stat}[/bold cyan]")
        return

    if uninstall:
        sync.uninstall_schedule()
        return

    if install:
        sync.install_schedule(cfg)
        return

    sync.sync_all(cfg)

    # Post-sync embedding refresh only if explicitly requested or auto_embed is configured
    if embed or cfg.ai.auto_embed:
        try:
            from pkm.ai import _get_api_key, AiEngine
            from pkm.embeddings import EmbeddingIndex

            _get_api_key(cfg)
            console.print("[dim]🔄 Running post-sync vector embedding refresh...[/dim]")
            idx = EmbeddingIndex(embed_fn=AiEngine(cfg).embed)
            n = idx.refresh(list(cfg.vaults.values()))
            if n > 0:
                console.print(f"[dim]🔄 AI index refreshed: {n} chunks updated.[/dim]")
            else:
                console.print("[dim]🔄 AI index is already up to date.[/dim]")
        except Exception as ex:
            console.print(f"[dim yellow]Notice: Post-sync embedding refresh skipped: {ex}[/dim yellow]")


@app.command("undo")
def undo_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """Restore .bak files created during the most recent operations."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)

    restored = 0
    for bak_file in vault_path.glob("*.bak"):
        orig_file = bak_file.with_suffix("")
        shutil.copy2(bak_file, orig_file)
        console.print(f"[green]Restored:[/green] {orig_file.name} from backup")
        restored += 1

    if restored == 0:
        console.print(f"[yellow]No .bak files found in {vault_path}[/yellow]")


@app.command("doctor")
def doctor_cmd():
    """Diagnose environment dependencies and vault health."""
    table = Table(title="PKM Health Check", box=box.ROUNDED)
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    # Tools check
    tools = [
        ("fzf", "Required for interactive selection & move"),
        ("git", "Required for sync and context tracking"),
        ("rg", "Recommended for fast markdown search"),
        ("bat", "Recommended for syntax preview in search"),
        ("python3", "Required runtime"),
    ]
    for tool, desc in tools:
        found = shutil.which(tool)
        if found:
            table.add_row(f"Tool: {tool}", "[green]✅ Found[/green]", found)
        else:
            status_str = "[red]❌ Missing[/red]" if tool in ("fzf", "git", "python3") else "[yellow]⚠️ Missing[/yellow]"
            table.add_row(f"Tool: {tool}", status_str, desc)

    # Config & Vaults
    cfg = config.load_config()
    table.add_row(
        "Config file",
        "[green]✅ OK[/green]" if config.CONFIG_FILE.exists() else "[yellow]Default/None[/yellow]",
        str(config.CONFIG_FILE),
    )

    for name, v in cfg.vaults.items():
        vp = Path(v.path)
        exists = vp.is_dir()
        git_init = (vp / ".git").is_dir()
        git_label = " (Git repo)" if git_init else " (No git)"
        status_lbl = "[green]✅ OK[/green]" if exists else "[red]❌ Not found[/red]"
        table.add_row(f"Vault: {name}", status_lbl, f"{vp}{git_label}")

    table.add_row("Sync schedule", "[cyan]Status[/cyan]", sync.schedule_status())

    console.print(table)


# -----------------------------------------------------------------------------
# Setup Subcommands
# -----------------------------------------------------------------------------
@setup_app.callback(invoke_without_command=True)
def setup_main(ctx: typer.Context):
    """Run interactive setup wizard."""
    if ctx.invoked_subcommand is not None:
        return

    console.print(Panel("[bold cyan]Welcome to the PKM Setup Wizard![/bold cyan]", box=box.ROUNDED))
    cfg = config.load_config()

    # 1. Shell integration
    shell_path = os.environ.get("SHELL", "")
    rc_file = Path.home() / (".zshrc" if "zsh" in shell_path else ".bashrc")
    alias_file = Path(__file__).resolve().parent.parent.parent / "shell" / "pkm.zsh"

    if rc_file.exists():
        content = rc_file.read_text()
        source_line = f'source "{alias_file}"'
        if source_line not in content and "pkm.zsh" not in content:
            with open(rc_file, "a") as f:
                f.write(f"\n# PKM Shell Integration\n{source_line}\n")
            console.print(f"[green]✅ Added shell integration to {rc_file}[/green]")
        else:
            console.print(f"[dim]Shell integration already present in {rc_file}[/dim]")

    # 2. Vault configuration
    add_vault = typer.confirm("Would you like to configure/add a vault now?", default=True)
    if add_vault:
        v_name = typer.prompt("Vault name", default="personal")
        v_path = typer.prompt("Vault directory path", default=str(Path.home() / "notes" / v_name))
        v_alias = typer.prompt("Short alias (e.g. p or w)", default="p" if v_name == "personal" else "")
        v_remote = typer.prompt("Git remote URL (optional, press Enter to skip)", default="")

        vp = Path(v_path).expanduser()
        vp.mkdir(parents=True, exist_ok=True)
        if v_remote and not (vp / ".git").is_dir():
            if typer.confirm(f"Clone from {v_remote} now?", default=True):
                subprocess.run(["git", "clone", v_remote, str(vp)])

        cfg.vaults[v_name] = VaultConfig(
            name=v_name,
            path=vp,
            alias=v_alias,
            remote=v_remote,
        )
        if not cfg.default_vault or cfg.default_vault not in cfg.vaults:
            cfg.default_vault = v_name

    # 3. Background Sync
    enable_sync = typer.confirm("Enable background sync schedule (every 30 mins)?", default=True)
    if enable_sync:
        cfg.sync_enabled = True
        sync.install_schedule(cfg)

    config.save_config(cfg)
    console.print(f"[bold green]Setup complete! Config saved to {config.CONFIG_FILE}[/bold green]")


@setup_app.command("add-vault")
def add_vault_cmd(
    name: str = typer.Argument(..., help="Vault name"),
    path: str = typer.Argument(..., help="Vault directory path"),
    alias: str = typer.Option("", "--alias", "-a", help="Short alias"),
    remote: str = typer.Option("", "--remote", "-r", help="Git remote URL"),
):
    """Add a new vault to config."""
    cfg = config.load_config()
    vp = Path(path).expanduser()
    vp.mkdir(parents=True, exist_ok=True)

    if remote and not (vp / ".git").is_dir():
        subprocess.run(["git", "clone", remote, str(vp)])

    cfg.vaults[name] = VaultConfig(
        name=name,
        path=vp,
        alias=alias,
        remote=remote,
    )
    config.save_config(cfg)
    console.print(f"[green]Added vault '{name}' ({alias or 'no alias'}) -> {vp}[/green]")


@setup_app.command("agent-tools")
def agent_tools_cmd():
    """Register Attic MCP server and verify agent skill/rules files."""
    import json as json_mod

    project_root = Path(__file__).resolve().parent.parent.parent
    gemini_config_dir = Path.home() / ".gemini" / "config"
    mcp_config_file = gemini_config_dir / "mcp_config.json"

    # 1. Register MCP server in global gemini config
    mcp_entry = {
        "attic": {
            "command": "uv",
            "args": ["run", "--project", str(project_root), "python", "-m", "pkm.mcp_server"],
        }
    }

    existing = {}
    if mcp_config_file.exists():
        try:
            existing = json_mod.loads(mcp_config_file.read_text())
        except (json_mod.JSONDecodeError, OSError):
            pass

    servers = existing.get("mcpServers", {})
    servers.update(mcp_entry)
    existing["mcpServers"] = servers

    gemini_config_dir.mkdir(parents=True, exist_ok=True)
    mcp_config_file.write_text(json_mod.dumps(existing, indent=2) + "\n")
    console.print(f"[green]✅ MCP server registered at {mcp_config_file}[/green]")
    console.print(f"   [dim]Command: uv run --project {project_root} python -m pkm.mcp_server[/dim]")

    # 2. Install skill globally into ~/.gemini/config/skills/pkm-query
    global_skills_dir = gemini_config_dir / "skills" / "pkm-query"
    skill_src_dir = project_root / ".agents" / "skills" / "pkm-query"

    if skill_src_dir.exists():
        global_skills_dir.mkdir(parents=True, exist_ok=True)
        (global_skills_dir / "references").mkdir(parents=True, exist_ok=True)

        shutil.copy2(skill_src_dir / "SKILL.md", global_skills_dir / "SKILL.md")

        ref_src = skill_src_dir / "references"
        if ref_src.exists():
            for ref_file in ref_src.glob("*.md"):
                shutil.copy2(ref_file, global_skills_dir / "references" / ref_file.name)

        console.print(f"[green]✅ Global Skill installed at {global_skills_dir}[/green]")
        console.print("   [dim]Active across all repositories and workspaces on this machine.[/dim]")
    else:
        console.print(f"[yellow]⚠️  Skill source not found at {skill_src_dir}[/yellow]")

    # 3. Append global rule to ~/.gemini/GEMINI.md
    global_gemini_file = Path.home() / ".gemini" / "GEMINI.md"
    rule_marker = "Attic PKM Integration"
    pkm_rule_text = (
        "\n\n# Attic PKM Integration\n"
        "- For personal task tracking and knowledge management, use Attic (pkm).\n"
        "- Use the `pkm-query` skill and Attic MCP tools when interacting with notes or suggesting follow-up tasks.\n"
        "- Always prefer `pkm suggest` to stage suggestions for user review rather than modifying files directly.\n"
    )

    if global_gemini_file.exists():
        existing_rules = global_gemini_file.read_text(encoding="utf-8")
        if rule_marker not in existing_rules:
            with open(global_gemini_file, "a", encoding="utf-8") as f:
                f.write(pkm_rule_text)
            console.print(f"[green]✅ Global Rules appended to {global_gemini_file}[/green]")
        else:
            console.print(f"[dim]Global rules already present in {global_gemini_file}[/dim]")
    else:
        global_gemini_file.parent.mkdir(parents=True, exist_ok=True)
        global_gemini_file.write_text(pkm_rule_text.lstrip(), encoding="utf-8")
        console.print(f"[green]✅ Global Rules created at {global_gemini_file}[/green]")

    # 4. Verify repo rules
    rules_file = project_root / "GEMINI.md"
    if rules_file.exists():
        console.print(f"[green]✅ Repo Rules: GEMINI.md[/green]")
    else:
        console.print(f"[yellow]⚠️  Repo Rules: GEMINI.md (not found)[/yellow]")

    # 5. Verify workspace MCP config
    workspace_mcp = project_root / ".agents" / "mcp_config.json"
    if workspace_mcp.exists():
        console.print(f"[green]✅ Workspace MCP: .agents/mcp_config.json[/green]")
    else:
        console.print(f"[yellow]⚠️  Workspace MCP: .agents/mcp_config.json (not found)[/yellow]")

    console.print(f"\n[bold green]🎉 Attic AI tools, skill, & global rules are now permanently active machine-wide.[/bold green]")


