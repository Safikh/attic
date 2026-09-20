# File Formats

This document provides detailed formatting rules and regex patterns for Attic (PKM) vault files.

## `inbox.md`
Append-only file for quick capture. Items must include a timestamp.
- **Format options**:
  - `- YYYY-MM-DD HH:MM: text`
  - `- YYYY-MM-DD HH:MM [tag]: text`
  - `- YYYY-MM-DD HH:MM: text (url)`
- **Example**:
  ```markdown
  - 2026-09-20 14:15 [pkm]: fix the sync bug
  - 2026-09-20 14:16: Read about event loops (https://example.com/loop)
  ```
- **Regex**: `^-\s(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})(?:\s\[([^\]]+)\])?:\s(.+)$`

## `active.md`
Structured focus document. Must maintain exactly three headers, with items beneath them.
- **Headers**:
  - `## In Flight` (requires checkbox format: `- [ ] text`)
  - `## Waiting / Blocked` (standard bullet: `- text`)
  - `## Scratchpad` (standard bullet: `- text`)
- **Example**:
  ```markdown
  # Active Focus
  
  ## In Flight
  - [ ] Implement sync background job [pkm]
  
  ## Waiting / Blocked
  - Wait for design review
  
  ## Scratchpad
  - Dump idea about refactoring cli
  ```

## `log.md`
Audit trail of completed or discarded items.
- **Format**: `- <disposition> [YYYY-MM-DD HH:MM]: text`
- **Dispositions**: `completed`, `dropped`, `not-actionable`
- **Example**:
  ```markdown
  - completed [2026-09-20 10:00]: fixed the sync bug [pkm]
  - dropped [2026-09-19 09:30]: old feature idea
  ```

## `someday.md`
Archive of old inbox items. Uses datestamp headers.
- **Format**:
  ```markdown
  ## Archived YYYY-MM-DD
  
  - 2026-01-01 10:00: Old item
  ```

## `projects/<name>.md`
Template for long-form project notes.
- **Format**:
  ```markdown
  # {name}
  
  ## Context
  <!-- What is this project? Why does it exist? -->
  
  ## Decisions
  <!-- Key decisions and their rationale -->
  
  ## Links
  <!-- Relevant URLs, docs, PRs -->
  
  ## Notes
  <!-- Running notes, scratchpad -->
  ```

## Tag Extraction & Clean Text
- **Tag Extraction**: Pattern `\[tag\]` anywhere in the line text. Excludes structural tags: `[x]`, `[ ]`, `[/]`.
  - Regex: `\[([^\]]+)\]` (excluding "x", " ", "/")
- **Clean Text**: When reading item text, strip these prefixes:
  - `- [ ] `
  - `- [x] `
  - `- `
  - `* `

## Config Schema (`~/.config/pkm/config.toml`)
- **`[settings]`**: `default_vault`, `wip_cap`, `greeting_cooldown_hours`
- **`[vaults.<name>]`**: `path`, `remote` (optional), `alias` (optional)
- **`[sync]`**: `enabled`, `interval_minutes`

**Example**:
```toml
[settings]
default_vault = "personal"
wip_cap = 5
greeting_cooldown_hours = 2

[vaults.personal]
path = "/Users/safiuddinkhaja/notes/personal"
alias = "p"
remote = "git@github.com:user/notes.git"

[sync]
enabled = true
interval_minutes = 30
```
