import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from typer.testing import CliRunner

from pkm import config, store
from pkm.ai import AiEngine
from pkm.cli import app
from pkm.embeddings import EmbeddingIndex, extract_chunks_from_file
from pkm.models import AiConfig, PkmConfig, Section, VaultConfig

runner = CliRunner()


@pytest.fixture
def test_vault(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        v_path = Path(tmpdir) / "notes" / "v1"
        v_path.mkdir(parents=True)
        db_path = Path(tmpdir) / "cache" / "embeddings.db"
        db_path.parent.mkdir(parents=True)

        cfg = PkmConfig(
            vaults={"v1": VaultConfig(name="v1", path=v_path, alias="1")},
            default_vault="v1",
            wip_cap=5,
            ai=AiConfig(api_key="test-api-key"),
        )
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        yield v_path, db_path, cfg


def test_chunking(test_vault):
    v_path, _, _ = test_vault

    # Structured file
    inbox = v_path / "inbox.md"
    inbox.write_text("- 2026-09-20 12:00 [auth]: OAuth token issue\n- 2026-09-20 12:05: Generic thought\n")
    chunks = extract_chunks_from_file(v_path, inbox)
    assert len(chunks) == 2
    assert "OAuth token issue" in chunks[0][1]
    assert chunks[0][2] == "auth"

    # Project note
    proj_dir = v_path / "projects"
    proj_dir.mkdir()
    proj_file = proj_dir / "auth.md"
    proj_file.write_text("# Auth Project\n\n## Decisions\n- Use JWT with short expiry\n")
    proj_chunks = extract_chunks_from_file(v_path, proj_file)
    assert len(proj_chunks) >= 1
    assert any("JWT with short expiry" in c[1] for c in proj_chunks)


def test_embedding_index_incremental_and_search(test_vault):
    v_path, db_path, cfg = test_vault

    inbox = v_path / "inbox.md"
    inbox.write_text("- 2026-09-20 12:00 [auth]: Implement refresh token rotation\n- 2026-09-20 12:10 [db]: Add index on user email\n")

    # Deterministic mock embed function (8 dimensions)
    def mock_embed(texts: list[str]) -> np.ndarray:
        vecs = []
        for t in texts:
            # Deterministic embedding based on presence of keywords
            vec = np.zeros(8, dtype=np.float32)
            if "auth" in t.lower() or "token" in t.lower():
                vec[0] = 1.0
            if "db" in t.lower() or "index" in t.lower():
                vec[1] = 1.0
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            vecs.append(vec)
        return np.array(vecs, dtype=np.float32)

    index = EmbeddingIndex(db_path=db_path, embed_fn=mock_embed)

    # 1. Initial build
    indexed_count = index.refresh(list(cfg.vaults.values()))
    assert indexed_count == 2
    assert index.count() == 2

    # 2. Incremental test: immediate second refresh should do 0 work
    second_count = index.refresh(list(cfg.vaults.values()))
    assert second_count == 0

    # 3. Vector search
    hits = index.search("token auth", k=1)
    assert len(hits) == 1
    assert "token rotation" in hits[0].text
    assert hits[0].score > 0.5


def test_cli_ask_missing_key(test_vault, monkeypatch):
    _, _, cfg = test_vault
    cfg.ai.api_key = ""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PKM_AI_KEY", raising=False)

    res = runner.invoke(app, ["ask", "What tasks do I have?"])
    assert res.exit_code != 0
    assert "Gemini API key not found" in res.output


def test_cli_ask_mocked(test_vault, monkeypatch):
    _, _, cfg = test_vault

    mock_agent_query = MagicMock(return_value="You have 1 active task in Flight: Implement auth.")
    monkeypatch.setattr(AiEngine, "agent_query", mock_agent_query)

    res = runner.invoke(app, ["ask", "What", "should", "I", "work", "on?"])
    assert res.exit_code == 0
    assert "Attic AI" in res.output
    assert "Implement auth" in res.output


def test_cli_summarize_mocked(test_vault, monkeypatch):
    v_path, _, cfg = test_vault
    log_file = v_path / "log.md"
    log_file.write_text("- completed [2026-09-20 11:00]: Finished unit test suite\n")

    mock_complete = MagicMock(return_value="## Highlights\n- Solid progress on test coverage!")
    monkeypatch.setattr(AiEngine, "complete", mock_complete)

    res = runner.invoke(app, ["summarize", "--period", "week"])
    assert res.exit_code == 0
    assert "Summary (Week)" in res.output
    assert "Solid progress" in res.output


def test_cli_triage_mocked(test_vault, monkeypatch):
    v_path, _, cfg = test_vault
    inbox = v_path / "inbox.md"
    inbox.write_text("- 2026-09-20 10:00 [sec]: Rotate production API secrets\n")

    mock_recs = json.dumps([
        {"index": 0, "action": "PROMOTE", "rationale": "High priority security item"}
    ])
    mock_complete = MagicMock(return_value=f"Here is your triage:\n```json\n{mock_recs}\n```")
    monkeypatch.setattr(AiEngine, "complete", mock_complete)

    # Input 's' to stage as suggestion
    res = runner.invoke(app, ["triage", "--smart"], input="s\n")
    assert res.exit_code == 0
    assert "AI Triage Recommendations" in res.output
    assert "Rotate production API secrets" in res.output
    assert "Staged 1 recommendation(s)" in res.output

    # Check suggestions.md was created
    sug_file = v_path / "suggestions.md"
    assert sug_file.exists()
    assert "Rotate production API secrets" in sug_file.read_text()


def test_cli_relate(test_vault, monkeypatch):
    from pkm.embeddings import SearchHit

    mock_search = MagicMock(return_value=[
        SearchHit(text="OAuth token rotation", file="inbox.md", line=1, vault="v1", tag="auth", score=0.92)
    ])
    monkeypatch.setattr(EmbeddingIndex, "refresh", MagicMock(return_value=0))
    monkeypatch.setattr(EmbeddingIndex, "search", mock_search)

    res = runner.invoke(app, ["relate", "authentication", "tokens"])
    assert res.exit_code == 0
    assert "Related Notes for: 'authentication tokens'" in res.output
    assert "92%" in res.output
    assert "OAuth token rotation" in res.output


def test_embedding_index_prunes_deleted_files(test_vault):
    v_path, db_path, cfg = test_vault

    proj_dir = v_path / "projects"
    proj_dir.mkdir(exist_ok=True)
    f1 = proj_dir / "doc1.md"
    f2 = proj_dir / "doc2.md"
    f1.write_text("# Doc 1\n- Some info 1\n")
    f2.write_text("# Doc 2\n- Some info 2\n")

    def mock_embed(texts: list[str]) -> np.ndarray:
        return np.ones((len(texts), 8), dtype=np.float32)

    index = EmbeddingIndex(db_path=db_path, embed_fn=mock_embed)
    count = index.refresh(list(cfg.vaults.values()))
    assert count >= 2
    initial_total = index.count()

    # Now delete doc1.md from disk
    f1.unlink()

    # Refresh index
    index.refresh(list(cfg.vaults.values()))

    # Verify doc1 is no longer in items or file_meta
    with sqlite3.connect(db_path) as conn:
        meta_files = [r[0] for r in conn.execute("SELECT file FROM file_meta WHERE vault = 'v1'").fetchall()]
        assert "projects/doc1.md" not in meta_files
        assert "projects/doc2.md" in meta_files

        item_files = [r[0] for r in conn.execute("SELECT file FROM items WHERE vault = 'v1'").fetchall()]
        assert "projects/doc1.md" not in item_files
        assert "projects/doc2.md" in item_files


def test_save_config_omits_plaintext_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.toml"
        cfg = PkmConfig(
            default_vault="main",
            ai=AiConfig(api_key="secret-key-12345", auto_embed=True),
        )
        with patch("pkm.config.CONFIG_FILE", cfg_file), patch("pkm.config.CONFIG_DIR", Path(tmpdir)):
            config.save_config(cfg)
            content = cfg_file.read_text()
            assert "secret-key-12345" not in content
            assert "auto_embed = true" in content


