"""SQLite-backed vector index with numpy similarity for Attic PKM.

Stores item chunks and embeddings in SQLite (BLOB storage), tracking file
mtimes for incremental updates. Computes cosine similarity across vectors
using numpy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from pkm import config
from pkm.models import Item, Section, VaultConfig
from pkm.store import read_items

DEFAULT_DB_PATH = config.CACHE_DIR / "embeddings.db"


@dataclass
class SearchHit:
    """A semantic search result item."""

    text: str
    file: str
    line: int
    vault: str
    tag: Optional[str]
    score: float

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "file": self.file,
            "line": self.line,
            "vault": self.vault,
            "tag": self.tag,
            "score": round(float(self.score), 4),
        }


def _init_db(conn: sqlite3.Connection) -> None:
    """Create schema if not existing."""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_meta (
                vault TEXT,
                file TEXT,
                mtime REAL,
                PRIMARY KEY (vault, file)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault TEXT,
                file TEXT,
                line INTEGER,
                text TEXT,
                tag TEXT,
                mtime REAL,
                embedding BLOB
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_vault_file ON items(vault, file)")


def extract_chunks_from_file(vault_path: Path, file_path: Path) -> list[tuple[int, str, Optional[str]]]:
    """Extract indexable chunks from a markdown file.

    Returns a list of (line_number, text, tag) tuples.
    """
    rel_file = str(file_path.relative_to(vault_path))
    chunks: list[tuple[int, str, Optional[str]]] = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    lines = content.splitlines()

    # For structured list files (inbox, active, log, someday)
    if file_path.name in ("inbox.md", "active.md", "log.md", "someday.md"):
        items = read_items(file_path)
        for it in items:
            clean = it.clean_text
            if clean and len(clean) >= 3:
                chunks.append((it.line_number + 1, clean, it.tag))
        return chunks

    # For project notes or other markdown files: chunk by paragraphs / sections
    current_header = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            current_header = stripped.lstrip("#").strip()
            continue
        if stripped.startswith(("- ", "* ", "- [ ] ", "- [x] ")):
            it = Item(raw_line=line, line_number=i, source_file=file_path.name)
            clean = it.clean_text
            if clean:
                tagged_text = f"[{current_header}] {clean}" if current_header else clean
                chunks.append((i + 1, tagged_text, it.tag or file_path.stem))
        elif len(stripped) > 20:  # Non-trivial paragraph line
            tagged_text = f"[{current_header}] {stripped}" if current_header else stripped
            chunks.append((i + 1, tagged_text, file_path.stem))

    return chunks


class EmbeddingIndex:
    """Manages SQLite embedding storage, incremental indexing, and vector search."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        embed_fn: Optional[Callable[[list[str]], np.ndarray]] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embed_fn = embed_fn

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        _init_db(conn)
        return conn

    def refresh(
        self,
        vaults: list[VaultConfig],
        force: bool = False,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Incrementally update the index for the given vaults.

        Returns the number of chunks embedded.
        """
        if self.embed_fn is None:
            raise ValueError("Embedding function (embed_fn) must be provided to refresh the index.")

        conn = self._get_conn()
        total_indexed = 0

        try:
            if force:
                with conn:
                    conn.execute("DELETE FROM items")
                    conn.execute("DELETE FROM file_meta")

            for vc in vaults:
                v_path = Path(vc.path)
                if not v_path.is_dir():
                    continue

                for md_file in sorted(v_path.rglob("*.md")):
                    # Skip backup, temp, or hidden files
                    if md_file.name.endswith((".bak", ".tmp")) or md_file.name.startswith("."):
                        continue

                    rel_path = str(md_file.relative_to(v_path))
                    cur_mtime = md_file.stat().st_mtime

                    # Check recorded mtime
                    cursor = conn.execute(
                        "SELECT mtime FROM file_meta WHERE vault = ? AND file = ?",
                        (vc.name, rel_path),
                    )
                    row = cursor.fetchone()

                    if not force and row is not None and abs(row[0] - cur_mtime) < 1e-4:
                        continue  # Unchanged

                    chunks = extract_chunks_from_file(v_path, md_file)
                    if progress_cb:
                        progress_cb(f"Indexing {vc.name}/{rel_path} ({len(chunks)} chunks)...")

                    with conn:
                        conn.execute(
                            "DELETE FROM items WHERE vault = ? AND file = ?",
                            (vc.name, rel_path),
                        )

                    if chunks:
                        texts = [c[1] for c in chunks]
                        # Batch embeddings
                        batch_size = 64
                        all_embeddings = []
                        for i in range(0, len(texts), batch_size):
                            batch_texts = texts[i : i + batch_size]
                            embs = self.embed_fn(batch_texts)
                            all_embeddings.append(embs)

                        combined_embs = np.vstack(all_embeddings).astype(np.float32)

                        with conn:
                            for (line_no, text, tag), emb in zip(chunks, combined_embs):
                                conn.execute(
                                    """
                                    INSERT INTO items (vault, file, line, text, tag, mtime, embedding)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (vc.name, rel_path, line_no, text, tag, cur_mtime, emb.tobytes()),
                                )
                        total_indexed += len(chunks)

                    with conn:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO file_meta (vault, file, mtime)
                            VALUES (?, ?, ?)
                            """,
                            (vc.name, rel_path, cur_mtime),
                        )

        finally:
            conn.close()

        return total_indexed

    def search(
        self,
        query: str,
        k: int = 5,
        vault: Optional[str] = None,
        query_embedding: Optional[np.ndarray] = None,
    ) -> list[SearchHit]:
        """Perform vectorized cosine similarity search against stored embeddings."""
        if query_embedding is None:
            if self.embed_fn is None:
                raise ValueError("Embedding function or pre-computed query_embedding required.")
            embs = self.embed_fn([query])
            query_embedding = embs[0].astype(np.float32)
        else:
            query_embedding = query_embedding.astype(np.float32)

        conn = self._get_conn()
        try:
            if vault:
                cursor = conn.execute(
                    "SELECT id, vault, file, line, text, tag, embedding FROM items WHERE vault = ? AND embedding IS NOT NULL",
                    (vault,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, vault, file, line, text, tag, embedding FROM items WHERE embedding IS NOT NULL"
                )

            rows = cursor.fetchall()
            if not rows:
                return []

            meta = []
            emb_list = []
            for row in rows:
                _, v, f, l, t, tag, blob = row
                emb_arr = np.frombuffer(blob, dtype=np.float32)
                meta.append((t, f, l, v, tag))
                emb_list.append(emb_arr)

            # Vectorized cosine similarity
            matrix = np.vstack(emb_list)  # (N, D)
            q = query_embedding.reshape(1, -1)  # (1, D)

            # Norms
            matrix_norm = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
            q_norm = np.linalg.norm(q) + 1e-9

            # Dot products divided by norms
            sims = (np.dot(matrix, q.T) / (matrix_norm * q_norm)).flatten()

            top_k_indices = np.argsort(sims)[::-1][:k]

            results = []
            for idx in top_k_indices:
                t, f, l, v, tag = meta[idx]
                results.append(
                    SearchHit(
                        text=t,
                        file=f,
                        line=l,
                        vault=v,
                        tag=tag,
                        score=float(sims[idx]),
                    )
                )

            return results
        finally:
            conn.close()

    def count(self) -> int:
        """Return total number of embedded chunks in the database."""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM items")
            return cursor.fetchone()[0]
        finally:
            conn.close()
