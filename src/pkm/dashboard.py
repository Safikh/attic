import os
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from pkm.models import PkmConfig, Section, Item
from pkm.config import get_greeting_stamp_file
from pkm import store

console = Console()

def render_dashboard(config: PkmConfig, greeting: bool = False, full: bool = False) -> None:
    if greeting:
        stamp_file = get_greeting_stamp_file()
        if stamp_file.exists():
            try:
                last_stamp = stamp_file.read_text().strip()
                last_time = datetime.fromisoformat(last_stamp)
                if datetime.now() - last_time < timedelta(hours=config.greeting_cooldown_hours):
                    return
            except Exception:
                pass
        
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text(datetime.now().isoformat())

    inbox_items = 0
    for vault in config.vaults.values():
        try:
            inbox_path = Path(vault.path) / "inbox.md"
            inbox_items += len(store.read_items(inbox_path))
        except Exception:
            pass
            
    if greeting and inbox_items > 0:
        console.print(Panel(
            f"[bold yellow]You have {inbox_items} item(s) in your inbox to triage.[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED
        ))

    width = console.size.width
    wide_mode = width >= 100

    if wide_mode:
        table = Table(box=None, show_header=False, expand=True)
        for _ in config.vaults.values():
            table.add_column(ratio=1)

    panels = []
    
    total_done_today = 0
    total_done_week = 0
    last_completed_text = "None"
    
    for vault in config.vaults.values():
        path = Path(vault.path)
        active_path = path / "active.md"
        log_path = path / "log.md"
        
        try:
            flight_items = store.read_items(active_path, Section.FLIGHT)
        except Exception:
            flight_items = []
            
        try:
            today_count, last_today = store.get_completions_today(log_path)
            total_done_today += today_count
            if last_today:
                last_completed_text = last_today
            total_done_week += store.get_completions_this_week(log_path)
        except Exception:
            pass

        count = len(flight_items)
        header = f"[bold]{vault.name.upper()}[/bold]"
        if count > config.wip_cap:
            header += f" [red]({count}/{config.wip_cap} - OVER CAP!)[/red]"
        else:
            header += f" [blue]({count}/{config.wip_cap})[/blue]"
        
        content = Text()
        if not flight_items:
            content.append("No items in flight.\n", style="dim")
        else:
            for item in flight_items:
                content.append(f"☐ {item.clean_text}\n")
                
        if full:
            try:
                waiting = store.read_items(active_path, Section.WAITING)
                if waiting:
                    content.append("\n[bold yellow]WAITING / BLOCKED[/bold yellow]\n")
                    for w in waiting:
                        content.append(f"☐ {w.clean_text}\n")
                        
                scratch = store.read_items(active_path, Section.SCRATCH)
                if scratch:
                    content.append("\n[bold green]SCRATCHPAD[/bold green]\n")
                    for s in scratch:
                        content.append(f"• {s.clean_text}\n")
            except Exception:
                pass

        panel = Panel(content, title=header, title_align="left", box=box.ROUNDED)
        panels.append(panel)

    if wide_mode and panels:
        table.add_row(*panels)
        console.print(table)
    else:
        for p in panels:
            console.print(p)

    # Dopamine footer
    footer = f"✅ Done today: {total_done_today}  │  🔥 This week: {total_done_week}  │  Last: {last_completed_text}"
    console.print(Panel(footer, border_style="green", box=box.ROUNDED))

def render_top(config: PkmConfig) -> None:
    for vault in config.vaults.values():
        active_path = Path(vault.path) / "active.md"
        try:
            items = store.read_items(active_path, Section.FLIGHT)
            if items:
                console.print(f"🎯 [[blue]{vault.name}[/blue]] {items[0].clean_text}")
        except Exception:
            pass

def render_next(config: PkmConfig) -> None:
    default_vault = config.resolve_vault()
    active_path = Path(default_vault.path) / "active.md"
    try:
        items = store.read_items(active_path, Section.FLIGHT)
        if items:
            console.print(f"🎯 [[blue]{default_vault.name}[/blue]] {items[0].clean_text}")
    except Exception:
        pass

def render_today(vault_path: Path) -> None:
    active_path = vault_path / "active.md"
    log_path = vault_path / "log.md"
    try:
        flight = store.read_items(active_path, Section.FLIGHT)
    except Exception:
        flight = []

    content = Text()
    content.append("[bold blue]ACTIVE FOCUS[/bold blue]\n")
    if flight:
        for f in flight:
            content.append(f"☐ {f.clean_text}\n")
    else:
        content.append("Nothing in flight.\n", style="dim")
        
    content.append("\n[bold green]TODAY'S LOG[/bold green]\n")
    today_items = []
    if log_path.exists():
        today_str = datetime.now().strftime("%Y-%m-%d")
        for item in store.read_items(log_path):
            if today_str in item.clean_text or today_str in item.raw_line:
                today_items.append(item)
                
    if today_items:
        for l in today_items:
            content.append(f"✓ {l.clean_text}\n")
    else:
        content.append("No items logged today.\n", style="dim")
        
    console.print(Panel(content, title=f"Today's Overview ({vault_path.name})", box=box.ROUNDED))

def render_count(vault_path: Path, wip_cap: int) -> None:
    active_path = vault_path / "active.md"
    try:
        flight = store.read_items(active_path, Section.FLIGHT)
        count = len(flight)
    except Exception:
        count = 0
        
    if count > wip_cap:
        console.print(f"[red]⚠️ In Flight: {count} (Capacity exceeded: > {wip_cap} items)[/red]")
    else:
        console.print(f"[blue]In Flight: {count} / {wip_cap}[/blue]")
