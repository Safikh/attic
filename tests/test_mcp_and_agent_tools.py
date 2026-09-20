import json
import tempfile
from pathlib import Path
from typer.testing import CliRunner
import pytest

from pkm.cli import app
from pkm import config, store
from pkm.models import PkmConfig, VaultConfig, Section

runner = CliRunner()


@pytest.fixture
def test_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        vault_dir = Path(tmpdir) / "notes" / "testvault"
        vault_dir.mkdir(parents=True)
        
        cfg = PkmConfig(
            vaults={"testvault": VaultConfig(name="testvault", path=vault_dir, alias="t")},
            default_vault="testvault",
            wip_cap=5,
            mcp_allow_direct_writes=False,
        )
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        yield vault_dir, cfg


def test_cli_ctx_json_and_stats(test_env):
    vault_dir, cfg = test_env

    # Seed some data
    store.add_item(vault_dir / "active.md", "Active item [proj1]", section=Section.FLIGHT)
    store.append_to_inbox_with_tag(vault_dir / "inbox.md", "Inbox note", "proj1")
    
    # Add project note
    proj_dir = vault_dir / "projects"
    proj_dir.mkdir(parents=True)
    (proj_dir / "proj1.md").write_text("# proj1\nProject details here.")

    # Test JSON output with stats and include_projects
    res = runner.invoke(app, ["ctx", "--format", "json", "--stats", "--include-projects"])
    assert res.exit_code == 0
    data = json.loads(res.output)

    assert "vaults" in data
    assert "testvault" in data["vaults"]
    vault_data = data["vaults"]["testvault"]

    assert "active.md" in vault_data
    assert "inbox.md" in vault_data
    assert "stats" in vault_data
    assert vault_data["stats"]["wip_count"] == 1
    assert "projects" in vault_data
    assert "proj1" in vault_data["projects"]
    assert "Project details here" in vault_data["projects"]["proj1"]


def test_cli_suggest_and_review(test_env, monkeypatch):
    vault_dir, cfg = test_env

    # 1. Stage a suggestion
    res = runner.invoke(app, [
        "suggest", "Review authentication token issue",
        "--action", "promote",
        "--rationale", "Related to ongoing PR"
    ])
    assert res.exit_code == 0
    assert "Suggestion staged" in res.output

    suggestions_file = vault_dir / "suggestions.md"
    assert suggestions_file.exists()
    content = suggestions_file.read_text()
    assert "- **promote**: Review authentication token issue" in content
    assert "> Rationale: Related to ongoing PR" in content

    # 2. Review suggestions interactively (mock fzf to accept index 0)
    import pkm.cli as pkm_cli
    monkeypatch.setattr(pkm_cli, "_run_fzf", lambda inp, prompt=None, multi=False: ["0: [promote] Review authentication token issue"])

    res_review = runner.invoke(app, ["review-suggestions"])
    assert res_review.exit_code == 0
    assert "Promoted to In Flight" in res_review.output

    # Check active.md now has the task
    active_items = store.read_items(vault_dir / "active.md", Section.FLIGHT)
    assert any("Review authentication token issue" in i.clean_text for i in active_items)

    # Check suggestions.md was cleaned up
    remaining = suggestions_file.read_text()
    assert "Review authentication token issue" not in remaining


def test_mcp_server_tools(test_env):
    vault_dir, cfg = test_env
    from pkm import mcp_server

    # Add items to test queries
    store.add_item(vault_dir / "active.md", "Flight task [feat1]", section=Section.FLIGHT)
    store.append_to_inbox_with_tag(vault_dir / "inbox.md", "Inbox task", "feat1")

    # 1. list_vaults
    vaults = mcp_server.list_vaults()
    assert len(vaults) == 1
    assert vaults[0]["name"] == "testvault"

    # 2. get_active
    active = mcp_server.get_active()
    assert "flight" in active
    assert len(active["flight"]) == 1
    assert "Flight task" in active["flight"][0]["text"]

    # 3. get_inbox
    inbox = mcp_server.get_inbox(tag="feat1")
    assert len(inbox) == 1
    assert "Inbox task" in inbox[0]["text"]

    # 4. search_notes
    matches = mcp_server.search_notes(query="Flight")
    assert len(matches) >= 1
    assert matches[0]["vault"] == "testvault"

    # 5. suggest
    sug_res = mcp_server.suggest("Clean up dead code", action="capture", rationale="Tidy up codebase")
    assert "Suggestion staged" in sug_res
    assert (vault_dir / "suggestions.md").exists()
    assert "Clean up dead code" in (vault_dir / "suggestions.md").read_text()

    # 6. get_stats
    stats = mcp_server.get_stats()
    assert "testvault" in stats["vaults"]
    assert stats["vaults"]["testvault"]["wip_count"] == 1
