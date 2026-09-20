"""AI engine module for Attic PKM.

Provides Gemini integration with agentic tool-use, semantic search retrieval,
note summarization, and smart triage.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from rich.console import Console

from pkm import config, store
from pkm.embeddings import EmbeddingIndex
from pkm.models import PkmConfig, Section, VaultConfig

console = Console(stderr=True)

SYSTEM_INSTRUCTION = """You are Attic AI, the grounded, intelligent assistant embedded inside the user's personal action and knowledge workspace.

You have access to tools to query the user's notes across all their vaults:
- Active focus items (In Flight, Waiting/Blocked, Scratchpad)
- Inbox items (recent captures)
- Completed log items (audit history)
- Project notes
- Full-text search and semantic search

Guidelines:
1. Always look up information using the available tools before answering. Do not speculate about notes the user might have.
2. When answering, cite the source vault, file, tag, and date where relevant.
3. Be clear, concise, and structured. Use bullet points and bold highlights for readability.
4. If the user asks what to do next or for suggestions, inspect their active tasks and WIP limits.
"""


def _get_api_key(cfg: PkmConfig) -> str:
    """Resolve API key from environment (preferred) or config."""
    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("PKM_AI_KEY")
    if env_key:
        return env_key

    if cfg.ai.api_key:
        console.print(
            "[dim yellow]Notice: Using API key from ~/.config/pkm/config.toml. "
            "For better security, set GEMINI_API_KEY in your environment.[/dim yellow]"
        )
        return cfg.ai.api_key

    raise ValueError(
        "Gemini API key not found.\n"
        "Please set GEMINI_API_KEY in your environment, or configure it via:\n"
        "  export GEMINI_API_KEY='your-key-here'\n"
        f"in your shell profile or config."
    )


class AiEngine:
    """Manages Gemini LLM operations, function-calling agent loops, and embeddings."""

    def __init__(self, cfg: Optional[PkmConfig] = None):
        self.cfg = cfg or config.load_config()
        self._client = None

    @property
    def client(self):
        """Lazily initialize the Google GenAI client."""
        if self._client is None:
            from google import genai

            api_key = _get_api_key(self.cfg)
            self._client = genai.Client(api_key=api_key)
        return self._client

    def embed(self, texts: list[str]) -> np.ndarray:
        """Generate vector embeddings for a list of text strings."""
        if not texts:
            return np.empty((0, 768), dtype=np.float32)

        client = self.client
        embed_model = self.cfg.ai.embed_model

        console.print(f"[dim]ℹ️ Sending {len(texts)} chunk(s) to Google Gemini API ({embed_model})...[/dim]")

        # Batch in chunks of 50
        batch_size = 50
        all_vecs = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = client.models.embed_content(
                model=embed_model,
                contents=batch,
            )
            for emb in response.embeddings:
                all_vecs.append(emb.values)

        return np.array(all_vecs, dtype=np.float32)

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Run a single-turn completion."""
        from google.genai import types

        client = self.client
        console.print(f"[dim]ℹ️ Sending completion prompt to Google Gemini ({self.cfg.ai.model})...[/dim]")
        cfg = types.GenerateContentConfig(
            system_instruction=system or SYSTEM_INSTRUCTION,
            temperature=0.3,
        )

        response = client.models.generate_content(
            model=self.cfg.ai.model,
            contents=prompt,
            config=cfg,
        )
        return response.text or ""

    def agent_query(
        self,
        question: str,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
    ) -> str:
        """Run an agentic query loop where Gemini uses internal PKM query tools."""
        from google.genai import types

        cfg = self.cfg

        # -------------------------------------------------------------------
        # Define internal query tools
        # -------------------------------------------------------------------
        def search_by_tag(tag: str, vault: Optional[str] = None) -> list[dict]:
            """Search for all items with a specific [tag] across active, inbox, and log files.

            Args:
                tag: The tag name to find (without brackets).
                vault: Optional vault name or alias.
            """
            if on_tool_call:
                on_tool_call("search_by_tag", {"tag": tag, "vault": vault})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            results = []
            for vc in target_vaults:
                items = store.get_project_items(Path(vc.path), tag)
                for it in items:
                    results.append(
                        {
                            "vault": vc.name,
                            "file": it.source_file,
                            "text": it.clean_text,
                            "tag": it.tag,
                        }
                    )
            return results

        def search_text(query: str, vault: Optional[str] = None) -> list[dict]:
            """Full-text keyword search across vault markdown notes.

            Args:
                query: Text pattern to match.
                vault: Optional vault name or alias.
            """
            if on_tool_call:
                on_tool_call("search_text", {"query": query, "vault": vault})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            matches = []
            q_lower = query.lower()

            for vc in target_vaults:
                v_path = Path(vc.path)
                if not v_path.is_dir():
                    continue
                for md_file in sorted(v_path.rglob("*.md")):
                    if md_file.name.endswith((".bak", ".tmp")):
                        continue
                    try:
                        lines = md_file.read_text(encoding="utf-8").splitlines()
                    except (OSError, UnicodeDecodeError):
                        continue
                    for idx, line in enumerate(lines):
                        if q_lower in line.lower():
                            matches.append(
                                {
                                    "vault": vc.name,
                                    "file": str(md_file.relative_to(v_path)),
                                    "line": idx + 1,
                                    "content": line.strip(),
                                }
                            )
                            if len(matches) >= 40:
                                return matches
            return matches

        def get_active_items(vault: Optional[str] = None, section: Optional[str] = None) -> dict:
            """Get currently active items from active.md.

            Args:
                vault: Optional vault name or alias.
                section: Optional section filter: 'flight', 'waiting', or 'scratch'.
            """
            if on_tool_call:
                on_tool_call("get_active_items", {"vault": vault, "section": section})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            output = {}

            sec_filter = None
            if section:
                try:
                    sec_filter = Section(section)
                except ValueError:
                    pass

            for vc in target_vaults:
                v_path = Path(vc.path)
                act_file = v_path / "active.md"
                v_data = {}
                for sec in [Section.FLIGHT, Section.WAITING, Section.SCRATCH]:
                    if sec_filter and sec != sec_filter:
                        continue
                    items = store.read_items(act_file, sec)
                    v_data[sec.value] = [i.clean_text for i in items]
                output[vc.name] = v_data

            return output

        def get_inbox_items(vault: Optional[str] = None, days: Optional[int] = None) -> list[dict]:
            """Get recent notes and captures from inbox.md.

            Args:
                vault: Optional vault name or alias.
                days: Optional age limit in days.
            """
            if on_tool_call:
                on_tool_call("get_inbox_items", {"vault": vault, "days": days})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            results = []
            cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days else None

            for vc in target_vaults:
                inbox_file = Path(vc.path) / "inbox.md"
                items = store.read_items(inbox_file)
                for it in items:
                    if cutoff_str:
                        m = re.match(r"(\d{4}-\d{2}-\d{2})", it.clean_text)
                        if m and m.group(1) < cutoff_str:
                            continue
                    results.append(
                        {
                            "vault": vc.name,
                            "text": it.clean_text,
                            "tag": it.tag,
                        }
                    )
            return results

        def get_log_entries(vault: Optional[str] = None, days: Optional[int] = 7) -> list[dict]:
            """Get completed or logged entries from log.md.

            Args:
                vault: Optional vault name or alias.
                days: Only return items completed in the last N days (default 7).
            """
            if on_tool_call:
                on_tool_call("get_log_entries", {"vault": vault, "days": days})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            results = []
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days else None

            for vc in target_vaults:
                log_file = Path(vc.path) / "log.md"
                items = store.read_items(log_file)
                for it in items:
                    clean = it.clean_text
                    if cutoff_date:
                        m = re.search(r"\[(\d{4}-\d{2}-\d{2})", clean)
                        if m and m.group(1) < cutoff_date:
                            continue
                    results.append(
                        {
                            "vault": vc.name,
                            "text": clean,
                            "tag": it.tag,
                        }
                    )
            return results

        def get_project_note(name: str, vault: Optional[str] = None) -> dict:
            """Retrieve the long-form project markdown note for a given project name/tag.

            Args:
                name: Project name.
                vault: Optional vault name or alias.
            """
            if on_tool_call:
                on_tool_call("get_project_note", {"name": name, "vault": vault})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            for vc in target_vaults:
                proj_file = Path(vc.path) / "projects" / f"{name}.md"
                if proj_file.exists():
                    return {
                        "vault": vc.name,
                        "project": name,
                        "content": proj_file.read_text(encoding="utf-8"),
                    }
            return {"error": f"Project note '{name}.md' not found."}

        def list_all_tags(vault: Optional[str] = None) -> dict[str, int]:
            """List all known tags and their occurrence counts across notes.

            Args:
                vault: Optional vault name or alias.
            """
            if on_tool_call:
                on_tool_call("list_all_tags", {"vault": vault})

            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            all_tags: dict[str, int] = {}
            for vc in target_vaults:
                v_tags = store.get_all_tags(Path(vc.path))
                for t, count in v_tags.items():
                    all_tags[t] = all_tags.get(t, 0) + count
            return all_tags

        def semantic_search(query: str, k: int = 5, vault: Optional[str] = None) -> list[dict]:
            """Perform conceptual semantic similarity search across embedded notes.

            Args:
                query: Conceptual query or sentence.
                k: Number of top results to return (default 5).
                vault: Optional vault name filter.
            """
            if on_tool_call:
                on_tool_call("semantic_search", {"query": query, "k": k, "vault": vault})

            index = EmbeddingIndex(embed_fn=self.embed)
            # Lazy check: ensure index is populated/updated
            target_vaults = [cfg.resolve_vault(vault)] if vault else list(cfg.vaults.values())
            try:
                index.refresh(target_vaults)
                hits = index.search(query=query, k=k, vault=vault)
                return [h.to_dict() for h in hits]
            except Exception as ex:
                return [{"error": f"Semantic search unavailable: {ex}"}]

        tools_list = [
            search_by_tag,
            search_text,
            get_active_items,
            get_inbox_items,
            get_log_entries,
            get_project_note,
            list_all_tags,
            semantic_search,
        ]

        generate_config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
            tools=tools_list,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=10,
            ),
        )

        response = self.client.models.generate_content(
            model=self.cfg.ai.model,
            contents=question,
            config=generate_config,
        )

        return response.text or "No answer generated."
