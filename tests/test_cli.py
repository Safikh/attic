import tempfile
from pathlib import Path
from typer.testing import CliRunner
from pkm.cli import app
from pkm import config, store
from pkm.models import PkmConfig, Section, VaultConfig

runner = CliRunner()

def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Terminal-native personal knowledge management CLI" in result.output
    assert "capture" in result.output
    assert "dash" in result.output
    assert "project" in result.output


def test_cli_capture_and_count(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_vault = Path(tmpdir) / "notes" / "personal"
        fake_vault.mkdir(parents=True)
        
        cfg = PkmConfig(
            vaults={"personal": VaultConfig(name="personal", path=fake_vault, alias="p")},
            default_vault="personal",
            wip_cap=5
        )
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        
        # Test capture
        res = runner.invoke(app, ["capture", "A test thought", "-t", "testproj"])
        assert res.exit_code == 0
        assert "Captured to personal inbox" in res.output
        
        # Check that inbox has it
        inbox_content = (fake_vault / "inbox.md").read_text()
        assert "A test thought" in inbox_content
        assert "[testproj]" in inbox_content
        
        # Test act
        res_act = runner.invoke(app, ["act", "Focus task 1"])
        assert res_act.exit_code == 0
        assert "Added to ## In Flight" in res_act.output
        
        # Test count
        res_count = runner.invoke(app, ["count"])
        assert res_count.exit_code == 0
        assert "1 / 5" in res_count.output


def test_cli_bankrupt(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_vault = Path(tmpdir) / "notes" / "personal"
        fake_vault.mkdir(parents=True)
        
        cfg = PkmConfig(
            vaults={"personal": VaultConfig(name="personal", path=fake_vault, alias="p")},
            default_vault="personal",
        )
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        
        runner.invoke(app, ["capture", "Item to archive"])
        res = runner.invoke(app, ["bankrupt"])
        assert res.exit_code == 0
        assert "Archived 1 item(s)" in res.output
        
        someday = (fake_vault / "someday.md").read_text()
        assert "Item to archive" in someday


def test_wip_limit_enforcement(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_vault = Path(tmpdir) / "notes" / "personal"
        fake_vault.mkdir(parents=True)

        cfg = PkmConfig(
            vaults={"personal": VaultConfig(name="personal", path=fake_vault, alias="p")},
            default_vault="personal",
            wip_cap=2,
        )
        monkeypatch.setattr(config, "load_config", lambda: cfg)

        # Add 2 items to fill capacity
        res1 = runner.invoke(app, ["act", "Task 1"])
        assert res1.exit_code == 0
        res2 = runner.invoke(app, ["act", "Task 2"])
        assert res2.exit_code == 0

        # Adding 3rd item should be BLOCKED by default
        res3 = runner.invoke(app, ["act", "Task 3"])
        assert res3.exit_code == 1
        assert "WIP limit reached (2/2 items In Flight)" in res3.output

        # Verify Task 3 was NOT added
        active_items = store.read_items(fake_vault / "active.md", Section.FLIGHT)
        assert len(active_items) == 2
        assert {it.clean_text for it in active_items} == {"Task 1", "Task 2"}

        # Adding 3rd item with --override should succeed
        res4 = runner.invoke(app, ["act", "Task 3", "--override"])
        assert res4.exit_code == 0
        assert "Added to ## In Flight" in res4.output

        active_items_after = store.read_items(fake_vault / "active.md", Section.FLIGHT)
        assert len(active_items_after) == 3


def test_duplicate_items_selection(monkeypatch):
    from pkm import cli
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_vault = Path(tmpdir) / "notes" / "personal"
        fake_vault.mkdir(parents=True)

        cfg = PkmConfig(
            vaults={"personal": VaultConfig(name="personal", path=fake_vault, alias="p")},
            default_vault="personal",
            wip_cap=5,
        )
        monkeypatch.setattr(config, "load_config", lambda: cfg)

        inbox_path = fake_vault / "inbox.md"
        # Add two identical items to inbox
        store.append_to_inbox(inbox_path, "Duplicate Task")
        store.append_to_inbox(inbox_path, "Duplicate Task")

        inbox_items = store.read_items(inbox_path)
        assert len(inbox_items) == 2

        # Mock fzf to only select the FIRST item (index 000)
        def mock_fzf(options, prompt="> ", multi=False, preview=None):
            # options will contain "000 │ L01 │ 2026-...: Duplicate Task", etc.
            assert len(options) == 2
            return [options[0]]

        monkeypatch.setattr(cli, "_run_fzf", mock_fzf)

        res = runner.invoke(app, ["promote"])
        assert res.exit_code == 0
        assert "Promoted:" in res.output

        # Inbox should still have exactly ONE "Duplicate Task" remaining
        remaining_inbox = store.read_items(inbox_path)
        assert len(remaining_inbox) == 1
        assert remaining_inbox[0].clean_text == inbox_items[1].clean_text

        # Active flight should have exactly ONE "Duplicate Task"
        flight_items = store.read_items(fake_vault / "active.md", Section.FLIGHT)
        assert len(flight_items) == 1

