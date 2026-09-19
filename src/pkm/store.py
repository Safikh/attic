"""Markdown store operations for PKM."""

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pkm.models import Disposition, Item, Section

ACTIVE_TEMPLATE = """# Active Focus

## In Flight

## Waiting / Blocked

## Scratchpad
"""

def _atomic_write(filepath: Path, lines: list[str]) -> None:
    """Safely write lines to filepath via a temporary file."""
    if filepath.exists():
        shutil.copy2(filepath, filepath.with_suffix(filepath.suffix + ".bak"))
    
    tmp_path = filepath.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        f.writelines(lines)
    
    os.replace(tmp_path, filepath)

def read_items(filepath: Path, section: Optional[Section] = None) -> list[Item]:
    """Parse items from a file, optionally filtering by section."""
    if not filepath.exists():
        if filepath.name == "active.md":
            _atomic_write(filepath, ACTIVE_TEMPLATE.splitlines(True))
        else:
            return []

    items = []
    with open(filepath, "r") as f:
        lines = f.readlines()

    current_section = None
    
    for i, line in enumerate(lines):
        if line.startswith("## "):
            header = line.strip().lower()
            current_section = None
            if header == "## in flight":
                current_section = Section.FLIGHT
            elif header == "## waiting / blocked":
                current_section = Section.WAITING
            elif header == "## scratchpad":
                current_section = Section.SCRATCH
            continue
        
        if section and current_section != section:
            continue
            
        if line.lstrip().startswith(("- ", "* ")):
            item = Item(
                raw_line=line.rstrip('\n'),
                line_number=i,
                source_file=filepath.name,
                source_section=current_section or "",
            )
            items.append(item)
            
    return items

def _insert_into_section(lines: list[str], section: Section, new_line: str) -> None:
    """Helper to insert a new line at the end of the specified section."""
    header_to_find = section.header.lower()
    insert_idx = -1
    
    for i, line in enumerate(lines):
        if line.strip().lower() == header_to_find:
            insert_idx = i + 1
            break
            
    if insert_idx == -1:
        lines.append(f"\n{section.header}\n")
        lines.append(new_line)
        return
        
    next_header_idx = len(lines)
    for i in range(insert_idx, len(lines)):
        if lines[i].startswith("## "):
            next_header_idx = i
            break
            
    last_content_idx = insert_idx - 1
    for i in range(insert_idx, next_header_idx):
        if lines[i].strip():
            last_content_idx = i
            
    lines.insert(last_content_idx + 1, new_line)

def add_item(filepath: Path, text: str, section: Section = Section.FLIGHT, as_checkbox: bool = True) -> None:
    """Insert a new item into the file. For active.md, inserts into the specified section."""
    if not filepath.exists() and filepath.name == "active.md":
        _atomic_write(filepath, ACTIVE_TEMPLATE.splitlines(True))
        
    lines = []
    if filepath.exists():
        with open(filepath, "r") as f:
            lines = f.readlines()
            
    if not lines and filepath.name == "active.md":
        lines = ACTIVE_TEMPLATE.splitlines(True)
        
    prefix = "- [ ] " if as_checkbox else "- "
    new_line = f"{prefix}{text}\n"
    
    if filepath.name == "active.md":
        _insert_into_section(lines, section, new_line)
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
        
    _atomic_write(filepath, lines)

def remove_item(filepath: Path, item: Item) -> bool:
    """Remove a specific item by matching both content and line number."""
    if not filepath.exists():
        return False
        
    with open(filepath, "r") as f:
        lines = f.readlines()
        
    if 0 <= item.line_number < len(lines):
        if lines[item.line_number].rstrip('\n') == item.raw_line.rstrip('\n'):
            lines.pop(item.line_number)
            _atomic_write(filepath, lines)
            return True
            
    return False

def move_item(item: Item, from_file: Path, to_file: Path, to_section: Optional[Section] = None, disposition: Optional[Disposition] = None) -> None:
    """Remove from source and add to destination atomically."""
    if from_file == to_file:
        with open(from_file, "r") as f:
            lines = f.readlines()
            
        if not (0 <= item.line_number < len(lines) and lines[item.line_number].rstrip('\n') == item.raw_line.rstrip('\n')):
            return
            
        lines.pop(item.line_number)
        
        text = item.clean_text
        prefix = "- [ ] " if to_section == Section.FLIGHT else "- "
        new_line = f"{prefix}{text}\n"
        
        if to_section:
            _insert_into_section(lines, to_section, new_line)
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(new_line)
            
        _atomic_write(from_file, lines)
    else:
        text = item.clean_text
        if to_file.name == "log.md" and disposition:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            formatted_text = f"{disposition.value} [{now}]: {text}"
            add_item(to_file, formatted_text, as_checkbox=False)
        else:
            add_item(to_file, text, section=to_section or Section.FLIGHT, as_checkbox=(to_section == Section.FLIGHT))
            
        remove_item(from_file, item)

def append_to_inbox(filepath: Path, text: str) -> None:
    """Append a timestamped line to inbox.md."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{now}: {text}"
    add_item(filepath, line, as_checkbox=False)

def append_to_inbox_with_tag(filepath: Path, text: str, tag: str) -> None:
    """Append a timestamped line to inbox.md with a tag."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{now} [{tag}]: {text}"
    add_item(filepath, line, as_checkbox=False)

def append_to_inbox_with_url(filepath: Path, text: str, url: str, tag: Optional[str] = None) -> None:
    """Append a timestamped line to inbox.md with a URL provenance."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tag_str = f" [{tag}]" if tag else ""
    line = f"{now}{tag_str}: {text} ({url})"
    add_item(filepath, line, as_checkbox=False)

def bankrupt_inbox(inbox_path: Path, someday_path: Path) -> int:
    """Move all items from inbox.md to someday.md under a timestamped header."""
    if not inbox_path.exists():
        return 0
        
    items = read_items(inbox_path)
    if not items:
        return 0
        
    someday_lines = []
    if someday_path.exists():
        with open(someday_path, "r") as f:
            someday_lines = f.readlines()
            
    if someday_lines and not someday_lines[-1].endswith("\n"):
        someday_lines[-1] += "\n"
        
    now_date = datetime.now().strftime("%Y-%m-%d")
    someday_lines.append(f"## Archived {now_date}\n\n")
    
    count = 0
    for item in items:
        someday_lines.append(f"{item.raw_line}\n")
        count += 1
        
    if count > 0:
        someday_lines.append("\n")
        
    _atomic_write(someday_path, someday_lines)
    
    with open(inbox_path, "r") as f:
        inbox_lines = f.readlines()
        
    new_inbox_lines = []
    for line in inbox_lines:
        if not line.lstrip().startswith(("- ", "* ")):
            new_inbox_lines.append(line)
            
    _atomic_write(inbox_path, new_inbox_lines)
    return count

def get_all_tags(vault_path: Path) -> dict[str, int]:
    """Scan all .md files in the vault for [tag] patterns and count them."""
    from collections import defaultdict
    tags = defaultdict(int)
    
    for md_file in vault_path.glob("*.md"):
        items = read_items(md_file)
        for item in items:
            if item.tag:
                tags[item.tag] += 1
                
    return dict(tags)

def get_project_items(vault_path: Path, tag: str) -> list[Item]:
    """Get all items tagged with a specific tag across inbox, active, and log."""
    items = []
    for filename in ["inbox.md", "active.md", "log.md"]:
        filepath = vault_path / filename
        if filepath.exists():
            file_items = read_items(filepath)
            for item in file_items:
                if item.tag == tag:
                    items.append(item)
    return items

def count_flight_items(filepath: Path) -> int:
    """Count items in the In Flight section of active.md."""
    return len(read_items(filepath, Section.FLIGHT))

def get_completions_today(log_path: Path) -> tuple[int, Optional[str]]:
    """Count items completed today, returning the count and the last completed text."""
    if not log_path.exists():
        return 0, None
        
    items = read_items(log_path)
    count = 0
    last_text = None
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    for item in items:
        text = item.clean_text
        if text.startswith(Disposition.COMPLETED.value):
            m = re.search(r"\[(.*?)\]", text)
            if m:
                date_str = m.group(1)
                if date_str.startswith(today):
                    count += 1
                    parts = text.split("]: ", 1)
                    if len(parts) == 2:
                        last_text = parts[1]
                    else:
                        last_text = text
    return count, last_text

def get_completions_this_week(log_path: Path) -> int:
    """Count completed items in the last 7 days."""
    if not log_path.exists():
        return 0
        
    items = read_items(log_path)
    count = 0
    
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    
    for item in items:
        text = item.clean_text
        if text.startswith(Disposition.COMPLETED.value):
            m = re.search(r"\[(.*?)\]", text)
            if m:
                date_str = m.group(1)
                try:
                    if len(date_str) >= 10:
                        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                        if dt.date() >= week_ago.date():
                            count += 1
                except ValueError:
                    pass
    return count
