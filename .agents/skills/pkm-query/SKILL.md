---
name: pkm-query
description: >-
  Use this skill when the user asks about their notes, tasks, or personal knowledge
  managed by Attic (PKM). Covers querying, capturing, triaging, and understanding
  the vault structure. Also use when working within a PKM vault directory.
---

1. **Vault Layout** — Each vault is a directory containing:
   - `active.md` — Three sections: `## In Flight` (checkboxes `- [ ] text [tag]`), `## Waiting / Blocked` (bullets), `## Scratchpad` (bullets)
   - `inbox.md` — Append-only capture: `- YYYY-MM-DD HH:MM [tag]: text`
   - `log.md` — Completion audit trail: `- completed [YYYY-MM-DD HH:MM]: text` or `- dropped [...]` or `- not-actionable [...]`
   - `someday.md` — Archived inbox items under `## Archived YYYY-MM-DD` headers
   - `projects/<name>.md` — Long-form project notes with Context, Decisions, Links, Notes sections

2. **Key CLI Commands for Agents**:
   - `pkm ctx [TAG] [--auto] [--format json] [--stats] [--include-projects]` — Context dump for LLM consumption. Use `--format json` for structured output.
   - `pkm capture [TEXT]... [-t tag] [-v vault] [--clip] [--url]` — Capture to inbox
   - `pkm act [TEXT]... [-v vault] [-s/--scratch] [-b/--blocked] [-a/--flight]` — Add directly to focus sections
   - `pkm project [NAME] [--items] [-v vault]` — Show project notes or list items tagged with a project
   - `pkm search [-v vault]` — Full text search (interactive)
   - `pkm stale [DAYS] [-v vault]` — Find old inbox items
   - `pkm count [-v vault]` — Count items currently In Flight against the WIP cap
   - `pkm suggest "text" [--action capture|promote|tag|archive] [--rationale "why"]` — Stage a suggestion for user review
   - `pkm review-suggestions [-v vault]` — Interactive review of pending suggestions

   **MCP Server**: If the Attic MCP server is registered, tools like `get_inbox`, `get_active`, `get_log`, `search_notes`, `get_stats`, `list_tags`, and `suggest` are available for programmatic access without shelling out to the CLI.

3. **Reading Vault Files Directly** — When CLI isn't available, agents can:
   - Use ripgrep: `rg '\[tagname\]' /path/to/vault/` to find tagged items
   - Use ripgrep: `rg '2026-09-20' /path/to/vault/log.md` to find items by date
   - Read `active.md` directly for current focus items
   - IMPORTANT: No YAML frontmatter in any files. All metadata is inline.

4. **Write Discipline**:
   - ALWAYS prefer `pkm suggest` to stage suggestions for user review (if available)
   - Use `pkm capture` / `pkm act` for direct writes ONLY when user explicitly asks
   - NEVER edit vault .md files with raw file writes (breaks atomic write safety and .bak backups)

5. **Config Location**: `~/.config/pkm/config.toml` — contains vault paths, aliases, WIP cap

For detailed file format specifications and parsing rules, see [references/file-formats.md](./references/file-formats.md).
