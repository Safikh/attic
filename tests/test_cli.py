import tempfile
from pathlib import Path
from typer.testing import CliRunner
from pkm.cli import app
from pkm import config
from pkm.models import PkmConfig, VaultConfig

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
