import subprocess
import tempfile
from pathlib import Path
from pkm import sync

def test_sync_vault_halts_on_merge_conflict():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        remote_repo = root / "remote.git"
        vault1 = root / "vault1"
        vault2 = root / "vault2"

        # Initialize bare remote
        subprocess.run(["git", "init", "--bare", str(remote_repo)], check=True, capture_output=True)

        # Clone vault1 and create initial active.md
        subprocess.run(["git", "clone", str(remote_repo), str(vault1)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=vault1, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=vault1, check=True)
        
        # Ensure default branch is main
        subprocess.run(["git", "checkout", "-b", "main"], cwd=vault1, check=True, capture_output=True)
        (vault1 / "active.md").write_text("# Active Focus\n\n## In Flight\n- [ ] Task A\n")
        subprocess.run(["git", "add", "active.md"], cwd=vault1, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=vault1, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=vault1, check=True)

        # Clone vault2
        subprocess.run(["git", "clone", str(remote_repo), str(vault2)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=vault2, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=vault2, check=True)

        # Vault 1 makes a change, commits and pushes
        (vault1 / "active.md").write_text("# Active Focus\n\n## In Flight\n- [ ] Task from Vault 1\n")
        subprocess.run(["git", "add", "active.md"], cwd=vault1, check=True)
        subprocess.run(["git", "commit", "-m", "change from vault 1"], cwd=vault1, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=vault1, check=True)

        # Vault 2 makes a conflicting change locally, commits locally
        (vault2 / "active.md").write_text("# Active Focus\n\n## In Flight\n- [ ] Conflicting Task from Vault 2\n")
        subprocess.run(["git", "add", "active.md"], cwd=vault2, check=True)
        subprocess.run(["git", "commit", "-m", "change from vault 2"], cwd=vault2, check=True)

        # Now run sync_vault on vault2
        success, msg = sync.sync_vault(vault2)

        # Verify: sync must FAIL, indicate merge conflict, and NOT have auto-committed
        assert success is False
        assert "Merge conflict detected in: active.md" in msg

        # Verify unmerged status in git
        status_res = subprocess.run(["git", "status", "--porcelain"], cwd=vault2, capture_output=True, text=True)
        # Should have UU (unmerged) status
        assert "UU active.md" in status_res.stdout
