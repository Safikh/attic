import os
import sys
import subprocess
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from rich.console import Console

from pkm.models import PkmConfig

console = Console()

def run_cmd(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

def sync_vault(vault_path: Path) -> tuple[bool, str]:
    if not (vault_path / ".git").is_dir():
        return False, "Not a git repository"
    
    # Check remote connectivity
    res = run_cmd(["git", "ls-remote", "--exit-code", "origin"], cwd=vault_path)
    if res.returncode != 0:
        return False, "offline"
        
    # Get current branch
    res = run_cmd(["git", "branch", "--show-current"], cwd=vault_path)
    if res.returncode != 0 or not res.stdout.strip():
        return False, "Could not determine current branch"
    branch = res.stdout.strip()
    
    # Try pull with rebase
    res = run_cmd(["git", "pull", "--rebase", "--autostash", "origin", branch], cwd=vault_path)
    if res.returncode != 0:
        # Rebase failed, abort
        run_cmd(["git", "rebase", "--abort"], cwd=vault_path)
        # Fall back to merge
        res = run_cmd(["git", "merge", f"origin/{branch}"], cwd=vault_path)
        if res.returncode != 0:
            # Merge conflicts
            run_cmd(["git", "add", "-A"], cwd=vault_path)
            run_cmd(["git", "commit", "-m", "sync: merge conflicts"], cwd=vault_path)
            conflict = True
        else:
            conflict = False
    else:
        conflict = False
        
    # Commit local changes if any
    res = run_cmd(["git", "status", "--porcelain"], cwd=vault_path)
    if res.stdout.strip():
        run_cmd(["git", "add", "-A"], cwd=vault_path)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        run_cmd(["git", "commit", "-m", f"sync: {now}"], cwd=vault_path)
        
    # Push
    res = run_cmd(["git", "push", "origin", branch], cwd=vault_path)
    if res.returncode != 0:
        return False, f"Failed to push: {res.stderr.strip()}"
        
    if conflict:
        return True, "Synced with merge conflicts"
    return True, "Successfully synced"


def sync_all(config: PkmConfig) -> None:
    for name, vault in config.vaults.items():
        vault_path = Path(vault.path).expanduser()
        if (vault_path / ".git").is_dir():
            console.print(f"Syncing vault '{name}'...")
            success, msg = sync_vault(vault_path)
            if success:
                if "conflict" in msg.lower():
                    console.print(f"⚠️  {name}: {msg}")
                else:
                    console.print(f"✅ {name}: {msg}")
            else:
                console.print(f"❌ {name}: {msg}")


def get_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.pkm.sync.plist"

def install_schedule(config: PkmConfig) -> None:
    if sys.platform == "darwin":
        plist_path = get_plist_path()
        interval_seconds = config.sync_interval_minutes * 60
        pkm_cmd = shutil.which("pkm") or "pkm"
        
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pkm.sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>{pkm_cmd}</string>
        <string>sync</string>
    </array>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.pkm_sync.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.pkm_sync_error.log</string>
</dict>
</plist>"""
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_content)
        subprocess.run(["launchctl", "load", str(plist_path)])
        console.print(f"Installed macOS launchd schedule (every {config.sync_interval_minutes} minutes).")
    else:
        # Linux crontab
        try:
            current_crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        except Exception:
            current_crontab = ""
            
        lines = [line for line in current_crontab.splitlines() if "pkm sync" not in line]
        pkm_cmd = shutil.which("pkm") or "pkm"
        lines.append(f"*/{config.sync_interval_minutes} * * * * {pkm_cmd} sync >> {Path.home()}/.pkm_sync.log 2>&1")
        new_crontab = "\n".join(lines) + "\n"
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(new_crontab)
            tmp_path = f.name
            
        subprocess.run(["crontab", tmp_path])
        os.unlink(tmp_path)
        console.print(f"Installed Linux crontab schedule (every {config.sync_interval_minutes} minutes).")


def uninstall_schedule() -> None:
    if sys.platform == "darwin":
        plist_path = get_plist_path()
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)])
            plist_path.unlink()
            console.print("Removed macOS launchd schedule.")
        else:
            console.print("No macOS launchd schedule found.")
    else:
        try:
            current_crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        except Exception:
            current_crontab = ""
            
        lines = [line for line in current_crontab.splitlines() if "pkm sync" not in line]
        new_crontab = "\n".join(lines) + "\n"
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(new_crontab)
            tmp_path = f.name
            
        subprocess.run(["crontab", tmp_path])
        os.unlink(tmp_path)
        console.print("Removed Linux crontab schedule.")


def schedule_status() -> str:
    if sys.platform == "darwin":
        plist_path = get_plist_path()
        if plist_path.exists():
            res = subprocess.run(["launchctl", "list", "com.pkm.sync"], capture_output=True, text=True)
            if res.returncode == 0:
                return "Active (macOS launchd)"
            else:
                return "Installed but not loaded (macOS launchd)"
        return "Not installed"
    else:
        try:
            current_crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
            if "pkm sync" in current_crontab:
                return "Active (Linux crontab)"
        except Exception:
            pass
        return "Not installed"
