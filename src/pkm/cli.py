"""CLI entrypoint and Typer commands for PKM."""

from __future__ import annotations

import os
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
            candidates.append((f"[inbox]      {item.clean_text}", item, inbox_path))

    if active_path.exists():
        for item in store.read_items(active_path, Section.FLIGHT):
            candidates.append((f"[in-flight]  {item.clean_text}", item, active_path))
        for item in store.read_items(active_path, Section.WAITING):
            candidates.append((f"[waiting]    {item.clean_text}", item, active_path))
        for item in store.read_items(active_path, Section.SCRATCH):
            candidates.append((f"[scratch]    {item.clean_text}", item, active_path))

    if not candidates:
        console.print("[yellow]No items found to move.[/yellow]")
        return

    item_strings = [c[0] for c in candidates]
    selected_strings = _run_fzf(
        item_strings,
        prompt="Move Item(s) (Tab = select multiple, Enter = confirm) > ",
        multi=True,
    )

    if not selected_strings:
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

    # Execute moves in reverse line order to preserve indices when modifying the same file
    chosen_items = [c for c in candidates if c[0] in selected_strings]
    # Sort descending by line_number to avoid shifting index bugs
    chosen_items.sort(key=lambda c: c[1].line_number, reverse=True)

    count = 0
    for label, item, src_file in chosen_items:
        if "In Flight" in dest:
            store.move_item(item, from_file=src_file, to_file=active_path, to_section=Section.FLIGHT)
        elif "Waiting" in dest:
            store.move_item(item, from_file=src_file, to_file=active_path, to_section=Section.WAITING)
        elif "Scratchpad" in dest:
            store.move_item(item, from_file=src_file, to_file=active_path, to_section=Section.SCRATCH)
        elif "Done" in dest:
            store.move_item(
                item,
                from_file=src_file,
                to_file=log_path,
                disposition=disposition,
            )
        elif "Someday" in dest:
            store.move_item(item, from_file=src_file, to_file=someday_path)

        console.print(f"[green]Moved:[/green] {item.clean_text} -> [cyan]{dest}[/cyan]")
        count += 1

    console.print(f"[bold green]Successfully moved {count} item(s).[/bold green]")
    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("promote")
def promote_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
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

    item_map = {item.clean_text: item for item in items}
    selected = _run_fzf(
        list(item_map.keys()),
        prompt="Promote to In Flight (Tab = select multiple) > ",
        multi=True,
    )
    if not selected:
        return

    # Sort descending by line number
    items_to_move = [item_map[s] for s in selected]
    items_to_move.sort(key=lambda x: x.line_number, reverse=True)

    for it in items_to_move:
        store.move_item(it, from_file=inbox_path, to_file=active_path, to_section=Section.FLIGHT)
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

    item_map = {item.clean_text: item for item in items}
    selected = _run_fzf(
        list(item_map.keys()),
        prompt="Mark Done (Tab = select multiple) > ",
        multi=True,
    )
    if not selected:
        return

    items_to_move = [item_map[s] for s in selected]
    items_to_move.sort(key=lambda x: x.line_number, reverse=True)

    for it in items_to_move:
        store.move_item(
            it,
            from_file=active_path,
            to_file=log_path,
            disposition=Disposition.COMPLETED,
        )
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

    item_map = {f"[{item.source_section}] {item.clean_text}": item for item in items}
    selected = _run_fzf(
        list(item_map.keys()),
        prompt="Drop Item (Tab = select multiple) > ",
        multi=True,
    )
    if not selected:
        return

    items_to_move = [item_map[s] for s in selected]
    items_to_move.sort(key=lambda x: x.line_number, reverse=True)

    for it in items_to_move:
        store.move_item(
            it,
            from_file=active_path,
            to_file=log_path,
            disposition=Disposition.DROPPED,
        )
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

    item_map = {item.clean_text: item for item in items}
    selected = _run_fzf(
        list(item_map.keys()),
        prompt="Move to Waiting / Blocked (Tab = select multiple) > ",
        multi=True,
    )
    if not selected:
        return

    items_to_move = [item_map[s] for s in selected]
    items_to_move.sort(key=lambda x: x.line_number, reverse=True)

    for it in items_to_move:
        store.move_item(it, from_file=active_path, to_file=active_path, to_section=Section.WAITING)
        console.print(f"[yellow]Blocked:[/yellow] {it.clean_text}")

    dashboard.render_count(vault_path, cfg.wip_cap)


@app.command("unblock")
def unblock_cmd(vault: Optional[str] = typer.Option(None, "-v", "--vault")):
    """fzf multi-select Waiting items -> In Flight."""
    cfg = config.load_config()
    target_vault = _resolve_vault_param(cfg, vault)
    vault_path = Path(target_vault.path)
    active_path = vault_path / "active.md"

    items = store.read_items(active_path, Section.WAITING)
    if not items:
        console.print("[yellow]No waiting items to unblock.[/yellow]")
        return

    item_map = {item.clean_text: item for item in items}
    selected = _run_fzf(
        list(item_map.keys()),
        prompt="Unblock to In Flight (Tab = select multiple) > ",
        multi=True,
    )
    if not selected:
        return

    items_to_move = [item_map[s] for s in selected]
    items_to_move.sort(key=lambda x: x.line_number, reverse=True)

    for it in items_to_move:
        store.move_item(it, from_file=active_path, to_file=active_path, to_section=Section.FLIGHT)
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
):
    """Export read-only active/inbox/log context for AI agents."""
    cfg = config.load_config()
    filter_tag = tag

    if auto or filter_tag == "--auto":
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            filter_tag = Path(res.stdout.strip()).name
        else:
            filter_tag = None

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


# -----------------------------------------------------------------------------
# Safety: Sync, Undo, Doctor
# -----------------------------------------------------------------------------
@app.command("sync")
def sync_cmd(
    install: bool = typer.Option(False, "--install", help="Install automated background sync schedule"),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove background sync schedule"),
    status: bool = typer.Option(False, "--status", help="Check background sync schedule status"),
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
