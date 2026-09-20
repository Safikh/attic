# Attic — Personal Knowledge Management CLI

This is **Attic**, a terminal-native personal knowledge management system.
Notes are stored as plain Markdown files in vault directories.

## Key Architecture Rules
- All note mutations must go through `store.py` atomic writes or CLI commands. Never edit vault `.md` files directly — this bypasses atomic writes and `.bak` backup safety.
- The `pkm suggest` command is the preferred way for AI to propose changes. Users review suggestions interactively.
- Notes use NO YAML frontmatter. All metadata (timestamps, tags, dispositions) is encoded inline within list items.

## Tech Stack
- Python 3.11+, managed with `uv`
- CLI: `typer` + `rich` for terminal UI
- Storage: Plain Markdown files with atomic writes (temp file + `os.replace`)
- Tests: `pytest` in `tests/`, run with `uv run pytest`
- Sync: Git-based, with launchd/cron background scheduling

## Project Structure
- `src/pkm/models.py` — Data models (Item, Section, Disposition, VaultConfig, PkmConfig)
- `src/pkm/store.py` — File I/O, parsing, atomic writes
- `src/pkm/config.py` — Config loading/saving from `~/.config/pkm/config.toml`
- `src/pkm/cli.py` — All Typer CLI commands
- `src/pkm/dashboard.py` — Rich dashboard rendering
- `src/pkm/sync.py` — Git sync engine and OS scheduler
- `shell/pkm.zsh` — Shell aliases and greeting integration
